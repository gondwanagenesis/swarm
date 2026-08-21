import json

from swarm.core.models import (
    AgentCapability,
    BenchResult,
    MeasurementTrust,
    NodeProfile,
)
from swarm.core.serde import to_dict
from swarm.hub.fleet_power import fleet_power
from swarm.hub.registry import Registry


def _reg_with(node_id: str, kind: str, value: float, trust: MeasurementTrust) -> None:
    global REG
    profile = NodeProfile(node_id=node_id, hostname=node_id, os="linux", arch="x86_64")
    REG.upsert_node(profile, AgentCapability(), json.dumps(to_dict(profile)), "{}")
    REG.record_bench(
        node_id,
        BenchResult(
            name=kind,
            value=value,
            unit="GFLOPS" if "gflops" in kind else "GB/s",
            trust=trust,
            benchmark_run_id=f"run-{node_id}",
        ),
    )


REG = None


def setup_function():
    global REG
    REG = Registry(":memory:")


def test_fleet_never_sums_across_trust_tiers():
    _reg_with("a", "cpu_fp32_gflops", 100.0, MeasurementTrust.STANDARD)
    _reg_with("b", "cpu_fp32_gflops", 5.0, MeasurementTrust.FALLBACK)
    report = fleet_power(REG)
    totals = report["totals"]["cpu_fp32_gflops"]
    assert totals["proven"] == 100.0
    assert totals["fallback"] == 5.0
    assert report["node_count"] == 2
    per_node = {n["node_id"]: n for n in report["nodes"]}
    assert per_node["a"]["measured"]["cpu_fp32_gflops"]["tier"] == "proven"
    assert per_node["b"]["measured"]["cpu_fp32_gflops"]["tier"] == "fallback"


def test_fleet_handles_unmeasured_nodes():
    profile = NodeProfile(node_id="bare", hostname="bare", os="plan9", arch="mips")
    REG.upsert_node(profile, AgentCapability(), json.dumps(to_dict(profile)), "{}")
    report = fleet_power(REG)
    assert report["node_count"] == 1
    assert report["totals"] == {}
    assert report["nodes"][0]["measured"] == {}
