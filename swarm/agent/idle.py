"""The idle loop: spare cycles serve self-knowledge.

Ladder, cheap to expensive, welfare-gated at every rung (a host that needs
the machine gets silence from us):
  1. re-climb the capability tower (tools/packages may have appeared)
  2. re-run the pilot (cheap variance shrink toward confidence)
  3. after enough pilot history, re-run the full floor bench (fresh proof)

Each level publishes its fresh measurements to the hub so the fleet's picture
of you sharpens over time. This is the organism getting to know its own body
in its spare time.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from ..bench.fallback import run_floor_benchmarks
from ..bench.pilot import pilot_cpu_fp32
from ..core.serde import to_dict


class IdleHone:
    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        self.rung_times: List[float] = []
        self.history: List[float] = []
        self.last_bench_at = 0.0

    def sharpen(self, agent: Any) -> Dict[str, Any]:
        """One ladder step. Runs only when the agent calls it during idle;
        still checks welfare itself — defense in depth."""
        from .welfare import welfare_gate

        gate = welfare_gate()
        if not gate["allowed"]:
            return {"ran": None, "reason": gate["reason"]}

        from ..probe.self_probe import climb_tower

        cap, anomalies = climb_tower(bench_instrument=False)
        rung = "tower"
        payload: Dict[str, Any] = {"node_id": self.node_id, "rung": rung, "capability": to_dict(cap)}

        pilot = pilot_cpu_fp32()
        if pilot.get("score_gflops") is not None:
            self.history.append(pilot["score_gflops"])
        payload["pilot"] = pilot
        rung = "pilot"

        # every N successful pilots, upgrade trust with a fresh full bench
        if len(self.history) >= 10 and (time.time() - self.last_bench_at) > 3600.0:
            benches = run_floor_benchmarks()
            payload["benchmarks"] = [to_dict(b) for b in benches]
            self.last_bench_at = time.time()
            rung = "bench"

        posted = agent._post("/api/sharpen", payload) if hasattr(agent, "_post") else None
        return {
            "ran": rung,
            "hub_ack": bool(posted and posted.get("ok")),
            "anomalies": [to_dict(a) for a in anomalies],
        }
