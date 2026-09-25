"""The brain-to-fleet tools (MCP), and the smaller guarantees: code only on
opted-in workers, op routing for browser nodes, cancel, heat, signed updates,
measured speed steering placement, dedicated reserves, emulated devices,
and the cloud lane as an owner-armed fallback."""

import hashlib
import hmac
import io
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from swarm import mcp
from swarm.agent import welfare
from swarm.agent.daemon import EMULATED_PROFILES, Agent, apply_emulation
from swarm.agent.updater import signature_ok
from swarm.core.identity import canonical_hash
from swarm.hub.inference import GIB, plan_llama, usable_bytes
from swarm.hub.server import Hub
from swarm.probe import runtimes as runtimes_mod


@pytest.fixture()
def quiet_runtimes(monkeypatch):
    monkeypatch.setattr(runtimes_mod, "find_llama_binaries", lambda: {})
    monkeypatch.setenv("SWARM_OLLAMA_URL", "http://127.0.0.1:9")


def _mcp_session(hub_port, messages):
    tools = mcp.SwarmTools(f"http://127.0.0.1:{hub_port}", None)
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    stdout = io.StringIO()
    mcp.serve(tools, stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_mcp_handshake_and_tool_list(quiet_runtimes):
    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()
    try:
        out = _mcp_session(port, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "swarm_status", "arguments": {}}},
        ])
        assert out[0]["result"]["serverInfo"]["name"] == "swarm"
        names = {t["name"] for t in out[1]["result"]["tools"]}
        assert {"swarm_run_python", "swarm_map", "swarm_chat", "swarm_status"} <= names
        assert out[2]["result"]["isError"] is False
    finally:
        hub.stop()


def test_ai_code_runs_only_on_opted_in_code_workers(quiet_runtimes):
    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()
    ordinary = Agent(hub_url=f"http://127.0.0.1:{port}", bench=False, node_id="owners-laptop", ignore_welfare=True)
    try:
        assert ordinary.run_once()
        ordinary.start_worker(poll_seconds=0.2)
        call = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                "params": {"name": "swarm_run_python", "arguments": {"code": "result = 6 * 7", "timeout_s": 30}}}
        refused = _mcp_session(port, [call])[0]["result"]
        assert refused["isError"] and "no code-worker" in refused["content"][0]["text"]

        worker = Agent(hub_url=f"http://127.0.0.1:{port}", bench=False, node_id="old-phone",
                       ignore_welfare=True, code_worker=True)
        assert worker.run_once()
        worker.start_worker(poll_seconds=0.2)
        call["params"]["arguments"]["code"] = "print('hello from the fleet')\nresult = sum(range(10))"
        done = _mcp_session(port, [call])[0]["result"]
        assert not done["isError"], done
        body = json.loads(done["content"][0]["text"])
        assert body["result"] == 45 and "hello from the fleet" in body["stdout"]
        assert body["node"] == "old-phon"

        mapped = _mcp_session(port, [{"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {
            "name": "swarm_map",
            "arguments": {"function_source": "def run(p):\n    return p['item'] * 2", "inputs": [1, 2, 3]}}}])[0]["result"]
        assert [r["result"] for r in json.loads(mapped["content"][0]["text"])] == [2, 4, 6]
        worker.stop_worker()
    finally:
        ordinary.stop_worker()
        hub.stop()


def test_browser_nodes_only_get_ops_they_can_run():
    hub = Hub(host="127.0.0.1", port=0)
    try:
        q = hub.queue
        q.submit_bag("map", [{"adapter_source": "x"}], ["k1"])
        q.submit_bag("primesum", [{"n": 100}], ["k2"])
        q.set_node_device_classes("phone-browser", ["cpu:generic", "op:primesum", "op:hashwork", "op:matmul"])
        got = q.pull("phone-browser", 8, 60, 10)
        assert [t["op"] for t in got] == ["primesum"], "a browser must never receive map work"
        q.set_node_device_classes("python-node", ["cpu:generic", "op:*"])
        assert [t["op"] for t in q.pull("python-node", 8, 60, 10)] == ["map"]
    finally:
        hub.registry.close()


def test_worker_page_is_served():
    hub = Hub(host="127.0.0.1", port=0, secure=True, owner_key="swo_k")
    _, port = hub.start_background()
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/worker", timeout=10).read().decode()
        assert "Start contributing" in page and '"primesum", "hashwork", "matmul"' in page
    finally:
        hub.stop()


def test_cancelled_bag_hands_out_nothing_and_drops_late_results():
    hub = Hub(host="127.0.0.1", port=0)
    try:
        q = hub.queue
        bag = q.submit_bag("primesum", [{"n": 10}, {"n": 20}], ["a", "b"])
        task = q.pull("n1", 1, 60, 10)[0]
        assert q.cancel_bag(bag) == 2
        assert q.pull("n2", 8, 60, 10) == []
        out = q.complete("n1", [{"bag_id": bag, "seq": task["seq"], "idem_key": task["idem_key"], "payload": {}, "ok": True}])
        assert out["dropped"] == 1 and q.bag_status(bag)["status"] == "cancelled"
    finally:
        hub.registry.close()


def test_hot_device_rests(monkeypatch):
    monkeypatch.setenv("SWARM_EMULATE_TEMP_C", "47")
    gate = welfare.welfare_gate(dedicated=True)
    assert not gate["allowed"] and "cools off" in gate["reason"]
    monkeypatch.setenv("SWARM_EMULATE_TEMP_C", "30")
    assert welfare.thermal_state()["battery_c"] == 30.0


def test_updates_must_be_signed_with_the_node_key():
    key = "swn_secret"
    bundle_sha = "ab" * 32
    good = hmac.new(hashlib.sha256(key.encode()).hexdigest().encode(), bundle_sha.encode(), "sha256").hexdigest()
    headers = {"X-Swarm-Node-Key": key}
    assert signature_ok({"sha256": bundle_sha, "sig": good}, headers)
    assert not signature_ok({"sha256": bundle_sha, "sig": "0" * 64}, headers)
    assert not signature_ok({"sha256": bundle_sha}, headers), "a keyed node refuses unsigned offers"
    assert signature_ok({"sha256": bundle_sha}, None), "open lab hubs send no signatures"


def test_measured_speed_picks_the_head():
    model = {"name": "m", "size_bytes": 2 * GIB, "n_layers": 16}
    slow_big = {"node_id": "big", "devices": [{"id": "Vulkan0", "free_bytes": 20 * GIB}], "measured_tps": 3.0}
    fast = {"node_id": "fast", "devices": [{"id": "CUDA0", "free_bytes": 8 * GIB}], "measured_tps": 40.0}
    assert plan_llama(model, [slow_big, fast], [])["head"]["node_id"] == "fast"
    unmeasured = dict(fast, measured_tps=None)
    assert plan_llama(model, [slow_big, unmeasured], [])["head"]["node_id"] == "big"


def test_dedicated_machines_lend_more_of_their_ram():
    assert usable_bytes(3 * GIB, "cpu") == 1 * GIB
    assert usable_bytes(3 * GIB, "cpu", dedicated=True) > 2 * GIB


def test_emulated_devices_say_so(monkeypatch):
    monkeypatch.setenv("SWARM_EMULATE_BATTERY", "")  # restored after: emulation sets it
    payload = {"profile": {"hostname": "box", "memory": {}}, "inference": {"llama": {"llama_server": "x", "devices": []}}}
    apply_emulation("gpu-box", payload)
    assert payload["emulated"] == "gpu-box" and payload["dedicated"]
    assert payload["profile"]["hostname"].endswith("-emu-gpu-box")
    assert payload["inference"]["llama"]["devices"][0]["name"] == "Emulated RTX 3060"
    assert any("not measured" in a["message"] for a in payload["profile"]["anomalies"])
    assert set(EMULATED_PROFILES) >= {"phone", "old-phone", "pi", "gpu-box", "laptop", "server"}
    import os

    assert os.environ["SWARM_EMULATE_BATTERY"] == "100:ac", "welfare judges the emulated device, not the host"


class _FakeFrontier:
    def __init__(self):
        self.calls = []
        owner = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append((self.headers.get("Authorization"), body))
                out = json.dumps({"model": body["model"], "choices": [{"message": {"role": "assistant", "content": "from the cloud"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/v1" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def test_cloud_lane_is_an_owner_armed_fallback(monkeypatch):
    fake = _FakeFrontier()
    monkeypatch.setenv("SWARM_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("SWARM_LLM_BASE_URL", fake.url)
    monkeypatch.setenv("SWARM_LLM_MODEL", "cloud-model")
    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()

    def chat():
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps({"model": "not-in-the-fleet", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        assert chat()[0] == 404, "armed but not enabled: the cloud is never a silent substitute"
        hub.brain.set_enabled(True)
        code, body = chat()
        assert code == 200 and body["choices"][0]["message"]["content"] == "from the cloud"
        assert body["swarm"]["path"] == "frontier" and fake.calls[0][0] == "Bearer sk-test"
        assert fake.calls[0][1]["model"] == "cloud-model"
        hub.brain.set_kill_switch(True)
        assert chat()[0] == 404, "the kill switch severs it instantly"
    finally:
        hub.stop()
        fake.httpd.shutdown()


def test_bag_priority_is_capped_below_interactive_chat():
    hub = Hub(host="127.0.0.1", port=0)
    try:
        bag = hub.handle_submit({"op": "primesum", "params_list": [{"n": 1}], "priority": 99})
        assert hub.queue.bag_status(bag)["priority"] == 9
        assert canonical_hash({"x": 1})
    finally:
        hub.registry.close()
