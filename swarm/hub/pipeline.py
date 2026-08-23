"""Pipeline-parallel inference planner (M5 scaffold — NOT an inference engine).

This module's job is honest planning, not pretending to run models. Given a
model described as an ORDERED list of stages (each with FLOPs and memory), and
the fleet's MEASURED capabilities, it answers: can we run this at all, and how
do stages map to nodes so that no node's resident set exceeds its free memory?

Two timings, because they answer two different questions
--------------------------------------------------------
- ``bottleneck_ms`` — the slowest node's segment time. In a pipeline running at
  steady state every node works concurrently on a different microbatch, so the
  slowest segment sets the rate. This is the number that governs THROUGHPUT.
  With one stage per node it degenerates to ``max(stage.est_ms)``.
- ``latency_ms`` — the sum of every stage's time: the end-to-end cost of pushing
  ONE token/pass through the whole chain with zero pipelining. This is the
  number that governs LATENCY.
- ``estimated_end_to_end_ms`` is a deprecated alias of ``latency_ms``, kept so
  the existing ``/api/pipeline/plan`` payload does not change shape.
- ``throughput_per_s`` = ``1000 / bottleneck_ms`` when the bottleneck is known.

Neither figure includes activation-transfer time between nodes; the planner does
not have a measured tensor-transfer benchmark to derive it from, and inventing
one would be a declared number. Link measurements are used only to ORDER the
chain, never to fabricate a transfer cost.

Mapping algorithm
-----------------
Contiguous-chain min-max partitioning by exact dynamic programming. Stages are
ordered and each node receives a CONTIGUOUS run of them — that is what makes it
a pipeline; you cannot ping-pong between nodes stage by stage. The DP is
O(nodes * stages^2), which is nothing at M5 scale.

What it refuses to do: schedule on declared/spec-sheet numbers (only measured
benches and measured links influence the map), put a segment on a node whose
FREE memory is smaller than that segment's resident need (a HARD filter, never
a soft cost), or shard a model that fits on one node — sharding costs latency
always and buys capacity only.

Behavioral testing of megabyte models lands when a real adapter ships
(M4 impressions → real agents). Until then: honest planning only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .fleet_power import PROVEN_TIERS, fleet_power
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
    #: DEPRECATED alias of :attr:`latency_ms`. Kept for wire compatibility with
    #: ``/api/pipeline/plan``. New callers should read ``latency_ms`` (one pass,
    #: no pipelining) or ``bottleneck_ms`` (steady-state rate) explicitly.
    estimated_end_to_end_ms: Optional[float] = None
    #: Slowest node segment — governs steady-state throughput.
    bottleneck_ms: Optional[float] = None
    #: Sum of all stage times — one-pass end-to-end latency.
    latency_ms: Optional[float] = None
    #: Passes per second at steady state, derived from ``bottleneck_ms``.
    throughput_per_s: Optional[float] = None


# A DP state is (unknown_segments, bottleneck_ms). Segments landing on a node
# with no measured throughput contribute an unknown, never a guessed millisecond
# count; states are compared lexicographically so a plan whose timing we can
# actually verify wins over one we would have to invent numbers for.
_State = Tuple[int, float]
_ZERO: _State = (0, 0.0)


def _combine(a: _State, b: _State) -> _State:
    return (a[0] + b[0], a[1] if a[1] >= b[1] else b[1])


def _node_resources(registry: Registry) -> Dict[str, Dict[str, Any]]:
    """Measured-only view of the fleet: free bytes from the profile's memory
    reading, throughput from a bench that actually ran."""
    nodes = fleet_power(registry)["nodes"]
    out: Dict[str, Dict[str, Any]] = {}
    for entry in nodes:
        detail = registry.node_detail(entry["node_id"]) or {}
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


def _link_quality(registry: Registry) -> Dict[str, Dict[str, Optional[float]]]:
    """Best MEASURED link seen for each node, from the registry's link table.

    Only rows whose trust tier is proven count. A row measured against the hub
    or against a peer both qualify — either way it is evidence about how well
    this node is connected to the rest of the chain.
    """
    quality: Dict[str, Dict[str, Optional[float]]] = {}
    try:
        rows = registry.list_links()
    except Exception:
        return quality
    for row in rows:
        if row.get("trust") not in PROVEN_TIERS:
            continue
        bw = row.get("bandwidth_bps")
        rtt = row.get("rtt_p50_ms")
        if bw is None and rtt is None:
            continue
        for nid in (row.get("src_node"), row.get("dst_node")):
            if not nid:
                continue
            slot = quality.setdefault(nid, {"bandwidth_bps": None, "rtt_p50_ms": None})
            if bw is not None and (slot["bandwidth_bps"] is None or bw > slot["bandwidth_bps"]):
                slot["bandwidth_bps"] = bw
            if rtt is not None and (slot["rtt_p50_ms"] is None or rtt < slot["rtt_p50_ms"]):
                slot["rtt_p50_ms"] = rtt
    return quality


def _order_nodes(
    candidates: List[str],
    nodes: Dict[str, Dict[str, Any]],
    quality: Dict[str, Dict[str, Optional[float]]],
) -> Tuple[List[str], str]:
    """Order the chain. Link quality first when it was measured, throughput
    otherwise — and the caller is told which, in the plan's reason string."""
    linked = [nid for nid in candidates if nid in quality]
    if linked:
        basis = "nodes ordered by measured link quality"

        def key(nid: str) -> Tuple[float, float, float, float]:
            q = quality.get(nid) or {}
            bw = q.get("bandwidth_bps")
            rtt = q.get("rtt_p50_ms")
            return (
                -(bw if bw is not None else 0.0),
                rtt if rtt is not None else float("inf"),
                -(nodes[nid].get("gflops") or 0.0),
                -float(nodes[nid]["free_bytes"]),
            )

    else:
        basis = "no measured link data; nodes ordered by measured throughput"

        def key(nid: str) -> Tuple[float, float, float, float]:
            return (
                -(nodes[nid].get("gflops") or 0.0),
                -float(nodes[nid]["free_bytes"]),
                0.0,
                0.0,
            )

    return sorted(candidates, key=key), basis


def _segment_ms(flops: float, gflops: Optional[float]) -> Optional[float]:
    if not gflops or gflops <= 0:
        return None
    return flops / (gflops * 1e9) * 1000.0


def _single_node_plan(
    model_name: str,
    stages: List[ModelStage],
    nid: str,
    nodes: Dict[str, Dict[str, Any]],
    total_mem: int,
) -> PipelinePlan:
    gflops = nodes[nid].get("gflops")
    assignments = [
        StageAssignment(
            stage=s.name,
            node_id=nid,
            fits_mem=True,
            est_ms=_segment_ms(s.flops, gflops),
        )
        for s in stages
    ]
    reason = (
        f"model fits on '{nid}' alone ({total_mem} bytes needed, "
        f"{nodes[nid]['free_bytes']} bytes measured free); not sharding: "
        "sharding buys capacity, never latency"
    )
    return _finalize(model_name, reason, assignments)


def _finalize(model_name: str, reason: str, assignments: List[StageAssignment]) -> PipelinePlan:
    """Attach both timings. A single unknown stage time makes both unknown —
    a partial sum would read like a measurement and it is not one."""
    known = all(a.est_ms is not None for a in assignments)
    latency_ms: Optional[float] = None
    bottleneck_ms: Optional[float] = None
    throughput: Optional[float] = None
    if known:
        latency_ms = sum(a.est_ms or 0.0 for a in assignments)
        per_node: Dict[str, float] = {}
        for a in assignments:
            per_node[a.node_id] = per_node.get(a.node_id, 0.0) + (a.est_ms or 0.0)
        bottleneck_ms = max(per_node.values()) if per_node else None
        if bottleneck_ms and bottleneck_ms > 0:
            throughput = 1000.0 / bottleneck_ms
    return PipelinePlan(
        model_name=model_name,
        feasible=True,
        reason=reason,
        assignments=assignments,
        estimated_end_to_end_ms=latency_ms,
        bottleneck_ms=bottleneck_ms,
        latency_ms=latency_ms,
        throughput_per_s=throughput,
    )


def plan_pipeline(registry: Registry, model_name: str, stages: List[ModelStage]) -> PipelinePlan:
    """Map an ordered stage list onto measured nodes, minimizing the bottleneck.

    Fail-closed: an empty model, a fleet with no measured free memory, or a
    stage/segment that cannot fit measured free memory all yield an infeasible
    plan that says exactly what was short and by how many bytes.
    """
    if not stages:
        return PipelinePlan(model_name=model_name, feasible=False, reason="empty model")

    nodes = _node_resources(registry)
    candidates = [
        nid for nid, r in nodes.items() if r.get("free_bytes") is not None and r["free_bytes"] > 0
    ]
    if not candidates:
        return PipelinePlan(
            model_name=model_name,
            feasible=False,
            reason="no nodes with measured free memory",
        )

    ordered, basis = _order_nodes(candidates, nodes, _link_quality(registry))

    # Hard filter, checked stage by stage first so the reason can name the
    # offending stage rather than blaming the model as a whole.
    largest_free = max(int(nodes[nid]["free_bytes"]) for nid in ordered)
    for stage in stages:
        if stage.peak_mem_bytes > largest_free:
            return PipelinePlan(
                model_name=model_name,
                feasible=False,
                reason=(
                    f"stage '{stage.name}' needs {stage.peak_mem_bytes} bytes; largest measured "
                    f"free memory is {largest_free} bytes (short by "
                    f"{stage.peak_mem_bytes - largest_free} bytes)"
                ),
            )

    # Never shard what fits whole: sharding costs latency always, and buys only
    # capacity. Pick the fastest node that can hold the entire model.
    total_mem = sum(s.peak_mem_bytes for s in stages)
    whole_fit = [nid for nid in ordered if int(nodes[nid]["free_bytes"]) >= total_mem]
    if whole_fit:
        best = max(
            whole_fit,
            key=lambda nid: (
                nodes[nid].get("gflops") or 0.0,
                float(nodes[nid]["free_bytes"]),
            ),
        )
        return _single_node_plan(model_name, stages, best, nodes, total_mem)

    # --- exact min-max contiguous partition -------------------------------
    n_stages = len(stages)
    n_nodes = len(ordered)
    pre_mem = [0] * (n_stages + 1)
    pre_flops = [0.0] * (n_stages + 1)
    for i, s in enumerate(stages):
        pre_mem[i + 1] = pre_mem[i] + s.peak_mem_bytes
        pre_flops[i + 1] = pre_flops[i] + s.flops

    # dp[i][j]: best state covering the first j stages using the first i nodes.
    dp: List[List[Optional[_State]]] = [[None] * (n_stages + 1) for _ in range(n_nodes + 1)]
    back: List[List[int]] = [[0] * (n_stages + 1) for _ in range(n_nodes + 1)]
    dp[0][0] = _ZERO
    for i in range(1, n_nodes + 1):
        nid = ordered[i - 1]
        free = int(nodes[nid]["free_bytes"])
        gflops = nodes[nid].get("gflops")
        for j in range(n_stages + 1):
            best_state: Optional[_State] = None
            best_k = j
            # k descends: the segment [k, j) only grows as k shrinks, so the
            # first k whose segment overflows `free` ends the useful range.
            for k in range(j, -1, -1):
                if pre_mem[j] - pre_mem[k] > free:
                    break
                prev = dp[i - 1][k]
                if prev is None:
                    continue
                if k == j:
                    seg: _State = _ZERO
                else:
                    ms = _segment_ms(pre_flops[j] - pre_flops[k], gflops)
                    seg = (1, 0.0) if ms is None else (0, ms)
                cand = _combine(prev, seg)
                if best_state is None or cand < best_state:
                    best_state = cand
                    best_k = k
            dp[i][j] = best_state
            back[i][j] = best_k

    if dp[n_nodes][n_stages] is None:
        reachable = max((j for j in range(n_stages + 1) if dp[n_nodes][j] is not None), default=0)
        blocked = stages[min(reachable, n_stages - 1)]
        total_free = sum(int(nodes[nid]["free_bytes"]) for nid in ordered)
        return PipelinePlan(
            model_name=model_name,
            feasible=False,
            reason=(
                f"stage '{blocked.name}' cannot be placed: the model needs {total_mem} bytes across "
                f"{n_stages} contiguous segments, but {n_nodes} node(s) offer only {total_free} bytes "
                f"of measured free memory (short by {max(total_mem - total_free, 0)} bytes)"
            ),
        )

    # Walk the DP back into contiguous segments, hindmost node first.
    segments: List[Tuple[str, int, int]] = []
    j = n_stages
    for i in range(n_nodes, 0, -1):
        k = back[i][j]
        if k < j:
            segments.append((ordered[i - 1], k, j))
        j = k
    segments.reverse()

    assignments: List[StageAssignment] = []
    for nid, start, end in segments:
        gflops = nodes[nid].get("gflops")
        for s in stages[start:end]:
            assignments.append(
                StageAssignment(
                    stage=s.name,
                    node_id=nid,
                    fits_mem=True,
                    est_ms=_segment_ms(s.flops, gflops),
                )
            )

    reason = (
        f"min-max contiguous partition over {len(segments)} of {n_nodes} nodes "
        f"({basis}); every segment fits measured free memory"
    )
    return _finalize(model_name, reason, assignments)
