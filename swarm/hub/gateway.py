"""The front door every existing tool already speaks: OpenAI-compatible HTTP.

``/v1/models``, ``/v1/chat/completions``, ``/v1/embeddings`` on the hub, with
the owner key as the API key. Point OpenCode, SillyTavern, a Python script —
anything with a "base URL" setting — at ``http://<hub>:8777/v1`` and the
fleet answers. The caller never learns (or cares) which machine did it; the
response carries a ``swarm`` block saying so anyway, because attribution is
law (Law 5).

Routing, cheapest honest path first:

1. **GGUF model** (llama.cpp): make sure it is deployed — on one node if it
   fits, pooled over several if not (``inference.py``) — then proxy the
   request to the head's ``llama-server``, streaming included.
2. **Ollama model**: a one-task, high-priority bag routed by ``model:<name>``
   to a node that holds it. The node calls its own local Ollama. Works
   through NAT (the node pulls; nothing dials in). Streaming is delivered as
   a single SSE chunk — honest about what the path can do.
3. Anything else: 404 with the list of models that *are* servable, never a
   silent fallback to some other model.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import uuid
from typing import Any, Dict, List
from urllib.parse import urlparse

DEPLOY_WAIT_S = float(os.environ.get("SWARM_DEPLOY_WAIT_S", "600"))
TASK_WAIT_S = float(os.environ.get("SWARM_TASK_WAIT_S", "600"))
CHAT_PRIORITY = 10
EMBED_PRIORITY = 5

_CHAT_PASSTHROUGH = (
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "stop", "seed",
    "presence_penalty", "frequency_penalty", "response_format", "tools", "tool_choice",
)


def openai_error(message: str, code: str = "swarm_error", status: int = 400) -> Dict[str, Any]:
    return {"error": {"message": message, "type": code, "code": status}}


class GatewayError(Exception):
    def __init__(self, status: int, message: str, code: str = "swarm_error") -> None:
        super().__init__(message)
        self.status = status
        self.body = openai_error(message, code, status)


class Gateway:
    def __init__(self, hub: Any) -> None:
        self.hub = hub

    # -- /v1/models -----------------------------------------------------------

    def models(self) -> Dict[str, Any]:
        data = []
        for entry in self.hub.inference.catalog():
            data.append(
                {
                    "id": entry["name"],
                    "object": "model",
                    "created": 0,
                    "owned_by": "swarm",
                    "swarm": {
                        "kind": entry["kind"],
                        "runtime": entry["runtime"],
                        "nodes": [n["hostname"] for n in entry["nodes"]],
                        "deployment": entry.get("deployment"),
                    },
                }
            )
        return {"object": "list", "data": data}

    def _available(self, kind: str) -> List[str]:
        return [e["name"] for e in self.hub.inference.catalog() if e["kind"] == kind]

    def _resolve(self, body: Dict[str, Any], kind: str) -> Dict[str, Any]:
        requested = str(body.get("model") or "")
        available = self._available(kind)
        if not requested:
            if len(available) == 1:
                requested = available[0]
            else:
                raise GatewayError(400, f"'model' is required; servable {kind} models: {available}")
        entry = self.hub.inference.resolve(requested, kind=kind)
        if entry is None:
            raise GatewayError(
                404,
                f"no online node can serve {kind} model {requested!r}; servable now: {available}",
                "model_not_found",
            )
        return entry

    # -- /v1/chat/completions -------------------------------------------------

    def chat(self, body: Dict[str, Any], handler: Any) -> None:
        if not isinstance(body.get("messages"), list) or not body["messages"]:
            raise GatewayError(400, "'messages' must be a non-empty list")
        entry = self._resolve(body, "chat")
        if entry["runtime"] == "llama_cpp":
            self._chat_llama(entry, body, handler)
        elif entry["runtime"] == "ollama":
            self._chat_task(entry, body, handler)
        else:
            raise GatewayError(501, f"runtime {entry['runtime']!r} has no chat path")

    def _chat_llama(self, entry: Dict[str, Any], body: Dict[str, Any], handler: Any) -> None:
        inf = self.hub.inference
        dep = inf.deploy(entry["name"])
        if dep.get("state") != "ready":
            dep = inf.wait_ready(entry["name"], DEPLOY_WAIT_S) or dep
        if dep.get("state") != "ready":
            reason = dep.get("reason") or dep.get("state")
            raise GatewayError(503, f"model {entry['name']!r} is not ready ({dep.get('state')}): {reason}", "not_ready")
        inf.touch(entry["name"])
        forwarded = dict(body, model=entry["name"])
        self._proxy(dep["endpoint"], "/v1/chat/completions", forwarded, handler, dep)

    def _proxy(self, endpoint: str, path: str, body: Dict[str, Any], handler: Any, dep: Dict[str, Any]) -> None:
        parsed = urlparse(endpoint)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=TASK_WAIT_S)
        payload = json.dumps(body).encode("utf-8")
        try:
            conn.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
        except OSError as exc:
            self.hub.inference.undeploy(dep["model"], f"head unreachable: {exc}")
            raise GatewayError(502, f"the model's head node did not answer ({exc}); it will be re-placed on the next request") from exc
        stream = bool(body.get("stream"))
        handler.send_response(resp.status)
        handler.send_header("Content-Type", resp.getheader("Content-Type") or "application/json")
        handler.send_header("X-Swarm-Head", str(dep.get("head_node_id") or ""))
        handler.send_header("X-Swarm-Mode", str((dep.get("plan") or {}).get("mode") or ""))
        if stream:
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("Connection", "close")
            handler.end_headers()
            reader = getattr(resp, "read1", None)
            while True:
                chunk = reader(8192) if reader else resp.read(1024)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                handler.wfile.flush()
        else:
            data = resp.read()
            try:
                obj = json.loads(data.decode("utf-8"))
                if isinstance(obj, dict) and resp.status == 200:
                    obj["swarm"] = {
                        "path": "llama_cpp",
                        "mode": (dep.get("plan") or {}).get("mode"),
                        "head": dep.get("head_node_id"),
                        "participants": [
                            {"node_id": p["node_id"], "role": p["role"], "layers": p.get("layers")}
                            for p in (dep.get("plan") or {}).get("participants") or []
                        ],
                    }
                    data = json.dumps(obj).encode("utf-8")
            except ValueError:
                pass
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
        conn.close()

    def _run_bag(self, op: str, params_list: List[Dict[str, Any]], device_class: str, priority: int) -> List[Dict[str, Any]]:
        from ..core.identity import canonical_hash

        queue = self.hub.queue
        keys = [canonical_hash({"op": op, "params": p}) for p in params_list]
        bag_id = queue.submit_bag(op, params_list, keys, device_class=device_class, priority=priority)
        status = queue.wait_for_bag(bag_id, TASK_WAIT_S)
        if status is None or status.get("status") == "open":
            raise GatewayError(504, f"no node finished the {op} work within {int(TASK_WAIT_S)} s (bag {bag_id})", "timeout")
        return queue.results_for_bag(bag_id)

    def _chat_task(self, entry: Dict[str, Any], body: Dict[str, Any], handler: Any) -> None:
        params: Dict[str, Any] = {
            "model": entry["name"],
            "messages": body["messages"],
            # Chat is not a pure function of its prompt when sampled; a
            # request id keeps two identical requests from sharing a result.
            "request_id": uuid.uuid4().hex,
        }
        for key in _CHAT_PASSTHROUGH:
            if key in body:
                params[key] = body[key]
        results = self._run_bag("chat", [params], f"model:{entry['name']}", CHAT_PRIORITY)
        if not results:
            raise GatewayError(502, "the chat task closed without a result")
        row = results[0]
        payload = json.loads(row["payload_json"])
        if row.get("status") == "failed" or not isinstance(payload, dict) or "response" not in payload:
            err = payload.get("error") if isinstance(payload, dict) else payload
            raise GatewayError(502, f"chat failed on every attempt: {err}")
        completion = payload["response"]
        completion["swarm"] = {
            "path": "task",
            "node_id": row.get("node_id"),
            "runtime": payload.get("backend"),
            "elapsed_s": payload.get("elapsed_s"),
        }
        if body.get("stream"):
            self._emit_as_stream(completion, handler)
            return
        data = json.dumps(completion).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    @staticmethod
    def _emit_as_stream(completion: Dict[str, Any], handler: Any) -> None:
        choice = (completion.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        chunk = {
            "id": completion.get("id") or "chatcmpl-swarm",
            "object": "chat.completion.chunk",
            "created": completion.get("created") or int(time.time()),
            "model": completion.get("model"),
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": message.get("content") or ""},
                    "finish_reason": choice.get("finish_reason") or "stop",
                }
            ],
        }
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n")
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()

    # -- /v1/embeddings -------------------------------------------------------

    def embeddings(self, body: Dict[str, Any]) -> Dict[str, Any]:
        raw = body.get("input")
        texts: List[str]
        if isinstance(raw, str):
            texts = [raw]
        elif isinstance(raw, list) and raw and all(isinstance(t, str) for t in raw):
            texts = list(raw)
        else:
            raise GatewayError(400, "'input' must be a string or a non-empty list of strings")
        if any(not t for t in texts):
            raise GatewayError(400, "empty strings cannot be embedded")
        entry = self._resolve(body, "embed")
        params = [{"text": t, "model": entry["name"]} for t in texts]
        results = self._run_bag("embed", params, f"model:{entry['name']}", EMBED_PRIORITY)
        by_seq = {r["seq"]: r for r in results}
        data = []
        nodes = set()
        for i in range(len(texts)):
            row = by_seq.get(i)
            payload = json.loads(row["payload_json"]) if row else None
            if not row or row.get("status") == "failed" or not isinstance(payload, dict) or "embedding" not in payload:
                err = payload.get("error") if isinstance(payload, dict) else "no result"
                raise GatewayError(502, f"embedding input {i} failed: {err}")
            nodes.add(row.get("node_id"))
            data.append({"object": "embedding", "index": i, "embedding": payload["embedding"]})
        return {
            "object": "list",
            "data": data,
            "model": entry["name"],
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
            "swarm": {"path": "task", "nodes": sorted(n for n in nodes if n)},
        }
