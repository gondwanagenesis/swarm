"""Hub server. Stdlib http.server + sqlite registry. Endpoints:

GET  /api/ping       liveness (also the RTT probe target)
POST /api/echo       byte mirror (the bandwidth measurement target)
POST /api/register   node registration: {profile, capability, benchmarks}
POST /api/heartbeat  {node_id}
POST /api/link       link measurement report
GET  /api/nodes      node registry summary
GET  /api/nodes/<id> full stored profile
GET  /api/links      link matrix
GET  /api/anomalies  recent anomalies
GET  /               dashboard
"""

from __future__ import annotations

import argparse
import contextlib
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from ..core.identity import canonical_hash
from ..core.models import BenchResult, LinkMeasurement, NodeProfile
from ..core.serde import from_dict
from ..integrator.llm import from_env as llm_from_env
from .dashboard import render_dashboard
from .queue import WorkQueue
from .registry import Registry
from .scheduler import ChunkPlanner

MAX_BODY = 64 * 1024 * 1024


def _swarm_version() -> str:
    try:
        import swarm

        return swarm.__version__
    except Exception:
        return "unknown"


class Hub:
    def __init__(self, host: str = "127.0.0.1", port: int = 8777, db_path: str = ":memory:") -> None:
        self.host = host
        self.port = port
        self.registry = Registry(db_path)
        self.queue = WorkQueue(self.registry._conn, lock=self.registry._lock)
        self.planner = ChunkPlanner(self.registry)
        self.llm_config = llm_from_env()
        self.agent_payload: Optional[bytes] = None
        self.started_at = time.time()
        self._httpd = None

    def set_agent_payload(self, payload: bytes) -> None:
        self.agent_payload = payload

    def make_handler(self) -> type:
        hub = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "SwarmHub/0.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_json(self, obj: Any, status: int = 200) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, html: str, status: int = 200) -> None:
                body = html.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise ValueError("body too large")
                return self.rfile.read(length)

            def _read_json(self) -> Dict[str, Any]:
                raw = self._read_body()
                if not raw:
                    return {}
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("expected JSON object")
                return data

            def _bad(self, msg: str) -> None:
                self._send_json({"ok": False, "error": msg}, status=400)

            def do_GET(self) -> None:
                try:
                    path = self.path.split("?", 1)[0]
                    if path == "/api/ping":
                        self._send_json({"ok": True, "ts": time.time()})
                    elif path == "/api/config":
                        self._send_json(
                            {
                                "llm": hub.llm_config.status(),
                                "version": _swarm_version(),
                                "uptime_s": round(time.time() - hub.started_at, 1),
                            }
                        )
                    elif path == "/agent.pyz":
                        if hub.agent_payload is None:
                            self._send_json(
                                {"ok": False, "error": "agent bundle not built on this hub"},
                                status=404,
                            )
                        else:
                            body = hub.agent_payload
                            self.send_response(200)
                            self.send_header("Content-Type", "application/octet-stream")
                            self.send_header("Content-Disposition", "attachment; filename=swarm-agent.pyz")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                    elif path == "/api/nodes":
                        self._send_json({"nodes": hub.registry.list_nodes()})
                    elif path.startswith("/api/nodes/"):
                        node_id = path.rsplit("/", 1)[-1]
                        detail = hub.registry.node_detail(node_id)
                        if detail is None:
                            self._send_json({"ok": False, "error": "unknown node"}, status=404)
                        else:
                            detail["benches"] = hub.registry.latest_benches(node_id)
                            self._send_json(detail)
                    elif path == "/api/links":
                        self._send_json({"links": hub.registry.list_links()})
                    elif path == "/api/anomalies":
                        self._send_json({"anomalies": hub.registry.recent_anomalies()})
                    elif path == "/api/bags":
                        self._send_json({"bags": hub.queue.open_bags()})
                    elif path.startswith("/api/bag/"):
                        bag_id = path.rsplit("/", 1)[-1]
                        status = hub.queue.bag_status(bag_id)
                        if status is None:
                            self._send_json({"ok": False, "error": "unknown bag"}, status=404)
                        else:
                            self._send_json(status)
                    elif path in ("/", "/index.html"):
                        self._send_html(render_dashboard(hub.registry, hub.queue))
                    else:
                        self._send_json({"ok": False, "error": "not found"}, status=404)
                except BrokenPipeError:
                    pass
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        self._send_json({"ok": False, "error": str(exc)}, status=500)

            def do_POST(self) -> None:
                try:
                    path = self.path.split("?", 1)[0]
                    if path == "/api/echo":
                        body = self._read_body()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    elif path == "/api/register":
                        payload = self._read_json()
                        node_id = hub.handle_register(payload)
                        self._send_json({"ok": True, "node_id": node_id})
                    elif path == "/api/heartbeat":
                        payload = self._read_json()
                        known = hub.registry.heartbeat(str(payload.get("node_id", "")))
                        self._send_json({"ok": known})
                    elif path == "/api/link":
                        payload = self._read_json()
                        link = from_dict(LinkMeasurement, payload)
                        hub.registry.record_link(link)
                        self._send_json({"ok": True})
                    elif path == "/api/bag/submit":
                        payload = self._read_json()
                        bag_id = hub.handle_submit(payload)
                        self._send_json({"ok": True, "bag_id": bag_id})
                    elif path == "/api/tasks/pull":
                        payload = self._read_json()
                        items = hub.handle_pull(str(payload.get("node_id", "")))
                        self._send_json({"ok": True, "tasks": items})
                    elif path == "/api/tasks/complete":
                        payload = self._read_json()
                        outcome = hub.queue.complete(
                            str(payload.get("node_id", "")),
                            list(payload.get("results") or []),
                        )
                        self._send_json({"ok": True, **outcome})
                    elif path == "/api/tasks/renew":
                        payload = self._read_json()
                        renewed = hub.queue.renew(
                            str(payload.get("node_id", "")),
                            str(payload.get("bag_id", "")),
                            [int(s) for s in (payload.get("seqs") or [])],
                            float(payload.get("lease_seconds") or 60.0),
                        )
                        self._send_json({"ok": True, "renewed": renewed})
                    else:
                        self._send_json({"ok": False, "error": "not found"}, status=404)
                except ValueError as exc:
                    self._bad(str(exc))
                except BrokenPipeError:
                    pass
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        self._send_json({"ok": False, "error": str(exc)}, status=500)

        return Handler

    def handle_register(self, payload: Dict[str, Any]) -> str:
        profile_data = payload.get("profile") or {}
        capability_data = payload.get("capability") or {}
        profile = from_dict(NodeProfile, profile_data)
        self.registry.upsert_node(
            profile,
            capability_data,
            json.dumps(profile_data, sort_keys=True),
            json.dumps(capability_data, sort_keys=True),
        )
        for bench_data in payload.get("benchmarks") or []:
            bench = from_dict(BenchResult, bench_data)
            self.registry.record_bench(profile.node_id, bench)
        return profile.node_id

    def handle_submit(self, payload: Dict[str, Any]) -> str:
        op = str(payload.get("op") or "")
        params_list = payload.get("params_list") or []
        if not op or not isinstance(params_list, list) or not params_list:
            raise ValueError("submit requires op and non-empty params_list")
        idem_keys = [canonical_hash({"op": op, "params": params}) for params in params_list]
        return self.queue.submit_bag(op, params_list, idem_keys)

    def handle_pull(self, node_id: str) -> list:
        open_bags = self.queue.open_bags()
        if not open_bags:
            return []
        status = open_bags[0]
        self.planner.touch_worker(node_id)
        stats = self.queue.node_stats(node_id)
        chunk, lease, predicted_ms = self.planner.plan(
            node_id=node_id,
            op=status["op"],
            bag_total=status["total"],
            bag_remaining=status["queued"],
            queue_stats=stats,
            active_worker_count=self.planner.active_count(),
        )
        items = self.queue.pull(node_id, chunk, lease, predicted_ms)
        return items

    def serve_forever(self) -> Tuple[str, int]:
        handler = self.make_handler()
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = self._httpd.server_address[1]
        self._httpd.serve_forever()
        return self.host, self.port

    def start_background(self) -> Tuple[str, int]:
        import threading

        handler = self.make_handler()
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = self._httpd.server_address[1]
        thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        thread.start()
        return self.host, self.port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        self.registry.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Swarm hub")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--db", default=":memory:", help="sqlite path (default in-memory)")
    parser.add_argument(
        "--serve-agent",
        action="store_true",
        help="build and serve the single-file agent at /agent.pyz",
    )
    args = parser.parse_args()
    hub = Hub(host=args.host, port=args.port, db_path=args.db)
    if args.serve_agent:
        from .agentbundle import build_agent_pyz

        payload = build_agent_pyz()
        hub.set_agent_payload(payload)
        print(f"agent bundle ready at /agent.pyz ({len(payload)} bytes)")
    print(f"swarm hub listening on http://{args.host}:{args.port} (dashboard at /)")
    try:
        hub.serve_forever()
    except KeyboardInterrupt:
        hub.stop()


if __name__ == "__main__":
    main()
