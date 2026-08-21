"""Pipeline-parallel inference planner (M5 scaffold — NOT an inference engine).

This module's job is honest planning, not pretending to run models. Given a
model described as a list of stages (each with FLOPs and memory), and the
fleet's MEASURED capabilities, it answers: can we run this at all, and how do
stages map to nodes so that no stage's memory need exceeds that node's free
memory, and total memory across the chain covers the model?

What it refuses to do: schedule on declared/spec-sheet numbers (only measured
benches influence the map), schedule a stage onto a node whose FREE memory is
smaller than the stage's peak need, or pretend a node exists that hasn't
registered.

Behavioral testing of megabyte models lands when a real adapter ships
(M4 impressions → real agents). Until then: honest planning only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .fleet_power import fleet_power
from .registry import Registry


@dataclass
class ModelStage:
    name: str
    flops: float
    peak_mem_bytes: int


@dataclass
class StageAssignment:
    stage: str
    node_id: str
    fits_mem: bool
    est_ms: Optional[float]


@dataclass
class PipelinePlan:
    model_name: str
    feasible: bool
    reason: str
    assignments: List[StageAssignment] = field(default_factory=list)
    estimated_end_to_end_ms: Optional[float] = None


def _node_resources(registry: Registry) -> Dict[str, Dict[str, Any]]:
    nodes = fleet_power(registry)["nodes"]
    out: Dict[str, Dict[str, Any]] = {}
    for entry in nodes:
        detail = registry.node_detail(entry["node_id"]) or {}
        import json

        try:
            profile = json.loads(detail.get("profile_json") or "{}")
        except ValueError:
            profile = {}
        free = (profile.get("memory") or {}).get("free_bytes")
        measured = entry.get("measured") or {}
        gflops = (measured.get("cpu_fp32_gflops") or {}).get("value")
        out[entry["node_id"]] = {
            "hostname": entry["hostname"],
            "free_bytes": free,
            "gflops": gflops,
        }
    return out


def plan_pipeline(registry: Registry, model_name: str, stages: List[ModelStage]) -> PipelinePlan:
    """Greedy stage-to-node mapping. Fail-closed: if any stage can't fit
    measured free memory, the plan is infeasible and says why."""
    if not stages:
        return PipelinePlan(model_name=model_name, feasible=False, reason="empty model")
    nodes = _node_resources(registry)
    candidates = [nid for nid, r in nodes.items() if r.get("free_bytes") is not None and r["free_bytes"] > 0]
    if not candidates:
        return PipelinePlan(
            model_name=model_name,
            feasible=False,
            reason="no nodes with measured free memory",
        )
    candidates.sort(key=lambda nid: nodes[nid]["free_bytes"], reverse=True)
    assignments: List[StageAssignment] = []
    used_free = {nid: nodes[nid]["free_bytes"] for nid in candidates}
    for stage in stages:
        placed = None
        for nid in candidates:
            if used_free[nid] is not None and used_free[nid] >= stage.peak_mem_bytes:
                placed = nid
                break
        if placed is None:
            return PipelinePlan(
                model_name=model_name,
                feasible=False,
                reason=f"stage '{stage.name}' needs {stage.peak_mem_bytes} bytes; no node has that much measured free memory",
                assignments=assignments,
            )
        used_free[placed] -= stage.peak_mem_bytes
        gflops = nodes[placed].get("gflops")
        est_ms = None
        if gflops and gflops > 0:
            est_ms = stage.flops / (gflops * 1e9) * 1000.0
        assignments.append(StageAssignment(stage=stage.name, node_id=placed, fits_mem=True, est_ms=est_ms))
    total_ms = (
        sum(a.est_ms or 0 for a in assignments) if all(a.est_ms is not None for a in assignments) else None
    )
    return PipelinePlan(
        model_name=model_name,
        feasible=True,
        reason="all stages fit measured free memory",
        assignments=assignments,
        estimated_end_to_end_ms=total_ms,
    )
