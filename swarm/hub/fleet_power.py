"""The fuel gauge: how much compute does the organism actually have at its
disposal, right now, honestly?

Rule: numbers are grouped by the trust of the instrument that produced them.
"Proven" = measured under benchmark with STANDARD or better trust.
"Fallback" = pure-stdlib estimates — real, but rough; the organism knows it.
"Sensed" = present in the profile, never benchmarked. Never summed across
tiers into one fantasy number.
"""

from __future__ import annotations

from typing import Any, Dict

from ..core.models import MeasurementTrust
from .registry import Registry

PROVEN_TIERS = {
    MeasurementTrust.VERIFIED.value,
    MeasurementTrust.CALIBRATED.value,
    MeasurementTrust.STANDARD.value,
}
FALLBACK_TIERS = {MeasurementTrust.FALLBACK.value}

TRACKED_KINDS = ["cpu_fp32_gflops", "mem_bandwidth_gbps", "mem_latency_ns"]


def fleet_power(registry: Registry) -> Dict[str, Any]:
    nodes = registry.list_nodes()
    report: Dict[str, Any] = {
        "node_count": len(nodes),
        "nodes": [],
        "totals": {},
    }
    totals: Dict[str, Dict[str, float]] = {}
    for node in nodes:
        nid = node["node_id"]
        entry: Dict[str, Any] = {
            "node_id": nid,
            "hostname": node["hostname"],
            "last_seen": node["last_seen"],
            "measured": {},
        }
        for bench in registry.latest_benches(nid):
            name = bench["name"]
            if name not in TRACKED_KINDS:
                continue
            value = bench["value"]
            trust = bench["trust"]
            bucket = (
                "proven" if trust in PROVEN_TIERS else "fallback" if trust in FALLBACK_TIERS else "sensed"
            )
            entry["measured"][name] = {
                "value": value,
                "unit": bench["unit"],
                "tier": bucket,
                "variance": bench["variance"],
            }
            if value is not None and bucket != "sensed":
                t = totals.setdefault(name, {"proven": 0.0, "fallback": 0.0})
                t[bucket] += value
        report["nodes"].append(entry)
    report["totals"] = totals
    return report
