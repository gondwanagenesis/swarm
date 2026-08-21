"""Brain policy: abliterated-local default, frontier always killable,
every route logged, unarmed lanes never selected."""

import json
import urllib.request

from swarm.hub.registry import Registry
from swarm.hub.server import Hub
from swarm.integrator.llm import LlmConfig
from swarm.integrator.policy import (
    LANE_ABLITERATED,
    LANE_FRONTIER,
    BrainRouter,
    local_brain_config_from_env,
)


def _armed(name="neural"):
    return LlmConfig(api_key="sk-test-12345678", base_url="http://127.0.0.1:9/v1", model=name)


def _router(reg=None, **kw):
    return BrainRouter(reg or Registry(":memory:"), **kw)


def test_default_is_abliterated_local_when_armed():
    brain = _router(frontier=_armed(), local=_armed("huihui"))
    assert brain.route(0.99)["lane"] == LANE_ABLITERATED, "frontier must be OFF by default"


def test_frontier_used_only_when_enabled_and_hard_task():
    brain = _router(frontier=_armed(), local=_armed("huihui"))
    brain.set_enabled(True)
    brain.set_sensitivity(0.5)
    assert brain.route(0.3)["lane"] == LANE_ABLITERATED
    assert brain.route(0.9)["lane"] == LANE_FRONTIER


def test_kill_switch_overrides_everything():
    brain = _router(frontier=_armed(), local=_armed("huihui"))
    brain.set_enabled(True)
    brain.set_kill_switch(True)
    assert brain.route(1.0)["lane"] == LANE_ABLITERATED


def test_unarmed_frontier_can_never_be_chosen():
    brain = _router(frontier=LlmConfig(), local=_armed("huihui"))
    brain.set_enabled(True)
    brain.set_sensitivity(0.0)
    assert brain.route(1.0)["lane"] == LANE_ABLITERATED


def test_state_persists_in_registry():
    reg = Registry(":memory:")
    brain = _router(reg=reg, frontier=_armed())
    brain.set_enabled(True)
    brain.set_sensitivity(0.42)
    brain2 = BrainRouter(reg, frontier=_armed())
    assert brain2.enabled is True
    assert abs(brain2.sensitivity - 0.42) < 1e-9


def test_every_route_is_logged():
    reg = Registry(":memory:")
    brain = _router(reg=reg, frontier=_armed(), local=_armed())
    brain.route(0.1)
    brain.route(0.95)
    lanes = [r["lane_name"] for r in reg._conn.execute("SELECT lane_name FROM brain_routes").fetchall()]
    assert lanes == ["abliterated-local", "abliterated-local"]


def test_local_config_only_when_env_set():
    assert local_brain_config_from_env(env={}) is None
    cfg = local_brain_config_from_env(env={"SWARM_LOCAL_BRAIN_URL": "http://127.0.0.1:11434/v1"})
    assert cfg is not None and "abliterated" in cfg.provider
    assert "Huihui" in cfg.model


def test_hub_brain_admin_endpoints():
    hub = Hub(port=0)
    host, port = hub.start_background()
    try:
        status = json.loads(urllib.request.urlopen(f"http://{host}:{port}/api/brain", timeout=5).read())
        assert status["enabled"] is False and status["kill_switch"] is False

        req = urllib.request.Request(
            f"http://{host}:{port}/api/brain/admin",
            data=json.dumps({"action": "enable"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
        assert resp["ok"] and resp["enabled"] is True

        req = urllib.request.Request(
            f"http://{host}:{port}/api/brain/admin",
            data=json.dumps({"action": "kill_on"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
        assert resp["kill_switch"] is True

        req = urllib.request.Request(
            f"http://{host}:{port}/api/brain/admin",
            data=json.dumps({"action": "sensitivity", "value": 0.25}).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
        assert abs(resp["sensitivity"] - 0.25) < 1e-9
    finally:
        hub.stop()
