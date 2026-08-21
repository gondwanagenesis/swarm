"""M3: hedging, earned tiers, suspension rail."""

import time

from swarm.hub.queue import WorkQueue
from swarm.hub.registry import Registry


def _q():
    reg = Registry(":memory:")
    return WorkQueue(reg._conn, lock=reg._lock)


def _submit(q, n=8):
    params = [{"n": 100 + i} for i in range(n)]
    idem = [f"m3-{i}" for i in range(n)]
    return q.submit_bag("primesum", params, idem)


def test_tier_starts_opportunistic_earns_up():
    q = _q()
    assert q.node_tier("new-node") == "opportunistic"
    bag = _submit(q, n=60)
    for _i in range(60):
        items = q.pull("core-node", 1, lease_seconds=60, predicted_ms_per_item=10)
        if not items:
            break
        for t in items:
            q.complete(
                "core-node",
                [
                    {
                        "bag_id": bag,
                        "seq": t["seq"],
                        "idem_key": t["idem_key"],
                        "payload": {"c": 1},
                        "duration_s": 0.010,
                    }
                ],
            )
    assert q.node_tier("core-node") == "core"
    assert q.node_stats("core-node")["completions"] == 60


def test_expiry_counts_failure_and_third_suspends():
    q = _q()
    _submit(q, n=4)
    for _round_i in range(3):
        q.pull("flaky", 1, lease_seconds=0.001, predicted_ms_per_item=1)
        q.sweep_expired(now=time.time() + 60.0)
    stats = q.node_stats("flaky")
    assert stats["failures"] >= 3
    assert stats["suspended"] == 1
    assert q.node_tier("flaky") == "suspended"


def test_completion_resets_failure_streak():
    q = _q()
    bag = _submit(q, n=4)
    q.note_failure("mixed")
    q.note_failure("mixed")
    items = q.pull("mixed", 1, lease_seconds=60, predicted_ms_per_item=10)
    q.complete(
        "mixed",
        [
            {
                "bag_id": bag,
                "seq": items[0]["seq"],
                "idem_key": items[0]["idem_key"],
                "payload": {},
                "duration_s": 0.01,
            }
        ],
    )
    stats = q.node_stats("mixed")
    assert stats["failures"] == 0


def test_hedge_fires_on_straggler_and_dedupes():
    q = _q()
    bag = _submit(q, n=8)
    lead = q.pull("fast", 6, lease_seconds=60, predicted_ms_per_item=10)
    for t in lead:
        q.complete(
            "fast",
            [
                {
                    "bag_id": bag,
                    "seq": t["seq"],
                    "idem_key": t["idem_key"],
                    "payload": {"c": t["seq"]},
                    "duration_s": 0.001,
                }
            ],
        )
    straggler = q.pull("slow", 2, lease_seconds=600, predicted_ms_per_item=10)
    assert len(straggler) == 2
    q.conn.execute(
        "UPDATE tasks SET lease_started_at=? WHERE status='leased' AND leased_to='slow'",
        (time.time() - 100.0,),
    )
    q.conn.commit()
    hedges = q.pull_hedges("hedger")
    assert hedges, "expected a hedge to be offered"
    assert all(h.get("hedge") for h in hedges)
    h = hedges[0]
    first = q.complete(
        "slow",
        [
            {
                "bag_id": bag,
                "seq": h["seq"],
                "idem_key": h["idem_key"],
                "payload": {"c": h["seq"]},
                "duration_s": 5.0,
            }
        ],
    )
    second = q.complete(
        "hedger",
        [
            {
                "bag_id": bag,
                "seq": h["seq"],
                "idem_key": h["idem_key"],
                "payload": {"c": h["seq"]},
                "duration_s": 0.5,
            }
        ],
    )
    assert first["accepted"] == 1
    assert second["duplicates"] == 1
    assert len(q.results_for_bag(bag)) == q.bag_status(bag)["done"]


def test_hedge_needs_75pct_claimed_and_elapsed():
    q = _q()
    _submit(q, n=8)
    q.pull("a", 2, lease_seconds=600, predicted_ms_per_item=10)
    q.conn.execute("UPDATE tasks SET lease_started_at=? WHERE status='leased'", (time.time() - 1e6,))
    q.conn.commit()
    assert q.pull_hedges("b") == [], "hedge must not fire before 75% claimed"
