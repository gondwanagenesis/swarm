"""``python -m swarm.mcp`` — the swarm as tools for any AI "brain".

A Model Context Protocol server over stdio (newline-delimited JSON-RPC 2.0),
stdlib only. Point Claude Code, OpenCode, Thea — anything that speaks MCP —
at it, and the brain can think on its big machine while handing the chores
to the fleet:

    swarm_status       who is online, what is being served
    swarm_models       every model the fleet can serve right now
    swarm_chat         ask a fleet model (local first; cloud lane if armed)
    swarm_embed        embed texts on whichever node holds the model
    swarm_run_python   run a Python snippet on a CODE WORKER; returns stdout
                       and the value of a variable named `result`
    swarm_map          run `def run(params)` over a list, across code workers

Safety: code a model wrote runs only on nodes that opted in with
``--code-worker`` (Termux phones are ideal: Android sandboxes each app).
With none online the tool says so instead of running it somewhere else.

Register with Claude Code, for example::

    claude mcp add swarm -- python -m swarm.mcp --hub http://<hub>:8777

(key from ``SWARM_OWNER_KEY`` or ``~/.swarm/owner.key``, or ``--key``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional

PROTOCOL_VERSION = "2025-06-18"

RUN_PYTHON_ADAPTER = '''
import contextlib, io, traceback

def run(params):
    buf = io.StringIO()
    ns = {"__name__": "__swarm__"}
    error = None
    with contextlib.redirect_stdout(buf):
        try:
            exec(compile(params["code"], "<swarm_run_python>", "exec"), ns)
        except Exception:
            error = traceback.format_exc(limit=5)
    import platform, socket
    out = {"stdout": buf.getvalue()[-20000:], "host": socket.gethostname(), "os": platform.system()}
    if "result" in ns:
        try:
            import json
            json.dumps(ns["result"])
            out["result"] = ns["result"]
        except Exception:
            out["result"] = repr(ns["result"])[:20000]
    if error:
        out["error"] = error
    return out
'''

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "swarm_status",
        "description": "Nodes in the swarm (online or not), and which models are being served where.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "swarm_models",
        "description": "Every chat/embedding model some online node can serve right now.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "swarm_chat",
        "description": "Ask a model on the swarm. Local fleet first; the hub's cloud lane only if armed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "model name from swarm_models"},
                "prompt": {"type": "string"},
                "max_tokens": {"type": "integer", "default": 512},
            },
            "required": ["model", "prompt"],
        },
    },
    {
        "name": "swarm_embed",
        "description": "Embed one or more texts on whichever node holds the embedding model.",
        "inputSchema": {
            "type": "object",
            "properties": {"model": {"type": "string"}, "texts": {"type": "array", "items": {"type": "string"}}},
            "required": ["model", "texts"],
        },
    },
    {
        "name": "swarm_run_python",
        "description": (
            "Run a Python snippet on a swarm code-worker node (never on the owner's own machines unless they opted in). "
            "Returns printed output and the value of a variable named `result`. Slow is fine; it retries elsewhere if a node dies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "timeout_s": {"type": "number", "default": 300},
            },
            "required": ["code"],
        },
    },
    {
        "name": "swarm_map",
        "description": (
            "Run `def run(params): ...` from `function_source` once per input, spread across code-worker nodes. "
            "Inputs are JSON objects (other values arrive as {'item': value}). Results come back in input order."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_source": {"type": "string"},
                "inputs": {"type": "array"},
                "timeout_s": {"type": "number", "default": 300},
            },
            "required": ["function_source", "inputs"],
        },
    },
]


class ToolError(Exception):
    pass


class SwarmTools:
    def __init__(self, hub: str, key: Optional[str]) -> None:
        from .cli import Client

        self.client = Client(hub, key)

    def call(self, path: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> Any:
        try:
            return self.client.call(path, payload, timeout=timeout)
        except SystemExit as exc:  # Client reports hub errors this way
            raise ToolError(str(exc)) from None

    def status(self, _args: Dict[str, Any]) -> Any:
        nodes = self.call("/api/nodes")["nodes"]
        now = time.time()
        models = self.call("/api/models")
        return {
            "nodes": [
                {"hostname": n["hostname"], "os": n["os"], "online": bool(n.get("last_seen") and now - n["last_seen"] < 90)}
                for n in nodes
            ],
            "deployments": [
                {"model": d["model"], "state": d["state"]} for d in models.get("deployments") or []
            ],
        }

    def models(self, _args: Dict[str, Any]) -> Any:
        return [
            {"name": m["name"], "kind": m["kind"], "runtime": m["runtime"], "nodes": [n["hostname"] for n in m["nodes"]]}
            for m in self.call("/api/models")["catalog"]
        ]

    def chat(self, args: Dict[str, Any]) -> Any:
        body = self.call(
            "/v1/chat/completions",
            {
                "model": args["model"],
                "messages": [{"role": "user", "content": args["prompt"]}],
                "max_tokens": int(args.get("max_tokens") or 512),
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=900,
        )
        msg = body["choices"][0]["message"]
        return {"content": msg.get("content") or msg.get("reasoning_content") or "", "swarm": body.get("swarm")}

    def embed(self, args: Dict[str, Any]) -> Any:
        body = self.call("/v1/embeddings", {"model": args["model"], "input": list(args["texts"])}, timeout=600)
        return {"dims": [len(d["embedding"]) for d in body["data"]], "embeddings": [d["embedding"] for d in body["data"]]}

    def _bag(self, op: str, params_list: List[Dict[str, Any]], timeout: float) -> List[Dict[str, Any]]:
        sub = self.call(
            "/api/bag/submit",
            {"op": op, "params_list": params_list, "device_class": "role:code-worker", "priority": 3},
        )
        bag = sub["bag_id"]
        deadline = time.time() + timeout
        while True:
            st = self.call(f"/api/bag/{bag}")
            if st["status"] != "open":
                break
            if not st.get("servable", True):
                self.call(f"/api/bag/{bag}/cancel", {})
                raise ToolError(
                    "no code-worker node is online. Start an agent with --code-worker on a machine you are "
                    "happy to run AI-written code on (a Termux phone is ideal), then retry."
                )
            if time.time() > deadline:
                self.call(f"/api/bag/{bag}/cancel", {})
                raise ToolError(f"timed out after {int(timeout)} s ({st['done']}/{st['total']} done)")
            time.sleep(0.5)
        return sorted(self.call(f"/api/bag/{bag}/results", timeout=120)["results"], key=lambda r: r["seq"])

    def run_python(self, args: Dict[str, Any]) -> Any:
        timeout = float(args.get("timeout_s") or 300)
        rows = self._bag(
            "run_python",
            [{"adapter_source": RUN_PYTHON_ADAPTER, "adapter_timeout_s": timeout, "adapter_params": {"code": args["code"]}}],
            timeout + 60,
        )
        row = rows[0]
        payload = row["payload"] or {}
        if not row["ok"] or not payload.get("ok", True):
            raise ToolError(payload.get("error") or "the code worker could not run it")
        out = dict(payload.get("payload") or {})
        out["node"] = (row.get("node_id") or "")[:8]
        return out

    def map(self, args: Dict[str, Any]) -> Any:
        timeout = float(args.get("timeout_s") or 300)
        source = str(args["function_source"])
        if "def run" not in source:
            raise ToolError("function_source must define run(params)")
        items = [x if isinstance(x, dict) else {"item": x} for x in args["inputs"]]
        rows = self._bag(
            "map",
            [{"adapter_source": source, "adapter_timeout_s": timeout, "adapter_params": p} for p in items],
            timeout * max(1, len(items)) + 60,
        )
        results = []
        for r in rows:
            payload = r["payload"] or {}
            ok = r["ok"] and payload.get("ok", True)
            results.append({"index": r["seq"], "ok": ok, **({"result": payload.get("payload")} if ok else {"error": payload.get("error")})})
        return results


def serve(tools: SwarmTools, stdin: Any = None, stdout: Any = None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {
        "swarm_status": tools.status,
        "swarm_models": tools.models,
        "swarm_chat": tools.chat,
        "swarm_embed": tools.embed,
        "swarm_run_python": tools.run_python,
        "swarm_map": tools.map,
    }

    def reply(msg_id: Any, result: Any = None, error: Optional[Dict[str, Any]] = None) -> None:
        out: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            out["error"] = error
        else:
            out["result"] = result
        stdout.write(json.dumps(out) + "\n")
        stdout.flush()

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply(None, error={"code": -32700, "message": "parse error"})
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        if msg_id is None:
            continue  # notifications (initialized, cancelled) need no answer
        if method == "initialize":
            reply(
                msg_id,
                {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "swarm", "version": "1.0"},
                    "instructions": "Tools that run on the owner's device swarm. Slow is fine; results are checked.",
                },
            )
        elif method == "ping":
            reply(msg_id, {})
        elif method == "tools/list":
            reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            handler = handlers.get(str(name))
            if handler is None:
                reply(msg_id, error={"code": -32602, "message": f"unknown tool {name!r}"})
                continue
            try:
                result = handler(params.get("arguments") or {})
                reply(msg_id, {"content": [{"type": "text", "text": json.dumps(result, indent=1, default=str)}], "isError": False})
            except ToolError as exc:
                reply(msg_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
            except Exception as exc:
                reply(msg_id, {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True})
        else:
            reply(msg_id, error={"code": -32601, "message": f"method {method!r} not found"})


def main(argv: Optional[List[str]] = None) -> int:
    from .cli import _key

    ap = argparse.ArgumentParser(prog="swarm-mcp", description="MCP server exposing the swarm as tools")
    ap.add_argument("--hub", default=os.environ.get("SWARM_HUB", "http://127.0.0.1:8777"))
    ap.add_argument("--key", default=None)
    args = ap.parse_args(argv)
    serve(SwarmTools(args.hub, _key(args.key)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
