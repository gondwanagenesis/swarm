import time

from swarm.hub.queue import WorkQueue
from swarm.hub.registry import Registry


def _queue():
    reg = Registry(":memory:")
    return reg, WorkQueue(reg._conn)


def _submit(queue, n=10, op="primesum"):
    params = [{"n": 100 + i} for i in range(n)]
    idem = [f"idem-{i}" for i in range(n)]
    return queue.submit_bag(op, params, idem)


def test_submit_and_pull_claims_unique_tasks():
    _, queue = _queue()
    bag = _submit(queue, n=10)
    a = queue.pull("node-a", 4, lease_seconds=60, predicted_ms_per_item=100)
    b = queue.pull("node-b", 4, lease_seconds=60, predicted_ms_per_item=100)
    assert len(a) == 4 and len(b) == 4
    seqs_a = {(t["bag_id"], t["seq"]) for t in a}
    seqs_b = {(t["bag_id"], t["seq"]) for t in b}
    assert seqs_a.isdisjoint(seqs_b)
    status = queue.bag_status(bag)
    assert status["leased"] == 8 and status["queued"] == 2


def test_lease_expiry_requeues():
    _, queue = _queue()
    _submit(queue, n=5)
    pulled = queue.pull("node-a", 3, lease_seconds=5, predicted_ms_per_item=100)
    assert len(pulled) == 3
    requeued = queue.sweep_expired(now=time.time() + 10.0)
    assert requeued == 3
    again = queue.pull("node-b", 3, lease_seconds=60, predicted_ms_per_item=100)
    seqs_a = {t["seq"] for t in pulled}
    seqs_b = {t["seq"] for t in again}
    assert seqs_a == seqs_b


def test_complete_idempotent_and_dedup():
    _, queue = _queue()
    bag = _submit(queue, n=3)
    items = queue.pull("node-a", 3, lease_seconds=60, predicted_ms_per_item=100)
    results = [
        {
            "bag_id": t["bag_id"],
            "seq": t["seq"],
            "idem_key": t["idem_key"],
            "payload": {"count": 42},
            "duration_s": 0.01,
        }
        for t in items
    ]
    outcome = queue.complete("node-a", results)
    assert outcome["accepted"] == 3 and outcome["duplicates"] == 0
    outcome2 = queue.complete("node-a", results)
    assert outcome2["accepted"] == 0 and outcome2["duplicates"] == 3
    assert queue.bag_status(bag)["status"] == "closed"
    assert len(queue.results_for_bag(bag)) == 3


def test_wrong_idem_key_dropped():
    _, queue = _queue()
    _submit(queue, n=2)
    items = queue.pull("node-a", 2, lease_seconds=60, predicted_ms_per_item=100)
    bad = dict(items[0])
    bad["idem_key"] = "forged"
    outcome = queue.complete(
        "node-a",
        [
            {
                "bag_id": bad["bag_id"],
                "seq": bad["seq"],
                "idem_key": bad["idem_key"],
                "payload": {},
                "duration_s": 0.1,
            }
        ],
    )
    assert outcome["dropped"] == 1


def test_renew_extends_lease():
    _, queue = _queue()
    _submit(queue, n=2)
    items = queue.pull("node-a", 2, lease_seconds=10, predicted_ms_per_item=100)
    seqs = [t["seq"] for t in items]
    renewed = queue.renew("node-a", items[0]["bag_id"], seqs, 120.0)
    assert renewed == 2
    assert queue.sweep_expired(now=time.time() + 30.0) == 0


def test_node_stats_ewma_tracks_durations():
    _, queue = _queue()
    _submit(queue, n=4)
    for _ in range(4):
        items = queue.pull("node-a", 1, lease_seconds=60, predicted_ms_per_item=100)
        queue.complete(
            "node-a",
            [
                {
                    "bag_id": items[0]["bag_id"],
                    "seq": items[0]["seq"],
                    "idem_key": items[0]["idem_key"],
                    "payload": {"x": 1},
                    "duration_s": 2.0,
                }
            ],
        )
    stats = queue.node_stats("node-a")
    assert stats["samples"] == 4
    assert stats["ewma_ms_per_item"] > 1500


def test_bag_status_unknown_is_none():
    _, queue = _queue()
    assert queue.bag_status("bag-nope") is None
