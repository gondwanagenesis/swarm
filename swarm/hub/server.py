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
from typing import Any, Dict, Tuple

from ..core.models import BenchResult, LinkMeasurement, NodeProfile
from ..core.serde import from_dict
from .dashboard import render_dashboard
from .registry import Registry

MAX_BODY = 64 * 1024 * 1024


class Hub:
    def __init__(
        self, host: str = "127.0.0.1", port: int = 8777, db_path: str = ":memory:"
    ) -> None:
        self.host = host
        self.port = port
        self.registry = Registry(db_path)
        self.started_at = time.time()
        self._httpd = None

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
                    elif path == "/api/nodes":
                        self._send_json({"nodes": hub.registry.list_nodes()})
                    elif path.startswith("/api/nodes/"):
                        node_id = path.rsplit("/", 1)[-1]
                        detail = hub.registry.node_detail(node_id)
                        if detail is None:
                            self._send_json(
                                {"ok": False, "error": "unknown node"}, status=404
                            )
                        else:
                            detail["benches"] = hub.registry.latest_benches(node_id)
                            self._send_json(detail)
                    elif path == "/api/links":
                        self._send_json({"links": hub.registry.list_links()})
                    elif path == "/api/anomalies":
                        self._send_json({"anomalies": hub.registry.recent_anomalies()})
                    elif path in ("/", "/index.html"):
                        self._send_html(render_dashboard(hub.registry))
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
    parser.add_argument(
        "--db", default=":memory:", help="sqlite path (default in-memory)"
    )
    args = parser.parse_args()
    hub = Hub(host=args.host, port=args.port, db_path=args.db)
    print(f"swarm hub listening on http://{args.host}:{args.port} (dashboard at /)")
    try:
        hub.serve_forever()
    except KeyboardInterrupt:
        hub.stop()


if __name__ == "__main__":
    main()
