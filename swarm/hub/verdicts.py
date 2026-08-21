"""The worth-it gate. Before the organism invests anything — bench time, LLM
dollars, scheduler slots — a device earns one of three verdicts, recorded
with the reason so nothing is a black box:

  adopt_now     an existing runtime binds it; no new code needed (Law 3)
  synthesize    worth sending to the integrator (M4) for an adapter
  park          not worth it right now — visible on the dashboard, never hidden

Signals: Tier-0 runtime bindings, pilot score (if one ran), node battery
state (a discharging phone doesn't count even if the silicon is decent),
whether a proven adapter already covers the class.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

ADOPT = "adopt_now"
SYNTHESIZE = "synthesize"
PARK = "park"


def decide_verdict(
    device_class: str,
    bindings: List[Dict[str, Any]],
    pilot_score_gflops: Optional[float],
    battery_trust: Optional[str],
    battery_watts: Optional[float],
    covered_classes: set,
) -> tuple:
    """Returns (verdict, reason). Deterministic and explainable."""
    if device_class in covered_classes:
        return ADOPT, f"class {device_class} already has a proven adapter"

    bound = [b for b in bindings if b.get("runtime")]
    if bound:
        best = max(bindings, key=lambda b: b.get("confidence") or 0.0)
        if best.get("runtime"):
            return (
                ADOPT,
                f"existing runtime binds it: {best['runtime']} (confidence {best.get('confidence')})",
            )

    if (
        battery_trust == "battery"
        and battery_watts is not None
        and battery_watts > 0
        and pilot_score_gflops is not None
        and pilot_score_gflops < 0.05
    ):
        return PARK, "battery-powered and pilot below floor; participation parked while discharging"

    if pilot_score_gflops is None:
        return PARK, "no runtime binding and no pilot possible — nothing measured, nothing invested"

    FLEET_FLOOR_GFLOPS = 0.001
    if pilot_score_gflops >= FLEET_FLOOR_GFLOPS:
        return SYNTHESIZE, f"pilot {pilot_score_gflops:.4f} GFLOPS clears floor; queue for integrator"
    return PARK, f"pilot {pilot_score_gflops:.4f} GFLOPS below floor; parked"
