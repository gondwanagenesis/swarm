"""device_class routing: work that needs particular silicon only goes to
nodes known to serve it, work that doesn't goes anywhere (as it always has),
and work nobody can serve is surfaced instead of hanging in the queue."""

import importlib
import sqlite3
import time

import pytest

from swarm.agent.ops import (
    TIER_NUMPY_BLAS,
    TIER_PYTHON,
    _gen_matrix_numpy,
    _gen_matrix_python,
    matmul_tiers_available,
    op_matmul,
)
from swarm.hub.queue import SCHEMA_VERSION, WorkQueue
from swarm.hub.registry import Registry

CUDA = "nvidia:geforce_rtx_4090"
OPENCL = "amd:radeon_rx_6800"


def _queue():
    reg = Registry(":memory:")
    return reg, WorkQueue(reg._conn)


def _submit(queue, n=6, op="matmul", device_class=None, task_device_classes=None):
    params = [{"m": 4, "k": 4, "n": 4, "seed": i} for i in range(n)]
    idem = [f"idem-{device_class}-{i}" for i in range(n)]
    return queue.submit_bag(op, params, idem, device_class=device_class,
                            task_device_classes=task_device_classes)


def _has(pkg):
    try:
        importlib.import_module(pkg)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------


def test_device_class_bag_only_leases_to_capable_nodes():
    _, q = _queue()
    bag = _submit(q, n=6, device_class=CUDA)
    q.set_node_device_classes("gpu-node", [CUDA])
    q.set_node_device_classes("pi-node", [])

    blind = q.pull("pi-node", 4, lease_seconds=60, predicted_ms_per_item=10)
    assert blind == [], "a node that cannot serve the class must see nothing"

    capable = q.pull("gpu-node", 4, lease_seconds=60, predicted_ms_per_item=10)
    assert len(capable) == 4
    assert {t["bag_id"] for t in capable} == {bag}


def test_wrong_device_class_does_not_match():
    _, q = _queue()
    _submit(q, n=3, device_class=CUDA)
    q.set_node_device_classes("amd-node", [OPENCL])
    assert q.pull("amd-node", 3, lease_seconds=60, predicted_ms_per_item=10) == []


def test_device_class_none_leases_anywhere_backward_compat():
    _, q = _queue()
    _submit(q, n=4, device_class=None)
    # a node with no recorded capabilities at all — the pre-routing world
    got = q.pull("unknown-node", 4, lease_seconds=60, predicted_ms_per_item=10)
    assert len(got) == 4
    assert all(t["op"] == "matmul" for t in got)


def test_capable_node_also_gets_unrestricted_work():
    _, q = _queue()
    _submit(q, n=2, device_class=None)
    _submit(q, n=2, device_class=CUDA)
    q.set_node_device_classes("gpu-node", [CUDA])
    got = q.pull("gpu-node", 10, lease_seconds=60, predicted_ms_per_item=10)
    assert len(got) == 4, "capable node serves both restricted and open work"


def test_explicit_node_device_classes_argument_overrides_the_index():
    _, q = _queue()
    _submit(q, n=2, device_class=CUDA)
    # nothing recorded for this node, but the caller measured it right now
    got = q.pull(
        "fresh-node", 2, lease_seconds=60, predicted_ms_per_item=10, node_device_classes=[CUDA]
    )
    assert len(got) == 2


def test_per_task_device_class_overrides_the_bag():
    _, q = _queue()
    _submit(q, n=3, device_class=None, task_device_classes=[None, CUDA, None])
    q.set_node_device_classes("gpu-node", [CUDA])
    open_node = q.pull("cpu-node", 5, lease_seconds=60, predicted_ms_per_item=10)
    assert {t["seq"] for t in open_node} == {0, 2}
    gpu = q.pull("gpu-node", 5, lease_seconds=60, predicted_ms_per_item=10)
    assert {t["seq"] for t in gpu} == {1}


def test_requeued_device_task_returns_to_the_capable_node_only():
    _, q = _queue()
    _submit(q, n=2, device_class=CUDA)
    q.set_node_device_classes("gpu-node", [CUDA])
    assert len(q.pull("gpu-node", 2, lease_seconds=5, predicted_ms_per_item=10)) == 2
    assert q.sweep_expired(now=time.time() + 10.0) == 2
    assert q.pull("cpu-node", 2, lease_seconds=60, predicted_ms_per_item=10) == []
    assert len(q.pull("gpu-node", 2, lease_seconds=60, predicted_ms_per_item=10)) == 2


def test_hedges_respect_device_class():
    _, q = _queue()
    bag = _submit(q, n=8, device_class=CUDA)
    q.set_node_device_classes("gpu-a", [CUDA])
    q.pull("gpu-a", 8, lease_seconds=600, predicted_ms_per_item=10)
    q.conn.execute("UPDATE tasks SET lease_started_at=? WHERE status='leased'", (time.time() - 1e6,))
    q.conn.execute(
        "INSERT INTO results (result_key, idem_key, bag_id, payload_json, node_id, duration_s, at)"
        " VALUES ('rk','ik',?, '{}', 'gpu-a', 1.0, ?)",
        (bag, time.time()),
    )
    q.conn.commit()
    assert q.pull_hedges("cpu-node") == [], "a blind node must not hedge GPU work"
    assert q.pull_hedges("gpu-b", node_device_classes=[CUDA]), "a capable node may hedge"


# --------------------------------------------------------------------------
# fail closed AND loud
# --------------------------------------------------------------------------


def test_unservable_device_class_is_surfaced_not_hung():
    _, q = _queue()
    bag = _submit(q, n=3, device_class="nvidia:h100")
    q.set_node_device_classes("cpu-node", [])

    status = q.bag_status(bag)
    assert status["device_class"] == "nvidia:h100"
    assert status["servable"] is False
    assert "nvidia:h100" in status["blocked_reason"]
    assert status["queued"] == 3

    stuck = q.unservable_bags()
    assert [b["bag_id"] for b in stuck] == [bag]


def test_bag_becomes_servable_once_a_node_can_serve_it():
    _, q = _queue()
    bag = _submit(q, n=2, device_class=CUDA)
    assert q.bag_status(bag)["servable"] is False
    q.set_node_device_classes("gpu-node", [CUDA])
    status = q.bag_status(bag)
    assert status["servable"] is True and status["blocked_reason"] is None
    assert q.unservable_bags() == []


def test_open_bags_without_device_class_are_never_flagged():
    _, q = _queue()
    bag = _submit(q, n=2, device_class=None)
    assert q.bag_status(bag)["servable"] is True
    assert q.unservable_bags() == []


def test_node_device_class_index_round_trip():
    _, q = _queue()
    assert q.set_node_device_classes("n1", [CUDA, OPENCL, "", None]) == 2
    assert q.node_device_classes("n1") == sorted([CUDA, OPENCL])
    assert q.served_device_classes() == sorted([CUDA, OPENCL])
    q.set_node_device_classes("n1", [CUDA])
    assert q.node_device_classes("n1") == [CUDA]
    assert q.served_device_classes() == [CUDA]


# --------------------------------------------------------------------------
# migration: an old database must upgrade, keeping its work
# --------------------------------------------------------------------------

_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS bags (
    bag_id TEXT PRIMARY KEY, op TEXT, total INTEGER, done INTEGER DEFAULT 0,
    created_at REAL, status TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS tasks (
    bag_id TEXT, seq INTEGER, idem_key TEXT, params_json TEXT,
    status TEXT DEFAULT 'queued', leased_to TEXT, lease_expires_at REAL,
    attempts INTEGER DEFAULT 0, result_key TEXT,
    lease_started_at REAL, hedge_count INTEGER DEFAULT 0, hedge_by TEXT,
    hedge_expires_at REAL, hedge_started_at REAL,
    PRIMARY KEY (bag_id, seq));
CREATE TABLE IF NOT EXISTS results (
    result_key TEXT PRIMARY KEY, idem_key TEXT, bag_id TEXT, payload_json TEXT,
    node_id TEXT, duration_s REAL, at REAL);
CREATE TABLE IF NOT EXISTS node_stats (
    node_id TEXT PRIMARY KEY, ewma_ms_per_item REAL DEFAULT 0,
    ewma_var REAL DEFAULT 0, samples INTEGER DEFAULT 0,
    completions INTEGER DEFAULT 0, failures INTEGER DEFAULT 0,
    suspended INTEGER DEFAULT 0);
"""


def _legacy_db(path):
    """A database as a v2 hub left it: bags, tasks, results, node stats,
    and no idea device classes exist."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_V2_SCHEMA)
    conn.execute(
        "INSERT INTO bags (bag_id, op, total, done, created_at, status) VALUES (?,?,?,?,?,?)",
        ("bag-legacy", "primesum", 3, 1, time.time(), "open"),
    )
    for seq in range(3):
        conn.execute(
            "INSERT INTO tasks (bag_id, seq, idem_key, params_json, status, result_key)"
            " VALUES (?,?,?,?,?,?)",
            (
                "bag-legacy",
                seq,
                f"legacy-{seq}",
                '{"n":101}',
                "done" if seq == 0 else "queued",
                "rk-0" if seq == 0 else None,
            ),
        )
    conn.execute(
        "INSERT INTO results (result_key, idem_key, bag_id, payload_json, node_id, duration_s, at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("rk-0", "legacy-0", "bag-legacy", '{"count":25}', "old-node", 0.5, time.time()),
    )
    conn.execute(
        "INSERT INTO node_stats (node_id, ewma_ms_per_item, samples, completions) VALUES (?,?,?,?)",
        ("old-node", 12.5, 4, 4),
    )
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    return conn


def test_v2_database_upgrades_and_keeps_its_work(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    conn = _legacy_db(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.close()

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    q = WorkQueue(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "device_class" in cols
    assert {r["name"] for r in conn.execute("PRAGMA table_info(bags)")} >= {"device_class"}

    # the pre-existing bag survived intact, and its device_class is NULL,
    # which means "any node" — the old behaviour, unchanged
    status = q.bag_status("bag-legacy")
    assert status["total"] == 3 and status["done"] == 1 and status["queued"] == 2
    assert status["device_class"] is None
    assert status["servable"] is True
    assert q.node_stats("old-node")["completions"] == 4
    assert len(q.results_for_bag("bag-legacy")) == 1

    # and the upgraded queue still hands that legacy work out
    got = q.pull("any-node", 2, lease_seconds=60, predicted_ms_per_item=10)
    assert len(got) == 2

    # new routed work coexists with it
    new_bag = q.submit_bag("matmul", [{"m": 2}], ["new-0"], device_class=CUDA)
    assert q.bag_status(new_bag)["device_class"] == CUDA
    conn.close()


def test_reopening_an_upgraded_database_is_a_no_op(tmp_path):
    db = tmp_path / "legacy2.sqlite3"
    _legacy_db(db).close()
    for _ in range(3):
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        q = WorkQueue(conn)
        assert q.bag_status("bag-legacy")["total"] == 3
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        conn.close()


def test_fresh_database_lands_on_the_current_version():
    reg = Registry(":memory:")
    WorkQueue(reg._conn)
    assert reg._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


# --------------------------------------------------------------------------
# the op the routing exists for
# --------------------------------------------------------------------------


def test_matmul_matches_a_known_good_product():
    out = op_matmul({"a": [[1, 2], [3, 4]], "b": [[5, 6], [7, 8]]})
    assert out["matrix"] == [[19, 22], [43, 50]]
    assert out["trace"] == 19 + 50
    assert out["sum"] == 19 + 22 + 43 + 50
    assert out["tier"] in matmul_tiers_available()
    assert out["device"]


def test_matmul_identity_and_shape():
    ident = [[1 if i == j else 0 for j in range(3)] for i in range(3)]
    a = [[2, 0, 1], [3, 5, 7], [11, 13, 17]]
    assert op_matmul({"a": a, "b": ident})["matrix"] == a
    out = op_matmul({"m": 3, "k": 5, "n": 2, "seed": 9})
    assert (out["m"], out["k"], out["n"]) == (3, 5, 2)
    assert len(out["matrix"]) == 3 and len(out["matrix"][0]) == 2


def test_matmul_is_a_pure_function_of_its_params():
    first = op_matmul({"m": 6, "k": 6, "n": 6, "seed": 123})
    second = op_matmul({"m": 6, "k": 6, "n": 6, "seed": 123})
    assert first["checksum"] == second["checksum"]
    assert first["matrix"] == second["matrix"]
    assert op_matmul({"m": 6, "k": 6, "n": 6, "seed": 124})["checksum"] != first["checksum"]


def test_matmul_floor_always_runs_and_reports_itself():
    out = op_matmul({"m": 5, "k": 4, "n": 3, "seed": 2, "tier": TIER_PYTHON})
    assert out["tier"] == TIER_PYTHON
    assert out["device"] == "cpu" and out["backend"] == "cpython"


def test_matmul_withholds_huge_matrices_but_still_attests():
    out = op_matmul({"m": 64, "k": 8, "n": 64, "seed": 1, "max_return": 16})
    assert out["matrix"] is None
    assert len(out["checksum"]) == 64 and isinstance(out["sum"], int)


def test_matmul_rejects_mismatched_explicit_shapes():
    with pytest.raises(ValueError):
        op_matmul({"a": [[1, 2, 3]], "b": [[1, 2]]})


@pytest.mark.skipif(not _has("numpy"), reason="numpy not available on this node")
def test_numpy_tier_agrees_with_the_pure_python_floor():
    """The tower may only degrade DOWNWARD — never into a different answer."""
    assert TIER_NUMPY_BLAS in matmul_tiers_available()
    params = {"m": 12, "k": 9, "n": 7, "seed": 4321}
    floor = op_matmul(dict(params, tier=TIER_PYTHON))
    blas = op_matmul(dict(params, tier=TIER_NUMPY_BLAS))
    assert blas["tier"] == TIER_NUMPY_BLAS
    assert floor["tier"] == TIER_PYTHON
    assert blas["matrix"] == floor["matrix"]
    assert blas["checksum"] == floor["checksum"]


@pytest.mark.skipif(not _has("numpy"), reason="numpy not available on this node")
def test_vectorised_generation_matches_the_scalar_generator():
    for seed in (0, 1, 4321, 2**40 + 7):
        for stream in (1, 2):
            assert (
                _gen_matrix_numpy(7, 5, seed, stream).tolist()
                == _gen_matrix_python(7, 5, seed, stream)
            )


def test_matmul_never_claims_a_tier_it_cannot_run():
    """Pinning a tier this node lacks degrades and says so, rather than
    lying about where the work happened."""
    out = op_matmul({"m": 4, "k": 4, "n": 4, "seed": 1, "tier": "torch_cuda"})
    available = matmul_tiers_available()
    assert out["tier"] in available
    if "torch_cuda" not in available:
        assert out["tier"] != "torch_cuda"
        assert "cuda" not in out["device"].lower()
