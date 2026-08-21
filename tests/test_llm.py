import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from swarm.hub.server import Hub
from swarm.integrator.llm import LlmClient, LlmConfig, from_env, masked_key


class _MockLlmHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode())
        assert self.headers.get("Authorization") == "Bearer sk-test-1234567890"
        assert body["model"] == "test-model"
        resp = json.dumps({"choices": [{"message": {"content": "adapter code here"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def test_never_calls_network_when_unarmed():
    client = LlmClient(from_env(env={}))
    assert client.config.armed is False
    assert client.chat([{"role": "user", "content": "hi"}]) is None


def test_masked_key():
    assert masked_key("") is None
    assert masked_key("short") == "****"
    masked = masked_key("sk-neuralwatt-abcdef123456")
    assert masked.startswith("sk-n") and masked.endswith("3456")
    assert "abcdef" not in masked


def test_neuralwatt_env_precedence():
    cfg = from_env(
        env={
            "SWARM_NEURALWATT_API_KEY": "nw-123456789",
            "SWARM_LLM_API_KEY": "sk-other",
            "SWARM_LLM_MODEL": "glm-test",
        }
    )
    assert cfg.provider == "neuralwatt"
    assert cfg.model == "glm-test"
    assert "neuralwatt" in cfg.base_url


def test_generic_env():
    cfg = from_env(env={"SWARM_LLM_API_KEY": "sk-x", "SWARM_LLM_BASE_URL": "http://localhost:9/v1"})
    assert cfg.armed and cfg.provider == "openai-compatible"


def test_chat_against_mock_server():
    server = HTTPServer(("127.0.0.1", 0), _MockLlmHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cfg = LlmConfig(
            api_key="sk-test-1234567890",
            base_url=f"http://127.0.0.1:{port}/v1",
            model="test-model",
        )
        out = LlmClient(cfg, timeout=5.0).chat([{"role": "user", "content": "write me an adapter"}])
        assert out == "adapter code here"
    finally:
        server.shutdown()
        server.server_close()


def test_chat_failure_returns_none():
    cfg = LlmConfig(api_key="sk-test-1234567890", base_url="http://127.0.0.1:1/v1", model="m")
    assert LlmClient(cfg, timeout=0.5).chat([{"role": "user", "content": "hi"}]) is None


def test_hub_config_endpoint_masks_key():
    import os

    os.environ["SWARM_NEURALWATT_API_KEY"] = "nw-abcdef123456789"
    try:
        hub = Hub(port=0)
        host, port = hub.start_background()
        try:
            import urllib.request

            data = json.loads(urllib.request.urlopen(f"http://{host}:{port}/api/config", timeout=5).read())
            assert data["llm"]["armed"] is True
            assert data["llm"]["provider"] == "neuralwatt"
            assert "abcdef" not in json.dumps(data)
        finally:
            hub.stop()
    finally:
        del os.environ["SWARM_NEURALWATT_API_KEY"]
