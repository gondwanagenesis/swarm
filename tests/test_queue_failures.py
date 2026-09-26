"""Failures are first-class; interactive work jumps the queue; idle nodes
long-poll instead of napping; leases really renew over HTTP."""

import json
import threading
import time
import urllib.request

from swarm.core.identity import canonical_hash
from swarm.hub.queue import MAX_TASK_ATTEMPTS, WorkQueue
from swarm.hub.registry import Registry
from swarm.hub.server import Hub


def _queue():
    reg = Registry(":memory:")
    return reg, WorkQueue(reg._conn, lock=reg._lock)


def _submit(q, op="primesum", n=1, **kw):
    params = [{"n": 100 + i} for i in range(n)]
    return q.submit_bag(op, params, [canonical_hash({"op": op, "p": p, "kw": str(kw)}) for p in params], **kw)


def _result(task, ok=True, payload=None):
    return {
        "bag_id": task["bag_id"],
        "seq": task["seq"],
        "idem_key": task["idem_key"],
        "payload": payload if payload is not None else {"v": 1},
        "duration_s": 0.01,
        "ok": ok,
    }


def test_failed_op_is_retried_not_recorded_as_done():
    _, q = _queue()
    bag = _submit(q)
    task = q.pull("n1", 1, 60, 10)[0]
    out = q.complete("n1", [_result(task, ok=False, payload={"error": "ollama restarting"})])
    assert out["requeued"] == 1 and out["accepted"] == 0
    status = q.bag_status(bag)
    assert status["status"] == "open" and status["queued"] == 1 and status["done"] == 0
    # a healthy retry then completes it normally
    task = q.pull("n2", 1, 60, 10)[0]
    q.complete("n2", [_result(task)])
    assert q.bag_status(bag)["status"] == "closed"
    assert q.results_for_bag(bag)[0]["status"] == "done"


def test_task_closes_as_failed_after_attempt_budget_with_error_kept():
    _, q = _queue()
    bag = _submit(q)
    for _ in range(MAX_TASK_ATTEMPTS):
        task = q.pull("n1", 1, 60, 10)[0]
        q.complete("n1", [_result(task, ok=False, payload={"error": "no such model"})])
    status = q.bag_status(bag)
    assert status["status"] == "closed" and status["failed"] == 1
    rows = q.results_for_bag(bag)
    assert rows[0]["status"] == "failed"
    assert json.loads(rows[0]["payload_json"])["error"] == "no such model"


def test_results_without_ok_flag_still_count_as_success():
    _, q = _queue()
    bag = _submit(q)
    task = q.pull("n1", 1, 60, 10)[0]
    legacy = _result(task)
    del legacy["ok"]
    q.complete("n1", [legacy])
    assert q.bag_status(bag)["status"] == "closed"


def test_priority_bag_is_served_first_and_chat_travels_alone():
    _, q = _queue()
    _submit(q, n=20)
    chat = _submit(q, op="chat", n=3, priority=10)
    got = q.pull("n1", 8, 60, 10)
    assert [t["bag_id"] for t in got] == [chat], "one interactive request per lease"
    got2 = q.pull("n2", 8, 60, 10)
    assert len(got2) == 1 and got2[0]["bag_id"] == chat


def test_long_poll_wakes_when_work_arrives():
    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()
    try:
        box = {}

        def puller():
            t0 = time.time()
            data = json.dumps({"node_id": "waiter", "wait_s": 10}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/tasks/pull", data=data, headers={"Content-Type": "application/json"}
            )
            box["resp"] = json.loads(urllib.request.urlopen(req, timeout=20).read())
            box["waited"] = time.time() - t0

        th = threading.Thread(target=puller)
        th.start()
        time.sleep(0.5)
        _submit(hub.queue)
        th.join(15)
        assert box["resp"]["tasks"], "long-poll returned without the new work"
        assert box["waited"] < 5, f"woke late: {box['waited']:.1f}s"
    finally:
        hub.stop()


def test_lease_renewal_route_exists_and_extends():
    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()
    try:
        _submit(hub.queue)
        task = hub.queue.pull("n1", 1, 10, 10)[0]
        data = json.dumps(
            {"node_id": "n1", "bag_id": task["bag_id"], "seqs": [task["seq"]], "lease_seconds": 600}
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/tasks/renew", data=data, headers={"Content-Type": "application/json"}
        )
        body = json.loads(urllib.request.urlopen(req, timeout=10).read())
        assert body["ok"] and body["renewed"] == 1
        row = hub.registry._conn.execute(
            "SELECT lease_expires_at FROM tasks WHERE bag_id=? AND seq=?", (task["bag_id"], task["seq"])
        ).fetchone()
        assert row["lease_expires_at"] > time.time() + 500
    finally:
        hub.stop()


def test_results_are_readable_over_http():
    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()
    try:
        bag = _submit(hub.queue)
        task = hub.queue.pull("n1", 1, 60, 10)[0]
        hub.queue.complete("n1", [_result(task, payload={"answer": 42})])
        body = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/bag/{bag}/results", timeout=10).read())
        assert body["status"] == "closed"
        assert body["results"][0]["payload"] == {"answer": 42} and body["results"][0]["ok"]
    finally:
        hub.stop()


def test_a_node_hosting_model_layers_takes_no_batch_work():
    hub = Hub(host="127.0.0.1", port=0)
    try:
        _submit(hub.queue, n=3)
        spec = json.dumps({"kind": "llama_rpc", "port": 50052})
        with hub.registry._lock:
            hub.registry._conn.execute(
                "INSERT INTO services (service_id, deployment, node_id, kind, spec_json, desired, created_at, updated_at)"
                " VALUES ('svc-x', 'm', 'thinker', 'llama_rpc', ?, 1, 0, 0)",
                (spec,),
            )
        busy = hub.handle_pull("thinker")
        assert busy["tasks"] == [] and busy["busy_serving"]
        assert hub.handle_pull("phone")["tasks"], "a free node still gets the work"
    finally:
        hub.registry.close()
