"""A tiny in-process Ollama stand-in: /api/tags, /api/version,
/api/embeddings, /v1/chat/completions. Deterministic, so content addressing
and idempotency behave exactly as with the real runtime."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = [
    {"name": "tiny-chat:latest", "size": 1000, "capabilities": ["completion"]},
    {"name": "tiny-embed:latest", "size": 500, "capabilities": ["embedding"]},
]


class FakeOllama:
    def __init__(self, models=None, fail_chat=0):
        self.models = models if models is not None else MODELS
        self.fail_chat = fail_chat  # fail this many chat calls first
        self.calls = []
        owner = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def _send(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/tags":
                    self._send({"models": owner.models})
                elif self.path == "/api/version":
                    self._send({"version": "0.0-fake"})
                else:
                    self._send({"error": "nope"}, 404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                owner.calls.append((self.path, req))
                if self.path == "/api/embeddings":
                    digest = hashlib.sha256((req["model"] + req["prompt"]).encode()).digest()
                    self._send({"embedding": [b / 255.0 for b in digest[:8]]})
                elif self.path == "/v1/chat/completions":
                    if owner.fail_chat > 0:
                        owner.fail_chat -= 1
                        self._send({"error": "model is loading"}, 503)
                        return
                    text = "echo: " + str(req["messages"][-1]["content"])
                    self._send(
                        {
                            "id": "chatcmpl-1",
                            "object": "chat.completion",
                            "created": 1,
                            "model": req["model"],
                            "choices": [
                                {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
                            ],
                        }
                    )
                else:
                    self._send({"error": "nope"}, 404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
