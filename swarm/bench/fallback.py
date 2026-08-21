"""Pure-stdlib fallback benchmarks. The floor of the capability tower.

These are rough instruments — a pure-Python FP32 loop is orders of magnitude
below any vectorized path, and that is fine: every result is tagged
trust=FALLBACK so the scheduler (and the operator) knows exactly how much
weight to put on it. A rough number with an honest trust tag beats None,
and None beats a plausible-looking lie.

Each benchmark: warmup excluded, N timed samples, median reported, variance
kept, benchmark_run_id attached. Never raises.
"""

from __future__ import annotations

import statistics
import time
import uuid
from typing import List, Optional

from ..core.models import Anomaly, BenchResult, MeasurementTrust

FP32_INNER = 20_000
FP32_SAMPLES = 7
BANDWIDTH_BYTES = 512 * 1024 * 1024
BANDWIDTH_SAMPLES = 5
LATENCY_SLOTS = 1 << 20
LATENCY_STEPS = 4_000_000


def _new_run_id() -> str:
    return "run-" + uuid.uuid4().hex[:16]


def _finish(
    name: str,
    values: List[float],
    unit: str,
    duration_s: float,
    anomalies: List[Anomaly],
    burst: Optional[float] = None,
    sustained: Optional[float] = None,
) -> BenchResult:
    if not values:
        anomalies.append(Anomaly(f"bench.{name}", "no samples produced", "error"))
        return BenchResult(
            name=name,
            value=None,
            unit=unit,
            trust=MeasurementTrust.FALLBACK,
            benchmark_run_id=_new_run_id(),
            duration_s=duration_s,
            anomalies=anomalies,
        )
    try:
        median = statistics.median(values)
        variance = statistics.variance(values) if len(values) >= 2 else 0.0
    except Exception as exc:
        anomalies.append(
            Anomaly(f"bench.{name}", f"aggregation failed: {exc}", "error")
        )
        return BenchResult(
            name=name,
            value=None,
            unit=unit,
            trust=MeasurementTrust.FALLBACK,
            samples=[round(v, 4) for v in values],
            benchmark_run_id=_new_run_id(),
            duration_s=duration_s,
            burst=burst,
            sustained=sustained,
            anomalies=anomalies,
        )
    return BenchResult(
        name=name,
        value=median,
        unit=unit,
        trust=MeasurementTrust.FALLBACK,
        samples=[round(v, 4) for v in values],
        variance=variance,
        benchmark_run_id=_new_run_id(),
        duration_s=duration_s,
        burst=burst,
        sustained=sustained,
        anomalies=anomalies,
    )


def fp32_gflops(samples: int = FP32_SAMPLES) -> BenchResult:
    anomalies: List[Anomaly] = []
    started = time.perf_counter()
    values: List[float] = []
    try:
        acc = 1.0000001
        for _ in range(2):
            a = acc
            for _ in range(FP32_INNER):
                a = a * 1.0000001 + 0.0000001
            acc = a
        for _ in range(max(1, samples)):
            t0 = time.perf_counter()
            a = acc
            for _ in range(FP32_INNER):
                a = a * 1.0000001 + 0.0000001
            dt = time.perf_counter() - t0
            acc = a
            if dt > 0:
                flops = FP32_INNER * 2
                values.append(flops / dt / 1e9)
    except Exception as exc:
        anomalies.append(Anomaly("bench.fp32", f"crashed: {exc}", "error"))
    duration = time.perf_counter() - started
    result = _finish("cpu_fp32_gflops", values, "GFLOPS", duration, anomalies)
    if values and len(values) >= 3:
        result.burst = statistics.median(values[: max(1, len(values) // 3)])
        result.sustained = statistics.median(values[-max(1, len(values) // 3) :])
    return result


def memory_bandwidth(
    samples: int = BANDWIDTH_SAMPLES, size: int = BANDWIDTH_BYTES
) -> BenchResult:
    anomalies: List[Anomaly] = []
    started = time.perf_counter()
    values: List[float] = []
    try:
        buf = bytearray(size)
        try:
            memoryview(buf)[:: 8 * 1024 * 1024] = b"\x01" * len(
                memoryview(buf)[:: 8 * 1024 * 1024]
            )
        except (ValueError, IndexError):
            buf[: 1024 * 1024] = b"\x01" * (1024 * 1024)
        total = 0
        for _ in range(max(1, samples)):
            t0 = time.perf_counter()
            total = sum(buf)
            dt = time.perf_counter() - t0
            if dt > 0:
                values.append(size / dt / (1024**3))
        if total == -1:
            anomalies.append(Anomaly("bench.membw", "unreachable", "info"))
    except MemoryError:
        anomalies.append(
            Anomaly("bench.membw", "allocation failed; host memory exhausted", "error")
        )
    except Exception as exc:
        anomalies.append(Anomaly("bench.membw", f"crashed: {exc}", "error"))
    duration = time.perf_counter() - started
    return _finish("mem_bandwidth_gbps", values, "GB/s", duration, anomalies)


def memory_latency(
    steps: int = LATENCY_STEPS, slots: int = LATENCY_SLOTS
) -> BenchResult:
    anomalies: List[Anomaly] = []
    started = time.perf_counter()
    values: List[float] = []
    try:
        jump = 4099
        chain = [(i + jump) % slots for i in range(slots)]
        idx = 0
        for _i in range(0, slots, jump):
            idx = chain[idx]
        idx = 0
        t0 = time.perf_counter()
        for _ in range(steps):
            idx = chain[idx]
        dt = time.perf_counter() - t0
        if idx == -1:
            anomalies.append(Anomaly("bench.latency", "unreachable", "info"))
        values.append(dt / steps * 1e9)
    except MemoryError:
        anomalies.append(Anomaly("bench.latency", "allocation failed", "error"))
    except Exception as exc:
        anomalies.append(Anomaly("bench.latency", f"crashed: {exc}", "error"))
    duration = time.perf_counter() - started
    return _finish("mem_latency_ns", values, "ns", duration, anomalies)


def run_floor_benchmarks() -> List[BenchResult]:
    """The trust-tagged floor suite: always runnable on bare CPython."""
    return [fp32_gflops(), memory_bandwidth(), memory_latency()]
