"""Holographic hub: any part can recreate the whole.

Unit level: identity, successor ranking, replica restore (keys and owner key
survive as hashes), stepping aside for a newer epoch.

End to end: a real hub with real agents; the hub dies; the best-ranked
successor promotes itself from its replica into a NEW hub process at a higher
epoch; the other node follows it; work submitted afterwards completes.
"""

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from swarm.agent import daemon as daemon_mod
from swarm.agent import holo as agent_holo
from swarm.agent.daemon import Agent
from swarm.hub.holo import restore_replica
from swarm.hub.server import Hub
from swarm.probe import runtimes as runtimes_mod

OWNER = "swo_holo-owner"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _call(port, path, payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers={"Content-Type": "application/json", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _register(port, node_id, token=None, **extra):
    body = {"profile": {"node_id": node_id, "hostname": node_id, "os": "linux", "arch": "x86_64"},
            "capability": {}, "benchmarks": [], "addresses": ["100.64.0.%d" % (len(node_id) % 200)], **extra}
    if token:
        body["token"] = token
    return _call(port, "/api/register", body)


def test_identity_is_minted_once_and_replicates(tmp_path):
    hub = Hub(host="127.0.0.1", port=0, db_path=str(tmp_path / "a.db"), secure=True, owner_key=OWNER)
    swarm_id, epoch = hub.holo.swarm_id, hub.holo.epoch
    assert swarm_id.startswith("swm_") and epoch == 1
    token = hub.enrollment.create()["token"]
    _, port = hub.start_background()
    try:
        _, body = _register(port, "n1", token, can_hub=True)
        key = body["node_key"]
        rep = hub.holo.replica(max_age_s=0)
    finally:
        hub.stop()
    restored = tmp_path / "b.db"
    restore_replica(rep["bytes"], restored, epoch + 1)
    b = Hub(host="127.0.0.1", port=0, db_path=str(restored), secure=True)
    _, port = b.start_background()
    try:
        assert b.holo.swarm_id == swarm_id and b.holo.epoch == epoch + 1
        assert b.auth.owner_key is None, "a restored hub never holds the owner key itself"
        # the owner's key still works (verified against its hash)...
        assert _call(port, "/api/nodes", headers={"Authorization": f"Bearer {OWNER}"})[0] == 200
        assert _call(port, "/api/nodes", headers={"Authorization": "Bearer nope"})[0] == 401
        # ...and so does the node's key: it re-attaches without a new token
        hdr = {"X-Swarm-Node": "n1", "X-Swarm-Node-Key": key}
        assert _call(port, "/api/heartbeat", {"node_id": "n1"}, hdr)[0] == 200
    finally:
        b.stop()


def test_successors_rank_dedicated_first_and_need_the_hub_code():
    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()
    try:
        _register(port, "laptop", can_hub=True, hub_port=9001)
        _register(port, "closet-box", can_hub=True, dedicated=True, hub_port=9002)
        _register(port, "browser", can_hub=False)
        succ = hub.holo.successors()
        assert [s["node_id"] for s in succ] == ["closet-box", "laptop"]
        assert succ[0]["url"].endswith(":9002") and succ[0]["rank"] == 0
        # every heartbeat tells nodes who would take over
        _, hb = _call(port, "/api/heartbeat", {"node_id": "laptop"})
        assert [s["node_id"] for s in hb["holo"]["successors"]] == ["closet-box", "laptop"]
    finally:
        hub.stop()


def test_old_hub_steps_aside_for_a_newer_epoch(tmp_path):
    new = Hub(host="127.0.0.1", port=0, db_path=str(tmp_path / "new.db"))
    old = Hub(host="127.0.0.1", port=0, db_path=str(tmp_path / "old.db"))
    new.holo.settings.set("swarm_id", old.holo.swarm_id)
    new.holo.set_epoch(old.holo.epoch + 1)
    _, new_port = new.start_background()
    _, old_port = old.start_background()
    try:
        old.holo.settings.set("last_successors", json.dumps([{"url": f"http://127.0.0.1:{new_port}", "rank": 0}]))
        moved = old.holo.check_peers_once()
        assert moved and moved["moved_to"] == f"http://127.0.0.1:{new_port}"
        code, body = _call(old_port, "/api/tasks/pull", {"node_id": "x"})
        assert code == 409 and body["moved_to"] == f"http://127.0.0.1:{new_port}"
        assert _call(old_port, "/api/hubinfo")[1]["demoted"]
    finally:
        new.stop()
        old.stop()


def test_hub_failover_end_to_end(tmp_path, monkeypatch):
    """Kill the hub; a successor becomes the hub; the fleet follows; work flows."""
    monkeypatch.setattr(runtimes_mod, "find_llama_binaries", lambda: {})
    monkeypatch.setenv("SWARM_OLLAMA_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SWARM_FAILOVER_S", "2")
    monkeypatch.setattr(agent_holo, "RANK_GRACE_S", 3.0)
    monkeypatch.setattr(agent_holo, "REPLICA_REFRESH_S", 0.0)
    monkeypatch.setattr(agent_holo, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(daemon_mod, "HEARTBEAT_SECONDS", 0.4)
    monkeypatch.setattr(daemon_mod, "_local_addresses", lambda: [])

    hub = Hub(host="127.0.0.1", port=0, db_path=str(tmp_path / "hub-a.db"))
    _, port = hub.start_background()
    ports = {"a": _free_port(), "b": _free_port()}
    agents = {
        "a": Agent(hub_url=f"http://127.0.0.1:{port}", bench=False, node_id="node-a", ignore_welfare=True,
                   dedicated=True, hub_port=ports["a"]),
        "b": Agent(hub_url=f"http://127.0.0.1:{port}", bench=False, node_id="node-b", ignore_welfare=True,
                   hub_port=ports["b"]),
    }
    for a in agents.values():
        assert a.run_once()
    beats = [threading.Thread(target=a.heartbeat_forever, daemon=True) for a in agents.values()]
    for t in beats:
        t.start()
    promoted = None
    try:
        # node-a (dedicated) is successor #0 and pulls the replica
        deadline = time.time() + 30
        while time.time() < deadline and not (agents["a"].holo and agents["a"].holo.replica_path.exists()):
            time.sleep(0.2)
        assert agents["a"].holo.replica_path.exists(), "successor never received a replica"
        assert agents["a"].holo.my_rank() == 0 and agents["b"].holo.my_rank() == 1
        swarm_id = hub.holo.swarm_id

        hub.stop()  # the hub machine dies

        new_url = f"http://127.0.0.1:{ports['a']}"
        deadline = time.time() + 60
        info = None
        while time.time() < deadline:
            info = agent_holo.hub_info(new_url, timeout=1.0)
            if info and agents["b"].hub_port == ports["a"] and agents["a"].hub_port == ports["a"]:
                break
            time.sleep(0.3)
        assert info and info["swarm_id"] == swarm_id and info["epoch"] == 2, info
        assert agents["b"].hub_port == ports["a"], "the other node did not follow the new hub"
        promoted = agents["a"]._hub_proc

        # the new hub knows the fleet and runs work
        for a in agents.values():
            a.start_worker(poll_seconds=0.2)
        code, sub = _call(ports["a"], "/api/bag/submit", {"op": "primesum", "params_list": [{"n": 1000 + i} for i in range(6)]})
        assert code == 200
        deadline = time.time() + 30
        status = {}
        while time.time() < deadline:
            _, status = _call(ports["a"], f"/api/bag/{sub['bag_id']}")
            if status.get("status") == "closed":
                break
            time.sleep(0.3)
        assert status.get("status") == "closed", status
        _, nodes = _call(ports["a"], "/api/nodes")
        assert {n["node_id"] for n in nodes["nodes"]} >= {"node-a", "node-b"}
    finally:
        for a in agents.values():
            a.stop_worker()  # also ends the heartbeat loop, so nobody restarts the hub
        for t in beats:
            t.join(timeout=10)
        proc = agents["a"]._hub_proc or promoted
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=10)


@pytest.mark.parametrize("dead_for,expected", [(0.5, "wait"), (10.0, "promote")])
def test_choose_hub_waits_its_rank_grace(tmp_path, monkeypatch, dead_for, expected):
    monkeypatch.setenv("SWARM_FAILOVER_S", "1")
    monkeypatch.setattr(agent_holo, "RANK_GRACE_S", 2.0)
    st = agent_holo.HoloState("me", state_dir=tmp_path)
    st.data = {"swarm_id": "swm_x", "epoch": 3, "successors": [
        {"rank": 0, "node_id": "dead", "url": "http://127.0.0.1:9"},
        {"rank": 1, "node_id": "me", "url": "http://127.0.0.1:9"},
    ]}
    assert agent_holo.choose_hub(st, dead_for)["action"] == expected
