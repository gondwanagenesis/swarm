from swarm.bench import fallback
from swarm.core.models import MeasurementTrust


def test_fp32_produces_honest_number():
    result = fallback.fp32_gflops(samples=3)
    assert result.trust is MeasurementTrust.FALLBACK
    assert result.benchmark_run_id.startswith("run-")
    assert result.value is not None and result.value > 0
    assert result.unit == "GFLOPS"
    assert result.variance is not None and result.variance >= 0
    assert result.sustained_ratio is not None


def test_memory_bandwidth_small_size():
    result = fallback.memory_bandwidth(samples=2, size=8 * 1024 * 1024)
    assert result.trust is MeasurementTrust.FALLBACK
    assert result.value is not None and result.value > 0
    assert result.unit == "GB/s"


def test_memory_latency_small():
    result = fallback.memory_latency(steps=20_000, slots=1 << 12)
    assert result.trust is MeasurementTrust.FALLBACK
    assert result.value is not None and result.value > 0
    assert result.unit == "ns"


def test_never_raises_on_crash(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated")

    monkeypatch.setattr(fallback, "_finish", fallback._finish)
    import statistics

    monkeypatch.setattr(statistics, "median", boom)
    result = fallback.fp32_gflops(samples=1)
    assert result is not None
