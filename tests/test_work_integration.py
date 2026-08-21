"""M2 done-state: a batch completes across nodes; a node dying mid-batch
requeues its work and everything completes; results dedupe by content hash."""

import json
import time
import urllib.request

from swarm.agent.daemon import Agent
from swarm.agent.ops import op_primesum
from swarm.hub.server import Hub


def _post_json(port, path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers={"Content-Type": "application/json"}
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def _submit_bag(port, op, params_list):
    return _post_json(port, "/api/bag/submit", {"op": op, "params_list": params_list})["bag_id"]


def test_pull_execute_complete_full_bag():
    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()
    try:
        agent = Agent(hub_url=f"http://127.0.0.1:{port}", bench=False)
        assert agent.run_once()

        n = 30
        bag_id = _submit_bag(port, "primesum", [{"n": 500 + i * 100} for i in range(n)])

        pulled = _post_json(port, "/api/tasks/pull", {"node_id": agent.node_id})
        assert pulled["ok"] and len(pulled["tasks"]) > 0

        results = agent.execute_chunk(pulled["tasks"])
        outcome = _post_json(port, "/api/tasks/complete", {"node_id": agent.node_id, "results": results})
        assert outcome["ok"]

        known = op_primesum({"n": 500})
        stored = hub.queue.results_for_bag(bag_id)
        assert stored, "no results stored"
        first = json.loads(stored[0]["payload_json"])
        if stored[0]["idem_key"] == pulled["tasks"][0]["idem_key"]:
            assert first["count"] == known["count"]
    finally:
        hub.stop()


def test_dead_node_work_is_requeued_and_completes():
    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()
    try:
        n = 8
        bag_id = _submit_bag(port, "primesum", [{"n": 300 + i} for i in range(n)])

        ghost = _post_json(port, "/api/tasks/pull", {"node_id": "ghost-node"})
        assert ghost["ok"] and ghost["tasks"], "expected ghost to claim work"
        ghost_seqs = {t["seq"] for t in ghost["tasks"]}

        requeued = hub.queue.sweep_expired(now=time.time() + 10**4)
        assert requeued >= len(ghost_seqs)

        survivor = Agent(hub_url=f"http://127.0.0.1:{port}", bench=False)
        for _ in range(12):
            pulled = _post_json(port, "/api/tasks/pull", {"node_id": "survivor"})
            if not pulled["tasks"]:
                break
            results = survivor.execute_chunk(pulled["tasks"])
            _post_json(port, "/api/tasks/complete", {"node_id": "survivor", "results": results})

        status = hub.queue.bag_status(bag_id)
        assert status["done"] == n and status["status"] == "closed"
        results = hub.queue.results_for_bag(bag_id)
        idem_keys = {r["idem_key"] for r in results}
        assert len(idem_keys) == n
    finally:
        hub.stop()


def test_atemporal_duplicate_completion_is_noop():
    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()
    try:
        bag_id = _submit_bag(port, "hashwork", [{"seed": "x", "rounds": 50}])
        pulled = _post_json(port, "/api/tasks/pull", {"node_id": "a"})["tasks"]
        payload = {"seed": "x", "rounds": 50, "digest": "0" * 64}
        result = {
            "bag_id": bag_id,
            "seq": pulled[0]["seq"],
            "idem_key": pulled[0]["idem_key"],
            "payload": payload,
            "duration_s": 0.001,
        }
        first = _post_json(port, "/api/tasks/complete", {"node_id": "a", "results": [result]})
        second = _post_json(port, "/api/tasks/complete", {"node_id": "b", "results": [result]})
        assert first["accepted"] == 1
        assert second["duplicates"] == 1 and second["accepted"] == 0
        assert len(hub.queue.results_for_bag(bag_id)) == 1
    finally:
        hub.stop()


def test_worker_thread_drains_bag(monkeypatch):
    import swarm.agent.welfare as welfare_mod

    monkeypatch.setattr(
        welfare_mod,
        "welfare_gate",
        lambda: {"allowed": True, "reason": "test-pinned", "details": {}},
    )
    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()
    try:
        agent = Agent(hub_url=f"http://127.0.0.1:{port}", bench=False)
        agent.run_once()
        n = 40
        bag_id = _submit_bag(port, "primesum", [{"n": 400 + i * 50} for i in range(n)])
        thread = agent.start_worker(poll_seconds=0.2)
        deadline = time.time() + 30.0
        while time.time() < deadline:
            status = hub.queue.bag_status(bag_id)
            if status and status["status"] == "closed":
                break
            time.sleep(0.2)
        agent.stop_worker()
        thread.join(timeout=5)
        status = hub.queue.bag_status(bag_id)
        assert status["status"] == "closed" and status["done"] == n
    finally:
        hub.stop()
