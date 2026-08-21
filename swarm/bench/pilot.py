"""The pilot run — the sniff. Cheap, bounded, honest.

Purpose: answer "is this tissue worth investing in?" in ~200ms, BEFORE we
spend seconds on a full bench or dollars on adapter synthesis. The pilot only
runs what needs no runtime binding (host CPU/memory). A GPU without a
discovered binding gets NO pilot number — the honest answer is "can't pilot
it," not a fabrication.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from ..core.models import Anomaly

PILOT_INNER = 8_000
PILOT_SAMPLES = 3


def pilot_cpu_fp32() -> Dict[str, Any]:
    """Sub-second FP32 sniff. Pure stdlib. Runs everywhere."""
    anomalies = []
    acc = 1.0000001
    for _ in range(2):
        a = acc
        for _ in range(PILOT_INNER):
            a = a * 1.0000001 + 0.0000001
        acc = a
    samples = []
    for _ in range(PILOT_SAMPLES):
        t0 = time.perf_counter()
        a = acc
        for _ in range(PILOT_INNER):
            a = a * 1.0000001 + 0.0000001
        samples.append(time.perf_counter() - t0)
        acc = a
    if not samples:
        return {"score_gflops": None, "anomalies": [Anomaly("pilot.fp32", "no samples")]}
    best = min(samples)
    score = (PILOT_INNER * 2) / best / 1e9 if best > 0 else None
    return {
        "score_gflops": score,
        "duration_s": sum(samples),
        "trust": "fallback",
        "anomalies": anomalies,
    }
