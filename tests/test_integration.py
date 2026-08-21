"""End-to-end: full probe on the real machine, then hub+agent round trip."""

import json
import time
import urllib.request

from swarm.agent.daemon import Agent
from swarm.hub.server import Hub
from swarm.probe.orchestrator import full_probe


def test_full_probe_real_machine():
    profile, capability, ctx = full_probe(timeout=30.0)
    assert profile.node_id
    assert profile.hostname
    assert profile.os
    assert capability.max_floor >= 0
    assert "self_probe" in ctx.collectors_ran
    assert profile.cpu is None or profile.cpu.logical_cores
    assert profile.memory is not None
    if profile.os == "linux":
        assert profile.memory.total_bytes is not None


def test_full_probe_respects_timeout():
    started = time.perf_counter()
    full_probe(timeout=8.0)
    elapsed = time.perf_counter() - started
    assert elapsed < 60.0 * 3


def test_agent_registers_with_hub():
    hub = Hub(port=0)
    host, port = hub.start_background()
    try:
        agent = Agent(hub_url=f"http://{host}:{port}", bench=False)
        ok = agent.run_once()
        assert ok
        nodes = json.loads(
            urllib.request.urlopen(f"http://{host}:{port}/api/nodes", timeout=10).read()
        )["nodes"]
        assert any(n["node_id"] == agent.node_id for n in nodes)
        links = json.loads(
            urllib.request.urlopen(f"http://{host}:{port}/api/links", timeout=10).read()
        )["links"]
        assert any(l["src_node"] == agent.node_id for l in links)
    finally:
        hub.stop()


def test_agent_survives_dead_hub():
    agent = Agent(hub_url="http://127.0.0.1:1", bench=False)
    assert agent.run_once() is False
