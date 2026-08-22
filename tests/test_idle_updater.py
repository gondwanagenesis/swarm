"""Idle self-honing + self-update channel."""

import json
import urllib.request

from swarm.agent import updater as updater_mod
from swarm.agent.idle import IdleHone
from swarm.hub.server import Hub


class _FakeAgent:
    def __init__(self):
        self.posts = []

    def _post(self, path, payload, timeout=10.0):
        self.posts.append((path, payload))
        return {"ok": True}


def test_idle_ladder_runs_pilot_and_posts(monkeypatch):
    monkeypatch.setattr(
        "swarm.agent.welfare.welfare_gate",
        lambda: {"allowed": True, "reason": "ok", "details": {}},
    )
    hone = IdleHone("n1")
    agent = _FakeAgent()
    out = hone.sharpen(agent)
    assert out["ran"] == "pilot"
    assert any(p == "/api/sharpen" for p, _ in agent.posts)
    payload = dict(agent.posts[0][1])
    assert payload["node_id"] == "n1"
    assert payload["pilot"].get("score_gflops")


def test_idle_respects_welfare_denial(monkeypatch):
    monkeypatch.setattr(
        "swarm.agent.welfare.welfare_gate",
        lambda: {"allowed": False, "reason": "human typing", "details": {}},
    )
    hone = IdleHone("n1")
    agent = _FakeAgent()
    out = hone.sharpen(agent)
    assert out["ran"] is None and "human typing" in out["reason"]
    assert agent.posts == []


def test_sharpen_endpoint_folds_into_hub():
    hub = Hub(port=0)
    host, port = hub.start_background()
    try:
        payload = {"node_id": "sharped", "pilot": {"score_gflops": 0.05}, "capability": {}}
        req = urllib.request.Request(
            f"http://{host}:{port}/api/sharpen",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert json.loads(urllib.request.urlopen(req, timeout=5).read())["ok"]
        benches = hub.registry.latest_benches("sharped")
        assert any(b["name"] == "cpu_fp32_gflops" for b in benches)
    finally:
        hub.stop()


def test_updater_not_bundled():
    assert updater_mod.apply_update("http://127.0.0.1:1") in ("not-bundled", "no-offer")


def test_updater_offer_and_download_verify():
    hub = Hub(port=0)
    from swarm.hub.agentbundle import build_agent_pyz

    hub.set_agent_payload(build_agent_pyz())
    host, port = hub.start_background()
    base = f"http://{host}:{port}"
    try:
        offer = updater_mod.check_for_update(base)
        assert offer and offer["ok"] and len(offer["sha256"]) == 64
        blob = updater_mod.download_and_verify(base, offer["sha256"])
        assert blob is not None and blob == hub.agent_payload
        assert updater_mod.download_and_verify(base, "0" * 64) is None
    finally:
        hub.stop()
