import json

from swarm.core.models import AgentCapability, BenchResult, MeasurementTrust, MemoryInfo, NodeProfile
from swarm.core.serde import to_dict
from swarm.hub.pipeline import ModelStage, plan_pipeline
from swarm.hub.registry import Registry


def _node(reg, nid, free_gib, gflops=None):
    profile = NodeProfile(
        node_id=nid,
        hostname=nid,
        os="linux",
        arch="x86_64",
        memory=MemoryInfo(total_bytes=free_gib * 1024**3, free_bytes=free_gib * 1024**3),
    )
    reg.upsert_node(profile, AgentCapability(), json.dumps(to_dict(profile)), "{}")
    if gflops is not None:
        reg.record_bench(
            nid,
            BenchResult(
                name="cpu_fp32_gflops",
                value=gflops,
                unit="GFLOPS",
                trust=MeasurementTrust.STANDARD,
                benchmark_run_id=f"run-{nid}",
            ),
        )


def test_plan_maps_stages_to_nodes_with_enough_memory():
    reg = Registry(":memory:")
    _node(reg, "big", free_gib=8, gflops=10.0)
    _node(reg, "small", free_gib=2, gflops=2.0)
    stages = [
        ModelStage(name="shard1", flops=5e9, peak_mem_bytes=3 * 1024**3),
        ModelStage(name="shard2", flops=1e9, peak_mem_bytes=1 * 1024**3),
    ]
    plan = plan_pipeline(reg, "toy-7b", stages)
    assert plan.feasible
    assert plan.assignments[0].node_id == "big"
    assert plan.estimated_end_to_end_ms is not None


def test_plan_fails_closed_when_no_memory_data():
    reg = Registry(":memory:")
    profile = NodeProfile(node_id="mystery", hostname="mystery", os="x", arch="y")
    reg.upsert_node(profile, AgentCapability(), json.dumps(to_dict(profile)), "{}")
    plan = plan_pipeline(reg, "toy", [ModelStage("s", 1e9, 100)])
    assert not plan.feasible


def test_plan_refuses_when_no_node_fits():
    reg = Registry(":memory:")
    _node(reg, "tiny", free_gib=1)
    plan = plan_pipeline(reg, "fat", [ModelStage("s", 1e9, 8 * 1024**3)])
    assert not plan.feasible
    assert "measured free memory" in plan.reason


def test_plan_never_schedules_on_unmeasured():
    reg = Registry(":memory:")
    _node(reg, "mem-only", free_gib=64)
    plan = plan_pipeline(reg, "m", [ModelStage("s", 1e9, 100)])
    assert plan.feasible
    assert plan.assignments[0].est_ms is None
