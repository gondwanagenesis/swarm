"""Stand-ins for llama.cpp's binaries, for tests on machines without them.

    python fake_llama.py rpc    --host H --port P [...]   -> accepts TCP, like rpc-server
    python fake_llama.py server --host H --port P --rpc a,b --tensor-split x,y [...]

The fake server answers /health and /v1/chat/completions, and echoes the
--rpc / --tensor-split it was launched with so a test can prove the hub's
plan reached the command line.
"""

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def arg(flag, default=None):
    argv = sys.argv
    return argv[argv.index(flag) + 1] if flag in argv else default


def rpc(host, port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(8)
    while True:
        conn, _ = srv.accept()
        conn.close()


def server(host, port):
    launched = {"rpc": arg("--rpc"), "tensor_split": arg("--tensor-split"), "alias": arg("--alias")}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_GET(self):
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            text = "fake-llama heard: " + str(req["messages"][-1]["content"])
            if req.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for piece in (text[:10], text[10:]):
                    chunk = {"choices": [{"index": 0, "delta": {"content": piece}}]}
                    self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                return
            body = json.dumps(
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "model": req.get("model"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                    "launched": launched,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer((host, port), H).serve_forever()


if __name__ == "__main__":
    mode = sys.argv[1]
    host = arg("--host", "127.0.0.1")
    port = int(arg("--port", "0"))
    (rpc if mode == "rpc" else server)(host, port)
    threading.Event().wait()
