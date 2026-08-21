"""Chunk planner: how much work a node may pull, and for how long it's trusted.

chunk = BASE * (measured_throughput / baseline) * confidence, then clamped.
confidence comes from the benchmark's own trust tier and variance, shrunk
further by the node's observed execution variance (EWMA). New nodes start at
the confidence floor — they earn bigger chunks by completing work.

Near the end of a bag the chunk shrinks toward the minimum (guided
scheduling): the last chunk must not land as a giant on the slowest node.

The lease is 3x predicted duration + 30s (spec): three margins of execution
variance plus a floor. Predicted duration comes from the node's EWMA of
observed per-item times; unknown until it has completed work.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

from .queue import (
    BASE_CHUNK,
    DEFAULT_MAX_CHUNK,
    DEFAULT_MIN_CHUNK,
    NEW_NODE_CONFIDENCE_FLOOR,
)
from .registry import Registry

DEFAULT_PREDICTED_MS_PER_ITEM = 1000.0
TAIL_FRACTION = 0.15


def _capability_stats(registry: Registry, node_id: str, kind: str) -> Tuple[Optional[float], float]:
    benches = [b for b in registry.latest_benches(node_id) if b["name"] == kind or b["name"].endswith(kind)]
    if not benches:
        return None, 0.0
    bench = max(benches, key=lambda b: b["at"])
    value = bench["value"]
    if value is None:
        return None, 0.0
    from ..core.models import MeasurementTrust

    try:
        trust = MeasurementTrust(bench["trust"])
    except ValueError:
        trust = MeasurementTrust.THEORETICAL
    from ..core.models import TRUST_WEIGHT

    conf = TRUST_WEIGHT[trust]
    variance = bench["variance"]
    if variance is not None and variance >= 0 and value:
        cv = math.sqrt(variance) / abs(value)
        conf *= max(0.5, 1.0 - min(cv, 0.5))
    return value, conf


def _track_factor(stats: Dict[str, Any]) -> float:
    samples = int(stats.get("samples") or 0)
    if samples < 3:
        return NEW_NODE_CONFIDENCE_FLOOR if samples == 0 else 0.6
    mean = stats.get("ewma_ms_per_item") or 0.0
    var = stats.get("ewma_var") or 0.0
    if mean <= 0:
        return 0.6
    cv = math.sqrt(max(var, 0.0)) / mean
    return max(NEW_NODE_CONFIDENCE_FLOOR, min(1.0, 1.0 - min(cv, 0.8)))


class ChunkPlanner:
    def __init__(self, registry: Registry, ops_to_kind: Optional[Dict[str, str]] = None) -> None:
        self.registry = registry
        self.ops_to_kind = ops_to_kind or {}
        self.active_workers: Dict[str, float] = {}

    def baseline_throughput(self, kind: str) -> float:
        best = 0.0
        for node in self.registry.list_nodes():
            value, _ = _capability_stats(self.registry, node["node_id"], kind)
            if value and value > best:
                best = value
        return best

    def plan(
        self,
        node_id: str,
        op: str,
        bag_total: int,
        bag_remaining: int,
        queue_stats: Dict[str, Any],
        active_worker_count: int,
    ) -> Tuple[int, float, float]:
        kind = self.ops_to_kind.get(op, "cpu_fp32_gflops")
        throughput, conf = _capability_stats(self.registry, node_id, kind)
        conf *= _track_factor(queue_stats)
        baseline = self.baseline_throughput(kind)

        if throughput is None or baseline <= 0:
            raw = BASE_CHUNK * conf
        else:
            raw = BASE_CHUNK * (throughput / baseline) * conf
        chunk = int(max(DEFAULT_MIN_CHUNK, min(DEFAULT_MAX_CHUNK, round(raw))))

        if bag_total > 0 and bag_remaining <= bag_total * TAIL_FRACTION and bag_remaining > 0:
            tail_share = max(1, bag_remaining // max(1, 2 * max(1, active_worker_count)))
            chunk = min(chunk, max(DEFAULT_MIN_CHUNK, tail_share))

        predicted_ms = queue_stats.get("ewma_ms_per_item") or DEFAULT_PREDICTED_MS_PER_ITEM
        lease = 3.0 * chunk * predicted_ms / 1000.0 + 30.0
        return chunk, lease, predicted_ms

    def touch_worker(self, node_id: str) -> None:
        import time

        self.active_workers[node_id] = time.time()

    def active_count(self, within_s: float = 120.0) -> int:
        import time

        now = time.time()
        return sum(1 for ts in self.active_workers.values() if now - ts < within_s)
