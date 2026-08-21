import json

from swarm.core.models import (
    AgentCapability,
    BenchResult,
    MeasurementTrust,
    NodeProfile,
)
from swarm.core.serde import to_dict
from swarm.hub.queue import DEFAULT_MIN_CHUNK
from swarm.hub.registry import Registry
from swarm.hub.scheduler import ChunkPlanner


def _registry_with_bench(
    node_id: str, value: float, trust: MeasurementTrust, variance: float = 0.0
) -> Registry:
    reg = Registry(":memory:")
    profile = NodeProfile(node_id=node_id, hostname=node_id, os="linux", arch="x86_64")
    reg.upsert_node(profile, AgentCapability(), json.dumps(to_dict(profile)), "{}")
    reg.record_bench(
        node_id,
        BenchResult(
            name="cpu_fp32_gflops",
            value=value,
            unit="GFLOPS",
            trust=trust,
            variance=variance,
            samples=[value, value],
            benchmark_run_id=f"run-{node_id}",
        ),
    )
    return reg


def _plan(registry, node_id, bag_total=1000, bag_remaining=1000, stats=None, workers=1):
    planner = ChunkPlanner(registry)
    return planner.plan(
        node_id=node_id,
        op="primesum",
        bag_total=bag_total,
        bag_remaining=bag_remaining,
        queue_stats=stats or {"ewma_ms_per_item": 0.0, "ewma_var": 0.0, "samples": 0},
        active_worker_count=workers,
    )


def test_new_node_gets_floor_size_chunk():
    reg = _registry_with_bench("fresh", 10.0, MeasurementTrust.STANDARD)
    chunk, lease, _ = _plan(reg, "fresh")
    assert chunk == DEFAULT_MIN_CHUNK
    assert lease >= 30.0


def test_faster_measured_node_pulls_bigger_chunk():
    reg = Registry(":memory:")
    for node_id, value in (("slow", 5.0), ("fast", 50.0)):
        profile = NodeProfile(node_id=node_id, hostname=node_id, os="linux", arch="x86_64")
        reg.upsert_node(profile, AgentCapability(), json.dumps(to_dict(profile)), "{}")
        reg.record_bench(
            node_id,
            BenchResult(
                name="cpu_fp32_gflops",
                value=value,
                unit="GFLOPS",
                trust=MeasurementTrust.CALIBRATED,
                benchmark_run_id=f"run-{node_id}",
            ),
        )
    stats = {"ewma_ms_per_item": 1000.0, "ewma_var": 0.0, "samples": 10}
    slow_chunk, _, _ = _plan(reg, "slow", stats=stats)
    fast_chunk, _, _ = _plan(reg, "fast", stats=stats)
    assert fast_chunk > slow_chunk


def test_noisy_node_shrinks():
    reg = _registry_with_bench("steady", 20.0, MeasurementTrust.STANDARD, variance=0.0)
    reg2 = Registry(":memory:")
    profile = NodeProfile(node_id="noisy", hostname="noisy", os="linux", arch="x86_64")
    reg2.upsert_node(profile, AgentCapability(), json.dumps(to_dict(profile)), "{}")
    reg2.record_bench(
        "noisy",
        BenchResult(
            name="cpu_fp32_gflops",
            value=20.0,
            unit="GFLOPS",
            trust=MeasurementTrust.STANDARD,
            variance=400.0,
            samples=[5.0, 35.0],
            benchmark_run_id="run-noisy",
        ),
    )
    stats = {"ewma_ms_per_item": 1000.0, "ewma_var": 0.0, "samples": 10}
    steady_chunk, _, _ = _plan(reg, "steady", stats=stats)
    noisy_chunk, _, _ = _plan(reg2, "noisy", stats=stats)
    assert noisy_chunk <= steady_chunk


def test_tail_shrink_near_end_of_bag(monkeypatch):
    import swarm.hub.scheduler as sched

    monkeypatch.setattr(sched, "BASE_CHUNK", 100)
    reg = _registry_with_bench("n", 50.0, MeasurementTrust.CALIBRATED)
    stats = {"ewma_ms_per_item": 100.0, "ewma_var": 0.0, "samples": 20}
    big_chunk, _, _ = _plan(reg, "n", bag_total=1000, bag_remaining=900, stats=stats, workers=2)
    tail_chunk, _, _ = _plan(reg, "n", bag_total=1000, bag_remaining=24, stats=stats, workers=2)
    assert tail_chunk < big_chunk


def test_lease_scales_with_chunk_and_prediction():
    reg = _registry_with_bench("n", 10.0, MeasurementTrust.STANDARD)
    stats = {"ewma_ms_per_item": 2000.0, "ewma_var": 0.0, "samples": 10}
    chunk, lease, predicted = _plan(reg, "n", stats=stats)
    assert lease >= 3.0 * chunk * predicted / 1000.0 + 30.0 - 1e-6
    assert predicted == 2000.0


def test_unmeasured_node_still_gets_floor_work():
    reg = Registry(":memory:")
    profile = NodeProfile(node_id="bare", hostname="bare", os="plan9", arch="mips")
    reg.upsert_node(profile, AgentCapability(), json.dumps(to_dict(profile)), "{}")
    chunk, lease, _ = _plan(reg, "bare")
    assert chunk == DEFAULT_MIN_CHUNK
