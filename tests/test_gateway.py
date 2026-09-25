"""End to end through the OpenAI-compatible front door.

Task path: a real hub + a real agent worker + a fake Ollama — chat and
embeddings routed by ``model:<name>`` to the node that holds the model,
retried when the runtime hiccups, 404 (never a substitute) when nobody can
serve the name.

Pooled path: a real hub + TWO real agents + fake llama.cpp binaries — the
planner's split reaches ``llama-server --rpc ... --tensor-split ...`` on the
head, the helper runs ``rpc-server``, and the gateway proxies (streaming
too). Only the binaries are fakes; the scheduling, service reconcile loop
and proxy are the production code.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from swarm.agent import daemon as daemon_mod
from swarm.agent import services as services_mod
from swarm.agent.daemon import Agent
from swarm.hub import inference as inference_mod
from swarm.hub.server import Hub
from swarm.probe import runtimes as runtimes_mod
from tests.fake_ollama import FakeOllama

FAKE_LLAMA = str(Path(__file__).parent / "fixtures" / "fake_llama.py")


def _post(port, path, payload, timeout=60, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture()
def ollama_fleet(monkeypatch):
    fake = FakeOllama(fail_chat=1)
    monkeypatch.setenv("SWARM_OLLAMA_URL", fake.url)
    monkeypatch.setattr(runtimes_mod, "find_llama_binaries", lambda: {})
    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()
    agent = Agent(hub_url=f"http://127.0.0.1:{port}", bench=False, ignore_welfare=True)
    assert agent.run_once()
    agent.start_worker(poll_seconds=0.2)
    yield hub, port, fake
    agent.stop_worker()
    hub.stop()
    fake.stop()


def test_models_lists_what_nodes_hold(ollama_fleet):
    _, port, _ = ollama_fleet
    body = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=10).read())
    ids = {m["id"]: m["swarm"]["kind"] for m in body["data"]}
    assert ids == {"tiny-chat:latest": "chat", "tiny-embed:latest": "embed"}


def test_chat_routes_to_the_holder_and_survives_a_runtime_hiccup(ollama_fleet):
    _, port, fake = ollama_fleet
    code, raw = _post(port, "/v1/chat/completions", {"model": "tiny-chat", "messages": [{"role": "user", "content": "hi"}]})
    assert code == 200, raw
    body = json.loads(raw)
    assert body["choices"][0]["message"]["content"] == "echo: hi"
    assert body["swarm"]["path"] == "task" and body["swarm"]["node_id"]
    chats = [c for c in fake.calls if c[0] == "/v1/chat/completions"]
    assert len(chats) == 2, "first attempt failed (503), the retry answered"


def test_chat_stream_request_gets_valid_sse(ollama_fleet):
    _, port, _ = ollama_fleet
    code, raw = _post(
        port, "/v1/chat/completions",
        {"model": "tiny-chat:latest", "stream": True, "messages": [{"role": "user", "content": "yo"}]},
    )
    assert code == 200
    lines = [ln for ln in raw.decode().splitlines() if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    assert json.loads(lines[0][6:])["choices"][0]["delta"]["content"] == "echo: yo"


def test_embeddings_fan_out_and_keep_order(ollama_fleet):
    _, port, _ = ollama_fleet
    code, raw = _post(port, "/v1/embeddings", {"model": "tiny-embed", "input": ["a", "b", "a"]})
    assert code == 200, raw
    data = json.loads(raw)["data"]
    assert [d["index"] for d in data] == [0, 1, 2]
    assert data[0]["embedding"] == data[2]["embedding"] != data[1]["embedding"]


def test_unknown_model_is_404_never_a_substitute(ollama_fleet):
    _, port, _ = ollama_fleet
    code, raw = _post(port, "/v1/chat/completions", {"model": "gpt-9", "messages": [{"role": "user", "content": "x"}]})
    assert code == 404
    assert "tiny-chat:latest" in json.loads(raw)["error"]["message"]


def test_v1_needs_the_owner_key_on_a_secure_hub(monkeypatch):
    monkeypatch.setattr(runtimes_mod, "find_llama_binaries", lambda: {})
    hub = Hub(host="127.0.0.1", port=0, secure=True, owner_key="swo_k")
    _, port = hub.start_background()
    try:
        code, raw = _post(port, "/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "x"}]})
        assert code == 401 and json.loads(raw)["error"]["type"] == "unauthorized"
        code, _ = _post(
            port, "/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": "Bearer swo_k"},
        )
        assert code == 404  # authorized; simply no such model
    finally:
        hub.stop()


# ---------------------------------------------------------------- pooled path


def _free_port():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_pooled_model_end_to_end(monkeypatch, tmp_path):
    (tmp_path / "tiny.gguf").write_bytes(b"GGUF")
    monkeypatch.setenv("SWARM_MODELS_DIR", str(tmp_path))
    monkeypatch.setenv("SWARM_OLLAMA_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(daemon_mod, "_local_addresses", lambda: [])
    monkeypatch.setattr(inference_mod, "RPC_BASE_PORT", _free_port())
    monkeypatch.setattr(inference_mod, "SERVER_BASE_PORT", _free_port())

    real_build = services_mod.build_command

    def fake_build(spec, binaries):
        real = real_build(spec, {"llama_rpc": "x", "llama_server": "x"})
        mode = "rpc" if spec["kind"] == "llama_rpc" else "server"
        return [sys.executable, FAKE_LLAMA, mode, *real[1:]]

    monkeypatch.setattr(services_mod, "build_command", fake_build)

    head_data = {
        "runtimes": ["llama_server", "llama_rpc"],
        "models": [{"name": "tiny", "kind": "chat", "runtime": "llama_cpp", "size_bytes": 64 * 1024 * 1024, "n_layers": 8}],
        "llama": {"llama_server": "x", "llama_rpc": "x", "devices": [{"id": "Vulkan0", "free_bytes": 8 * 1024**3}]},
    }
    helper_data = {"runtimes": ["llama_rpc"], "models": [], "llama": {"llama_rpc": "x"}}
    current = {"data": head_data}
    monkeypatch.setattr(runtimes_mod, "discover_inference", lambda list_devices=True: (current["data"], []))

    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()
    head = helper = None
    try:
        head = Agent(hub_url=f"http://127.0.0.1:{port}", bench=False, node_id="head-node",
                     ignore_welfare=True, services=True)
        assert head.run_once()
        current["data"] = helper_data
        helper = Agent(hub_url=f"http://127.0.0.1:{port}", bench=False, node_id="helper-node",
                       ignore_welfare=True, services=True)
        assert helper.run_once()

        # It fits on the head alone, so the planner refuses to shard...
        plan = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/models/plan?model=tiny", timeout=10).read())
        assert plan["feasible"] and plan["mode"] == "single"
        # ...unless the owner forces a split to prove the pooled path.
        code, raw = _post(port, "/api/models/deploy", {"model": "tiny", "force_shard": True, "wait_s": 45}, timeout=90)
        dep = json.loads(raw)["deployment"]
        assert dep["state"] == "ready", dep
        assert dep["plan"]["mode"] == "pooled"
        assert [p["node_id"] for p in dep["plan"]["participants"]] == ["head-node", "helper-node"]

        code, raw = _post(port, "/v1/chat/completions", {"model": "tiny", "messages": [{"role": "user", "content": "hello"}]})
        assert code == 200, raw
        body = json.loads(raw)
        assert body["choices"][0]["message"]["content"] == "fake-llama heard: hello"
        rpc_port = next(s for s in dep["services"] if s["kind"] == "llama_rpc")["spec"]["port"]
        assert body["launched"]["rpc"] == f"127.0.0.1:{rpc_port}"
        assert body["launched"]["tensor_split"] == ",".join(str(x) for x in dep["plan"]["tensor_split"])
        assert body["swarm"]["mode"] == "pooled"

        code, raw = _post(port, "/v1/chat/completions",
                          {"model": "tiny", "stream": True, "messages": [{"role": "user", "content": "s"}]})
        text = "".join(
            json.loads(ln[6:])["choices"][0]["delta"].get("content", "")
            for ln in raw.decode().splitlines()
            if ln.startswith("data: {")
        )
        assert text == "fake-llama heard: s"

        assert json.loads(_post(port, "/api/models/undeploy", {"model": "tiny"})[1])["ok"]
        deadline = time.time() + 20
        while time.time() < deadline and (head.service_manager.status() or helper.service_manager.status()):
            time.sleep(0.3)
        assert head.service_manager.status() == [] and helper.service_manager.status() == []
    finally:
        for a in (head, helper):
            if a is not None:
                a.stop_worker()
                if a.service_manager is not None:
                    a.service_manager.stop_all()
        hub.stop()
