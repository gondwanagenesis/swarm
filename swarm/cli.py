"""``python -m swarm.cli`` — the owner's remote control for a hub.

    status                      nodes, what each can run, what is being served
    models                      every servable model and its deployment state
    chat MODEL "prompt"         one question to the fleet (OpenAI path)
    deploy MODEL [--force-shard] [--wait S]   start serving a GGUF model now
    undeploy MODEL              stop serving it; memory goes back to the hosts
    plan MODEL [--force-shard]  where it WOULD go, without starting anything
    map FN.py INPUTS.jsonl [-o OUT.jsonl]     run FN.py's run(params) over
                                every input line, across every node
    join                        print the add-a-device page URL

Hub: ``--hub`` or ``SWARM_HUB`` (default http://127.0.0.1:8777). Key:
``--key`` or ``SWARM_OWNER_KEY`` or ``~/.swarm/owner.key``. Stdlib only.

``map`` is general-purpose compute: your function ships as source, runs in a
subprocess on each worker (with whatever packages that worker's Python has),
and results come back in input order. Each line of INPUTS.jsonl is a JSON
object passed as ``params`` (non-objects arrive as ``{"item": value}``).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


def _key(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    if os.environ.get("SWARM_OWNER_KEY"):
        return os.environ["SWARM_OWNER_KEY"]
    path = Path.home() / ".swarm" / "owner.key"
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


class Client:
    def __init__(self, hub: str, key: Optional[str]) -> None:
        self.hub = hub.rstrip("/")
        self.key = key

    def call(self, path: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(self.hub + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
                msg = (parsed.get("error") or {}).get("message") if isinstance(parsed.get("error"), dict) else parsed.get("error")
            except ValueError:
                parsed, msg = {}, body[:300]
            if exc.code == 409 and parsed.get("moved_to") and parsed["moved_to"].rstrip("/") != self.hub:
                # This hub stepped aside after a failover: follow it, once.
                print(f"(hub moved to {parsed['moved_to']} — epoch {parsed.get('epoch')}; following)", file=sys.stderr)
                self.hub = parsed["moved_to"].rstrip("/")
                return self.call(path, payload, timeout)
            raise SystemExit(f"hub said {exc.code}: {msg}") from None
        except urllib.error.URLError as exc:
            raise SystemExit(f"cannot reach the hub at {self.hub}: {exc.reason}") from None


def cmd_status(c: Client, _args: argparse.Namespace) -> int:
    nodes = c.call("/api/nodes")["nodes"]
    models = c.call("/api/models")
    now = time.time()
    print(f"{len(nodes)} node(s)")
    for n in nodes:
        age = now - (n.get("last_seen") or 0)
        state = "online" if age < 90 else f"seen {int(age // 60)} min ago"
        print(f"  {n['hostname']:<24} {n['os']:<8} {state:<16} tier={n.get('tier')}")
    deps = models.get("deployments") or []
    if deps:
        print("serving:")
        for d in deps:
            parts = ((d.get("plan") or {}).get("participants")) or []
            where = ", ".join(p.get("hostname") or p["node_id"][:8] for p in parts)
            print(f"  {d['model']:<32} {d['state']:<8} {where}  {d.get('endpoint') or ''}")
    return 0


def cmd_models(c: Client, _args: argparse.Namespace) -> int:
    for m in c.call("/api/models")["catalog"]:
        dep = (m.get("deployment") or {}).get("state", "on demand")
        holders = ", ".join(n["hostname"] for n in m["nodes"])
        size = m.get("size_bytes")
        size_txt = f"{size / 1e9:.1f} GB" if isinstance(size, int) else "?"
        print(f"{m['name']:<36} {m['kind']:<6} {size_txt:>8}  {dep:<9} on {holders}")
    return 0


def cmd_chat(c: Client, args: argparse.Namespace) -> int:
    started = time.time()
    request: Dict[str, Any] = {
        "model": args.model,
        "messages": [{"role": "user", "content": args.prompt}],
        "max_tokens": args.max_tokens,
    }
    if not args.think:
        # Thinking models (Qwen3.5 and friends) otherwise spend the whole
        # budget reasoning; runtimes that do not know the flag ignore it.
        request["chat_template_kwargs"] = {"enable_thinking": False}
    body = c.call("/v1/chat/completions", request, timeout=900)
    msg = body["choices"][0]["message"]
    reasoning = (msg.get("reasoning_content") or "").strip()
    if reasoning and (args.think or not msg.get("content")):
        print("[thinking]", reasoning, "\n")
    print(msg.get("content") or "")
    sw = body.get("swarm") or {}
    usage = body.get("usage") or {}
    timings = body.get("timings") or {}
    tps = timings.get("predicted_per_second")
    extra = f", {tps:.1f} tok/s" if tps else ""
    print(f"\n[{sw.get('path')} / {sw.get('mode') or sw.get('runtime') or ''}, "
          f"{usage.get('completion_tokens', '?')} tokens in {time.time() - started:.1f}s{extra}]", file=sys.stderr)
    return 0


def cmd_deploy(c: Client, args: argparse.Namespace) -> int:
    out = c.call(
        "/api/models/deploy",
        {"model": args.model, "force_shard": args.force_shard, "wait_s": args.wait, "ctx": args.ctx},
        timeout=args.wait + 30,
    )
    dep = out["deployment"]
    print(f"{dep['model']}: {dep['state']}")
    plan = dep.get("plan") or {}
    print(f"  {plan.get('reason')}")
    for p in plan.get("participants") or []:
        print(f"  - {p.get('hostname') or p['node_id'][:8]:<20} {p['role']:<5} layers={p.get('layers')}")
    if dep["state"] == "failed":
        return 1
    return 0


def cmd_undeploy(c: Client, args: argparse.Namespace) -> int:
    print("stopped" if c.call("/api/models/undeploy", {"model": args.model})["ok"] else "was not running")
    return 0


def cmd_plan(c: Client, args: argparse.Namespace) -> int:
    from urllib.parse import quote

    q = f"/api/models/plan?model={quote(args.model)}" + ("&force_shard=1" if args.force_shard else "")
    print(json.dumps(c.call(q), indent=2))
    return 0


def cmd_map(c: Client, args: argparse.Namespace) -> int:
    source = Path(args.fn).read_text(encoding="utf-8")
    if "def " + args.entrypoint not in source:
        raise SystemExit(f"{args.fn} has no function named {args.entrypoint!r}")
    items: List[Dict[str, Any]] = []
    for line in Path(args.inputs).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        items.append(value if isinstance(value, dict) else {"item": value})
    params_list = [
        {
            "adapter_source": source,
            "adapter_entrypoint": args.entrypoint,
            "adapter_timeout_s": args.timeout,
            "adapter_params": p,
        }
        for p in items
    ]
    bag = c.call("/api/bag/submit", {"op": "map", "params_list": params_list})["bag_id"]
    print(f"submitted {len(items)} item(s) as {bag}", file=sys.stderr)
    last = -1
    while True:
        st = c.call(f"/api/bag/{bag}")
        if st["done"] != last:
            print(f"  {st['done']}/{st['total']} done ({st.get('failed', 0)} failed)", file=sys.stderr)
            last = st["done"]
        if st["status"] != "open":
            break
        if not st.get("servable", True):
            print(f"  blocked: {st.get('blocked_reason')}", file=sys.stderr)
        time.sleep(1.0)
    res = c.call(f"/api/bag/{bag}/results", timeout=300)["results"]
    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout  # noqa: SIM115
    failed = 0
    for row in sorted(res, key=lambda r: r["seq"]):
        payload = row["payload"] or {}
        ok = row["ok"] and payload.get("ok", True)
        failed += 0 if ok else 1
        record = {"index": row["seq"], "ok": ok, "node": row["node_id"][:8]}
        record["result" if ok else "error"] = payload.get("payload") if ok else payload.get("error")
        out.write(json.dumps(record) + "\n")
    if args.out:
        out.close()
        print(f"wrote {len(res)} result(s) to {args.out} ({failed} failed)", file=sys.stderr)
    return 1 if failed else 0


def cmd_join(c: Client, _args: argparse.Namespace) -> int:
    print(f"open in a browser: {c.hub}/join" + (f"?key={c.key}" if c.key else ""))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Windows consoles default to cp1252; model output is any language.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="swarm", description="Talk to a swarm hub.")
    ap.add_argument("--hub", default=os.environ.get("SWARM_HUB", "http://127.0.0.1:8777"))
    ap.add_argument("--key", default=None, help="owner key (default: SWARM_OWNER_KEY or ~/.swarm/owner.key)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("models")
    p = sub.add_parser("chat")
    p.add_argument("model")
    p.add_argument("prompt")
    p.add_argument("--max-tokens", type=int, default=512)
    p.add_argument("--think", action="store_true", help="let thinking models reason first (slower)")
    p = sub.add_parser("deploy")
    p.add_argument("model")
    p.add_argument("--force-shard", action="store_true", help="split across helpers even if it fits one node")
    p.add_argument("--wait", type=float, default=300.0)
    p.add_argument("--ctx", type=int, default=4096)
    p = sub.add_parser("undeploy")
    p.add_argument("model")
    p = sub.add_parser("plan")
    p.add_argument("model")
    p.add_argument("--force-shard", action="store_true")
    p = sub.add_parser("map")
    p.add_argument("fn")
    p.add_argument("inputs")
    p.add_argument("-o", "--out", default=None)
    p.add_argument("--entrypoint", default="run")
    p.add_argument("--timeout", type=float, default=300.0, help="seconds per item")
    sub.add_parser("join")
    args = ap.parse_args(argv)
    client = Client(args.hub, _key(args.key))
    handler = {
        "status": cmd_status, "models": cmd_models, "chat": cmd_chat, "deploy": cmd_deploy,
        "undeploy": cmd_undeploy, "plan": cmd_plan, "map": cmd_map, "join": cmd_join,
    }[args.cmd]
    return handler(client, args)


if __name__ == "__main__":
    sys.exit(main())
