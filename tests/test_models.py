"""Law 1 tests: profiles describe, capabilities prove, and the two never mix."""

import pytest

from swarm.core.models import (
    TRUST_WEIGHT,
    BenchResult,
    MeasurementTrust,
    NodeCapability,
    NodeProfile,
    TaskSpec,
)


def test_profile_and_capability_share_no_field_names():
    profile_fields = {f.name for f in NodeProfile.__dataclass_fields__.values()}
    capability_fields = {f.name for f in NodeCapability.__dataclass_fields__.values()}
    overlap = profile_fields & capability_fields
    assert overlap == {"node_id"}, f"unexpected overlap: {overlap}"


def test_verified_requires_benchmark_run_id():
    with pytest.raises(ValueError):
        NodeCapability(
            node_id="n1",
            kind="cpu_fp32_gflops",
            value=1.0,
            unit="GFLOPS",
            trust=MeasurementTrust.VERIFIED,
        )


def test_non_verified_may_lack_run_id():
    cap = NodeCapability(
        node_id="n1", kind="cpu_fp32_gflops", trust=MeasurementTrust.FALLBACK
    )
    assert cap.benchmark_run_id == ""


def test_trust_weights_are_explicit_and_ordered():
    assert (
        TRUST_WEIGHT[MeasurementTrust.VERIFIED]
        > TRUST_WEIGHT[MeasurementTrust.CALIBRATED]
        > TRUST_WEIGHT[MeasurementTrust.STANDARD]
        > TRUST_WEIGHT[MeasurementTrust.FALLBACK]
        > TRUST_WEIGHT[MeasurementTrust.THEORETICAL]
    )


def test_bench_confidence_zero_without_value():
    bench = BenchResult(name="x", value=None, trust=MeasurementTrust.FALLBACK)
    assert bench.confidence == 0.0


def test_bench_confidence_penalized_by_variance_but_bounded():
    clean = BenchResult(
        name="x",
        value=10.0,
        trust=MeasurementTrust.STANDARD,
        samples=[10.0, 10.0],
        variance=0.0,
    )
    noisy = BenchResult(
        name="x",
        value=10.0,
        trust=MeasurementTrust.STANDARD,
        samples=[1.0, 19.0],
        variance=162.0,
    )
    assert clean.confidence > noisy.confidence
    assert noisy.confidence >= TRUST_WEIGHT[MeasurementTrust.STANDARD] * 0.5 - 1e-4


def test_sustained_ratio():
    bench = BenchResult(name="x", value=1.0, burst=10.0, sustained=5.0)
    assert bench.sustained_ratio == 0.5
    empty = BenchResult(name="x", value=1.0)
    assert empty.sustained_ratio is None


def test_stddev_is_derived_not_stored():
    bench = BenchResult(name="x", value=1.0, variance=4.0)
    assert bench.stddev == 2.0


def test_task_idem_key_is_content_addressed():
    a = TaskSpec(task_id="t1", op="infer", input_refs=["a", "b"], params={"temp": "0"})
    b = TaskSpec(task_id="t2", op="infer", input_refs=["a", "b"], params={"temp": "0"})
    c = TaskSpec(task_id="t3", op="infer", input_refs=["a", "b"], params={"temp": "1"})
    assert a.idem_key == b.idem_key
    assert a.idem_key != c.idem_key
