import json

from swarm.core.models import (
    AgentCapability,
    BenchResult,
    LinkMeasurement,
    MeasurementTrust,
    MemoryInfo,
    NodeProfile,
)
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


GIB = 1024**3


def _link(reg, nid, bw_bps, rtt_ms, trust=MeasurementTrust.STANDARD):
    reg.record_link(
        LinkMeasurement(
            src_node=nid,
            dst_node="hub",
            rtt_p50_ms=rtt_ms,
            rtt_p95_ms=rtt_ms,
            bandwidth_bps=bw_bps,
            direct=True,
            trust=trust,
            measured_at=1.0,
        )
    )


def _runs(plan):
    """Collapse the assignment list into consecutive (node_id, count) runs."""
    runs = []
    for a in plan.assignments:
        if runs and runs[-1][0] == a.node_id:
            runs[-1][1] += 1
        else:
            runs.append([a.node_id, 1])
    return [(nid, n) for nid, n in runs]


# --- THE MISSING TEST -------------------------------------------------------


def test_plan_distributes_across_two_nodes_when_model_cannot_fit_one():
    reg = Registry(":memory:")
    _node(reg, "a", free_gib=4, gflops=10.0)
    _node(reg, "b", free_gib=4, gflops=10.0)
    stages = [ModelStage(name=f"blk{i}", flops=1e9, peak_mem_bytes=2 * GIB) for i in range(4)]
    plan = plan_pipeline(reg, "too-fat-for-one", stages)
    assert plan.feasible, plan.reason
    used = {a.node_id for a in plan.assignments}
    assert len(used) >= 2, f"expected a real pipeline, got {used}: {plan.reason}"
    assert len(plan.assignments) == 4


def test_plan_bottleneck_and_latency_differ_for_uneven_pipeline():
    reg = Registry(":memory:")
    _node(reg, "a", free_gib=4, gflops=10.0)
    _node(reg, "b", free_gib=4, gflops=10.0)
    # 6 GiB total: neither node can hold the model, so it must shard 1:1.
    stages = [
        ModelStage(name="heavy", flops=10e9, peak_mem_bytes=3 * GIB),
        ModelStage(name="light", flops=1e9, peak_mem_bytes=3 * GIB),
    ]
    plan = plan_pipeline(reg, "uneven", stages)
    assert plan.feasible, plan.reason
    assert len({a.node_id for a in plan.assignments}) == 2
    est = [a.est_ms for a in plan.assignments]
    assert plan.bottleneck_ms == max(est)
    assert plan.latency_ms == sum(est)
    assert plan.bottleneck_ms < plan.latency_ms
    # steady-state rate is governed by the slowest stage, not the sum
    assert plan.throughput_per_s == 1000.0 / plan.bottleneck_ms
    # deprecated alias still carries the latency number
    assert plan.estimated_end_to_end_ms == plan.latency_ms


def test_plan_never_shards_a_model_that_fits_on_one_node():
    reg = Registry(":memory:")
    _node(reg, "big", free_gib=32, gflops=4.0)
    _node(reg, "also-big", free_gib=32, gflops=1.0)
    stages = [ModelStage(name=f"blk{i}", flops=1e9, peak_mem_bytes=1 * GIB) for i in range(6)]
    plan = plan_pipeline(reg, "small-model", stages)
    assert plan.feasible, plan.reason
    assert {a.node_id for a in plan.assignments} == {"big"}  # fastest whole-fit node
    assert "not sharding" in plan.reason
    # no pipelining happened, so the two timings coincide
    assert plan.bottleneck_ms == plan.latency_ms


def test_plan_infeasible_reason_names_the_offending_stage():
    reg = Registry(":memory:")
    _node(reg, "a", free_gib=4, gflops=10.0)
    _node(reg, "b", free_gib=4, gflops=10.0)
    stages = [
        ModelStage(name="tiny-embed", flops=1e9, peak_mem_bytes=1 * GIB),
        ModelStage(name="huge-mlp", flops=1e9, peak_mem_bytes=9 * GIB),
    ]
    plan = plan_pipeline(reg, "lopsided", stages)
    assert not plan.feasible
    assert "huge-mlp" in plan.reason
    assert "measured free memory" in plan.reason
    assert str(9 * GIB - 4 * GIB) in plan.reason  # byte shortfall is named
    assert plan.bottleneck_ms is None and plan.latency_ms is None


def test_plan_infeasible_when_aggregate_capacity_is_short():
    reg = Registry(":memory:")
    _node(reg, "a", free_gib=2, gflops=10.0)
    _node(reg, "b", free_gib=2, gflops=10.0)
    stages = [ModelStage(name=f"blk{i}", flops=1e9, peak_mem_bytes=2 * GIB) for i in range(4)]
    plan = plan_pipeline(reg, "way-too-fat", stages)
    assert not plan.feasible
    assert "blk" in plan.reason
    assert "measured free memory" in plan.reason


def test_plan_segments_are_contiguous_per_node():
    reg = Registry(":memory:")
    _node(reg, "a", free_gib=3, gflops=10.0)
    _node(reg, "b", free_gib=3, gflops=10.0)
    _node(reg, "c", free_gib=3, gflops=10.0)
    stages = [ModelStage(name=f"blk{i}", flops=1e9, peak_mem_bytes=1 * GIB) for i in range(6)]
    plan = plan_pipeline(reg, "chain", stages)
    assert plan.feasible, plan.reason
    runs = _runs(plan)
    # each node owns exactly ONE run of stages: no ping-ponging
    assert len(runs) == len({nid for nid, _ in runs}), f"non-contiguous: {runs}"
    assert sum(n for _, n in runs) == 6
    assert len(runs) >= 2


def test_plan_says_when_it_falls_back_to_throughput_ordering():
    reg = Registry(":memory:")
    _node(reg, "a", free_gib=4, gflops=10.0)
    _node(reg, "b", free_gib=4, gflops=10.0)
    stages = [ModelStage(name=f"blk{i}", flops=1e9, peak_mem_bytes=2 * GIB) for i in range(4)]
    plan = plan_pipeline(reg, "m", stages)
    assert plan.feasible, plan.reason
    assert "no measured link data" in plan.reason
    assert "measured throughput" in plan.reason


def test_plan_uses_measured_link_data_to_order_the_chain():
    reg = Registry(":memory:")
    _node(reg, "a", free_gib=4, gflops=10.0)
    _node(reg, "b", free_gib=4, gflops=10.0)
    _link(reg, "a", bw_bps=1e9, rtt_ms=1.0)
    _link(reg, "b", bw_bps=1e6, rtt_ms=50.0)
    stages = [ModelStage(name=f"blk{i}", flops=1e9, peak_mem_bytes=2 * GIB) for i in range(4)]
    plan = plan_pipeline(reg, "m", stages)
    assert plan.feasible, plan.reason
    assert "measured link quality" in plan.reason
    # best-linked node heads the chain
    assert plan.assignments[0].node_id == "a"


def test_plan_stays_json_serializable():
    reg = Registry(":memory:")
    _node(reg, "a", free_gib=4, gflops=10.0)
    _node(reg, "b", free_gib=4, gflops=10.0)
    stages = [ModelStage(name=f"blk{i}", flops=1e9, peak_mem_bytes=2 * GIB) for i in range(4)]
    plan = plan_pipeline(reg, "m", stages)
    blob = json.loads(json.dumps(to_dict(plan)))
    assert blob["estimated_end_to_end_ms"] == blob["latency_ms"]
    assert blob["bottleneck_ms"] is not None
    assert blob["assignments"][0]["fits_mem"] is True
