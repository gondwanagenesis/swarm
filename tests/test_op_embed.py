"""The `embed` op — the swarm's first non-toy workload.

`primesum` and `hashwork` burn cycles to prove the scheduler works. This one
runs a real model, which makes two properties load-bearing rather than
academic:

- **Determinism.** Content-addressed results and lease requeue both assume a
  task re-run elsewhere yields the same answer. Embeddings are deterministic
  per (model, text), so that holds — but it is asserted here, not assumed.
- **Failing closed.** A node with no runtime must raise, not return zeros. A
  plausible-looking vector no model produced is precisely the lie this
  system exists not to tell.

Runtime-dependent tests skip when no local runtime is present — CI has none.
"""

from __future__ import annotations

import pytest

from swarm.agent.ops import OPS, embed_runtime_available, op_embed

_RUNTIME = embed_runtime_available()
needs_runtime = pytest.mark.skipif(
    _RUNTIME is None, reason="no local embedding runtime on this machine"
)


def test_embed_is_registered():
    assert "embed" in OPS


def test_missing_text_is_rejected():
    with pytest.raises(ValueError):
        op_embed({})
    with pytest.raises(ValueError):
        op_embed({"text": ""})


def test_no_runtime_raises_rather_than_faking(monkeypatch):
    """The whole point: no runtime means no answer, never a synthetic one."""
    monkeypatch.setattr("swarm.agent.ops.embed_runtime_available", lambda *a, **k: None)
    with pytest.raises(RuntimeError) as exc:
        op_embed({"text": "hello"})
    assert "no local embedding runtime" in str(exc.value)


def test_unknown_model_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "swarm.agent.ops.embed_runtime_available",
        lambda *a, **k: {"runtime": "ollama", "url": "http://x", "models": ["real:latest"]},
    )
    with pytest.raises(RuntimeError) as exc:
        op_embed({"text": "hello", "model": "invented:latest"})
    assert "not present here" in str(exc.value)


@needs_runtime
def test_embed_returns_a_real_vector():
    out = op_embed({"text": "measured, not declared"})
    assert out["dims"] > 0
    assert len(out["embedding"]) == out["dims"]
    assert any(v != 0 for v in out["embedding"]), "an all-zero vector is a failure, not a result"


@needs_runtime
def test_embed_is_deterministic():
    """Idempotency and content addressing depend on this."""
    a = op_embed({"text": "the same text", "dims_only": True})
    b = op_embed({"text": "the same text", "dims_only": True})
    assert a["checksum"] == b["checksum"]


@needs_runtime
def test_different_text_gives_different_vector():
    a = op_embed({"text": "alpha", "dims_only": True})
    b = op_embed({"text": "beta", "dims_only": True})
    assert a["checksum"] != b["checksum"]


@needs_runtime
def test_embed_reports_attribution():
    """Law 5: the result must say what actually produced it."""
    out = op_embed({"text": "who did this work?", "dims_only": True})
    assert out["tier"] == "ollama_local"
    assert out["device"] == "runtime:ollama"
    assert out["model"]
    assert out["elapsed_s"] >= 0


@needs_runtime
def test_dims_only_omits_the_vector():
    """Bulk embedding jobs should not have to ship every vector back."""
    out = op_embed({"text": "compact", "dims_only": True})
    assert "embedding" not in out
    assert out["dims"] > 0 and out["checksum"]
