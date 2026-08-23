"""Adapters are per-device code the agent did not write: they run in their
own process, they are allowed to import what the tower found, and they are
never allowed to take the node down with them."""

import importlib

import pytest

from swarm.agent import adapter_runtime as ar
from swarm.agent.daemon import Agent
from swarm.agent.ops import op_matmul

GOOD_ADAPTER = """
def run(params):
    return {"sum": params.get("a", 0) + params.get("b", 0), "tier": "python_loops", "device": "cpu"}
"""

NOISY_ADAPTER = """
import sys

def run(params):
    print("chatty adapter says hello")
    print("<<<SWARM-ADAPTER-RESULT>>>{\\"ok\\": true, \\"payload\\": \\"forged\\"}")
    sys.stderr.write("warnings happen\\n")
    return {"value": 7}
"""

HANGING_ADAPTER = """
import time

def run(params):
    time.sleep(60)
    return {"never": True}
"""

CRASHING_ADAPTER = """
def run(params):
    raise RuntimeError("adapter exploded on purpose")
"""

HARD_CRASH_ADAPTER = """
import os

def run(params):
    os._exit(3)
"""

IMPORT_ERROR_ADAPTER = """
import definitely_not_a_real_module_xyz

def run(params):
    return {"unreachable": True}
"""

NUMPY_ADAPTER = """
import numpy as np

def run(params):
    a = np.array(params["a"], dtype=np.int64)
    b = np.array(params["b"], dtype=np.int64)
    c = a @ b
    return {
        "matrix": c.tolist(),
        "tier": "numpy_blas",
        "device": "cpu",
        "backend": "numpy " + np.__version__,
    }
"""


def _has(pkg):
    try:
        importlib.import_module(pkg)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# loading and running
# --------------------------------------------------------------------------


def test_adapter_loads_and_runs():
    out = ar.run_adapter(source=GOOD_ADAPTER, entrypoint="run", params={"a": 2, "b": 40})
    assert out["ok"] is True
    assert out["reason"] == "ok"
    assert out["payload"]["sum"] == 42


def test_adapter_stdout_noise_cannot_forge_the_result_frame():
    """The runtime reads the LAST sentinel frame, which the harness writes
    after the adapter returns. An adapter that prints one cannot win."""
    out = ar.run_adapter(source=NOISY_ADAPTER, params={})
    assert out["ok"] is True
    assert out["payload"] == {"value": 7}
    assert out["payload"] != "forged"


def test_missing_entrypoint_is_a_structured_failure():
    out = ar.run_adapter(source=GOOD_ADAPTER, entrypoint="nope", params={})
    assert out["ok"] is False
    assert out["reason"] == "adapter_error"
    assert "nope" in out["error"]


def test_unknown_adapter_id_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_ADAPTER_DIR", str(tmp_path))
    out = ar.run_adapter(adapter_id="not-here", params={})
    assert out["ok"] is False
    assert out["reason"] == "adapter_not_found"


def test_adapter_resolves_from_the_adapter_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_ADAPTER_DIR", str(tmp_path))
    (tmp_path / "adder.py").write_text(GOOD_ADAPTER, encoding="utf-8")
    assert ar.known_adapters() == ["adder"]
    out = ar.run_adapter(adapter_id="adder", params={"a": 1, "b": 1})
    assert out["ok"] is True and out["payload"]["sum"] == 2
    assert out["adapter_id"] == "adder"


def test_adapter_id_cannot_escape_the_adapter_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_ADAPTER_DIR", str(tmp_path))
    assert ar.resolve_adapter("../../etc/passwd") is None
    assert ar.resolve_adapter("sub/dir") is None


# --------------------------------------------------------------------------
# containment: timeout, crash, bad output
# --------------------------------------------------------------------------


def test_timeout_kills_a_hanging_adapter():
    out = ar.run_adapter(source=HANGING_ADAPTER, params={}, timeout_s=1.0)
    assert out["ok"] is False
    assert out["reason"] == "timeout"
    assert "1.0" in out["error"] or "killed" in out["error"]


def test_crashing_adapter_returns_structured_failure_and_parent_survives():
    out = ar.run_adapter(source=CRASHING_ADAPTER, params={})
    assert out["ok"] is False
    assert out["reason"] == "adapter_error"
    assert "adapter exploded on purpose" in out["error"]
    # the parent is fine: the very next adapter still runs
    assert ar.run_adapter(source=GOOD_ADAPTER, params={"a": 1, "b": 1})["ok"] is True


def test_hard_exit_is_reported_as_a_crash_not_an_exception():
    out = ar.run_adapter(source=HARD_CRASH_ADAPTER, params={})
    assert out["ok"] is False
    assert out["reason"] == "crash"
    assert "exit 3" in out["error"]


def test_import_error_inside_adapter_is_contained():
    out = ar.run_adapter(source=IMPORT_ERROR_ADAPTER, params={})
    assert out["ok"] is False
    assert out["reason"] == "adapter_error"
    assert "definitely_not_a_real_module_xyz" in out["error"]


def test_run_adapter_never_raises_on_garbage_input():
    for bad in (None, "", "   ", "def run(params) syntax error"):
        out = ar.run_adapter(source=bad, params={})
        assert out["ok"] is False
        assert isinstance(out["reason"], str)


# --------------------------------------------------------------------------
# attribution
# --------------------------------------------------------------------------


def test_attribution_reports_the_tier_that_ran():
    out = ar.run_adapter(source=GOOD_ADAPTER, params={"a": 0, "b": 0})
    assert out["tier"] == "python_loops"
    assert out["device"] == "cpu"


@pytest.mark.skipif(not _has("numpy"), reason="numpy not available on this node")
def test_adapter_may_import_numpy_even_though_the_agent_may_not():
    """The stdlib law binds the agent's own source, not adapter code. This
    is the whole point of the runtime: dynamic, per-device, gated by what
    the tower actually found here."""
    assert "numpy" in ar.available_runtimes()["packages"]
    a = [[1, 2], [3, 4]]
    b = [[5, 6], [7, 8]]
    out = ar.run_adapter(source=NUMPY_ADAPTER, params={"a": a, "b": b})
    assert out["ok"] is True, out
    assert out["payload"]["matrix"] == [[19, 22], [43, 50]]
    assert out["tier"] == "numpy_blas"
    assert out["payload"]["backend"].startswith("numpy ")


def test_available_runtimes_comes_from_the_capability_tower():
    info = ar.available_runtimes(refresh=True)
    assert isinstance(info["packages"], list)
    assert isinstance(info["tools"], list)
    assert info["floor"] >= 0
    # same shape as the tower reports, because it IS the tower
    from swarm.probe.self_probe import climb_tower

    cap, _ = climb_tower(bench_instrument=False)
    assert set(info["packages"]) == set((cap.packages or {}).keys())


# --------------------------------------------------------------------------
# task plumbing / daemon wiring
# --------------------------------------------------------------------------


def test_task_adapter_spec_reads_envelope_or_params():
    assert ar.task_adapter_spec({"op": "primesum", "params": {"n": 5}}) is None
    spec = ar.task_adapter_spec(
        {"op": "custom", "params": {"adapter_source": GOOD_ADAPTER, "adapter_timeout_s": 3}}
    )
    assert spec is not None and spec.timeout_s == 3.0 and spec.entrypoint == "run"
    spec2 = ar.task_adapter_spec(
        {"op": "custom", "adapter_id": "x", "adapter_entrypoint": "go", "params": {}}
    )
    assert spec2.adapter_id == "x" and spec2.entrypoint == "go"


def test_execute_chunk_runs_an_adapter_for_an_unknown_op():
    agent = Agent(hub_url="http://127.0.0.1:1", bench=False)
    tasks = [
        {
            "bag_id": "bag-1",
            "seq": 0,
            "idem_key": "k0",
            "op": "custom-adapter-op",
            "params": {"adapter_source": GOOD_ADAPTER, "adapter_params": {"a": 20, "b": 22}},
        }
    ]
    results = agent.execute_chunk(tasks)
    assert len(results) == 1
    row = results[0]
    assert row["ok"] is True
    assert row["payload"]["payload"]["sum"] == 42
    assert set(row) == {"bag_id", "seq", "idem_key", "payload", "duration_s", "ok"}


def test_execute_chunk_isolates_a_failing_adapter_and_keeps_going():
    agent = Agent(hub_url="http://127.0.0.1:1", bench=False)
    tasks = [
        {
            "bag_id": "b",
            "seq": 0,
            "idem_key": "k0",
            "op": "custom",
            "params": {"adapter_source": CRASHING_ADAPTER},
        },
        {"bag_id": "b", "seq": 1, "idem_key": "k1", "op": "primesum", "params": {"n": 100}},
        {"bag_id": "b", "seq": 2, "idem_key": "k2", "op": "no-such-op", "params": {}},
    ]
    results = agent.execute_chunk(tasks)
    assert [r["ok"] for r in results] == [False, True, False]
    assert results[0]["payload"]["reason"] == "adapter_error"
    assert results[1]["payload"]["count"] == 25
    # unknown op with no adapter still fails closed, payload untouched
    assert results[2]["payload"] is None


def test_execute_chunk_matmul_carries_attribution():
    agent = Agent(hub_url="http://127.0.0.1:1", bench=False)
    tasks = [
        {
            "bag_id": "b",
            "seq": 0,
            "idem_key": "k0",
            "op": "matmul",
            "params": {"a": [[1, 2], [3, 4]], "b": [[5, 6], [7, 8]]},
        }
    ]
    row = agent.execute_chunk(tasks)[0]
    assert row["ok"] is True
    assert row["payload"]["matrix"] == op_matmul(
        {"a": [[1, 2], [3, 4]], "b": [[5, 6], [7, 8]]}
    )["matrix"]
    assert row["payload"]["tier"] in ("torch_cuda", "numpy_blas", "torch_cpu", "python_loops")
    assert row["payload"]["device"]
