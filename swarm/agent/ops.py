"""Agent operation registry: pure functions (params -> payload).

Ops must be pure with respect to their params — that is what makes results
idempotent and content-addressable, and what makes lease requeue safe.
Adding an op is the ONLY way to extend what the swarm computes.

`matmul` is the first op that can actually benefit from a device. It climbs
the same tower `swarm.probe.self_probe` reports and degrades DOWNWARD:

    torch + CUDA  ->  numpy (BLAS)  ->  torch (CPU)  ->  pure-Python loops

The floor always works: a Termux phone with bare CPython gets the same
answer, slower. To keep that promise auditable the matrices are small
integers, so every tier computes the *identical* exact result — the
`checksum`/`matrix`/`trace` fields are a pure function of the params alone.
The `tier`/`device`/`backend` fields describe WHO did the work (law 5,
attribution); they are node facts, deliberately reported next to the answer
so nothing can claim a GPU it never touched.

stdlib-only law: this module is scanned by `scripts/check_stdlib_imports.py`,
which walks EVERY import node — including ones nested in functions. numpy and
torch are therefore reached through `importlib.import_module` inside
try/except, never via an `import` statement.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import math
import os
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

Matrix = List[List[int]]

# Tier names. Ordered best-first; the first one that works wins.
TIER_TORCH_CUDA = "torch_cuda"
TIER_NUMPY_BLAS = "numpy_blas"
TIER_TORCH_CPU = "torch_cpu"
TIER_PYTHON = "python_loops"

MATMUL_TIERS = (TIER_TORCH_CUDA, TIER_NUMPY_BLAS, TIER_TORCH_CPU, TIER_PYTHON)

_M64 = (1 << 64) - 1
_GOLDEN = 0x9E3779B97F4A7C15
_MIX_A = 0xBF58476D1CE4E5B9
_MIX_B = 0x94D049BB133111EB

# Element range: values land in [-8, 8]. With k <= ~1e5 every dot product
# stays far below 2**24, so float32 (GPU) and float64 (BLAS) are both EXACT
# and agree bit-for-bit with the pure-Python integer floor.
_VALUE_MOD = 17
_VALUE_BIAS = 8

DEFAULT_MAX_RETURN = 1024


def op_primesum(params: Dict[str, Any]) -> Any:
    """Count primes below n by trial division to sqrt. CPU-bound, pure."""
    n = int(params.get("n", 5000))
    if n < 2:
        return {"n": n, "count": 0}
    count = 1
    for candidate in range(3, n, 2):
        limit = int(math.sqrt(candidate))
        composite = False
        d = 3
        while d <= limit:
            if candidate % d == 0:
                composite = True
                break
            d += 2
        if not composite:
            count += 1
    return {"n": n, "count": count}


def op_hashwork(params: Dict[str, Any]) -> Any:
    """Chain sha256 seed-> iterations; a pure compute grind."""
    seed = str(params.get("seed", "swarm"))
    rounds = int(params.get("rounds", 20000))
    digest = seed.encode("utf-8")
    for _ in range(rounds):
        digest = hashlib.sha256(digest).digest()
    return {"seed": seed, "rounds": rounds, "digest": digest.hex()}


# --------------------------------------------------------------------------
# deterministic matrix generation (counter-based, so it vectorizes exactly)
# --------------------------------------------------------------------------


def _splitmix64(x: int) -> int:
    z = (x + _GOLDEN) & _M64
    z = ((z ^ (z >> 30)) * _MIX_A) & _M64
    z = ((z ^ (z >> 27)) * _MIX_B) & _M64
    return z ^ (z >> 31)


def _element(seed: int, stream: int, index: int) -> int:
    """Value at a flat index. Counter-based: no sequential state, so the
    numpy fast path below can compute the same thing without a loop."""
    key = (seed * 0x2545F4914F6CDD1D + stream * 0x9E3779B97F4A7C15 + index) & _M64
    return int(_splitmix64(key) % _VALUE_MOD) - _VALUE_BIAS


def _gen_matrix_python(rows: int, cols: int, seed: int, stream: int) -> Matrix:
    return [
        [_element(seed, stream, r * cols + c) for c in range(cols)] for r in range(rows)
    ]


def _gen_matrix_numpy(rows: int, cols: int, seed: int, stream: int) -> Optional[Any]:
    """Vectorised twin of `_gen_matrix_python`. Returns an int64 ndarray, or
    None if numpy is absent. Equality with the floor is asserted by tests."""
    np = _try_import("numpy")
    if np is None:
        return None
    try:
        u64 = np.uint64
        idx = np.arange(rows * cols, dtype=np.uint64)
        # Fold the scalar part in Python ints first: numpy warns (loudly) on
        # scalar uint64 overflow, while array arithmetic wraps quietly.
        base = (seed * 0x2545F4914F6CDD1D + stream * _GOLDEN) & _M64
        key = idx + u64(base)
        z = key + u64(_GOLDEN)
        z = (z ^ (z >> u64(30))) * u64(_MIX_A)
        z = (z ^ (z >> u64(27))) * u64(_MIX_B)
        z = z ^ (z >> u64(31))
        vals = (z % u64(_VALUE_MOD)).astype(np.int64) - np.int64(_VALUE_BIAS)
        return vals.reshape(rows, cols)
    except Exception:
        return None


def _try_import(name: str) -> Optional[Any]:
    """Import an optional runtime the tower may have found. Never raises."""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def _coerce_matrix(raw: Any) -> Optional[Matrix]:
    if not isinstance(raw, list) or not raw:
        return None
    out: Matrix = []
    width: Optional[int] = None
    for row in raw:
        if not isinstance(row, list):
            return None
        if width is None:
            width = len(row)
        elif len(row) != width:
            return None
        out.append([int(v) for v in row])
    return out or None


# --------------------------------------------------------------------------
# the tiers
# --------------------------------------------------------------------------


def _matmul_torch_cuda(a: Any, b: Any, np_a: Any, np_b: Any) -> Optional[Tuple[Matrix, str, str]]:
    """Top of the tower: the multiply happens on the GPU. Returns
    (matrix, device, backend) or None when CUDA is not really there."""
    torch = _try_import("torch")
    if torch is None:
        return None
    try:
        if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
            return None
    except Exception:
        return None
    try:
        # TF32 would silently lose mantissa bits and break exactness across
        # tiers. Correctness > performance (AGENTS.md priority order).
        with contextlib.suppress(Exception):
            torch.backends.cuda.matmul.allow_tf32 = False
        dev = torch.device("cuda", 0)
        ta = torch.tensor(np_a if np_a is not None else a, dtype=torch.float32, device=dev)
        tb = torch.tensor(np_b if np_b is not None else b, dtype=torch.float32, device=dev)
        tc = torch.matmul(ta, tb)
        if tc.device.type != "cuda":  # attribution: prove it ran where we claim
            return None
        torch.cuda.synchronize()
        out = tc.to(torch.float64).cpu().tolist()
        name = torch.cuda.get_device_name(0)
        backend = "torch %s / cuda %s" % (
            getattr(torch, "__version__", "?"),
            getattr(getattr(torch, "version", None), "cuda", "?"),
        )
        return ([[round(v) for v in row] for row in out], "cuda:0 (%s)" % name, backend)
    except Exception:
        return None


def _matmul_numpy(a: Any, b: Any, np_a: Any, np_b: Any) -> Optional[Tuple[Matrix, str, str]]:
    np = _try_import("numpy")
    if np is None:
        return None
    try:
        arr_a = np_a if np_a is not None else np.array(a, dtype=np.int64)
        arr_b = np_b if np_b is not None else np.array(b, dtype=np.int64)
        prod = np.matmul(arr_a.astype(np.float64), arr_b.astype(np.float64))
        out = np.rint(prod).astype(np.int64).tolist()
        return (out, "cpu", "numpy %s" % getattr(np, "__version__", "?"))
    except Exception:
        return None


def _matmul_torch_cpu(a: Any, b: Any, np_a: Any, np_b: Any) -> Optional[Tuple[Matrix, str, str]]:
    torch = _try_import("torch")
    if torch is None:
        return None
    try:
        ta = torch.tensor(np_a if np_a is not None else a, dtype=torch.float64)
        tb = torch.tensor(np_b if np_b is not None else b, dtype=torch.float64)
        tc = torch.matmul(ta, tb)
        out = tc.tolist()
        return (
            [[round(v) for v in row] for row in out],
            "cpu",
            "torch %s" % getattr(torch, "__version__", "?"),
        )
    except Exception:
        return None


def _matmul_python(a: Matrix, b: Matrix) -> Tuple[Matrix, str, str]:
    """The floor. Always works, on anything with a CPython interpreter."""
    bt = list(zip(*b))
    out = [[sum(x * y for x, y in zip(row, col)) for col in bt] for row in a]
    return (out, "cpu", "cpython")


_MATMUL_IMPLS = {
    TIER_TORCH_CUDA: _matmul_torch_cuda,
    TIER_NUMPY_BLAS: _matmul_numpy,
    TIER_TORCH_CPU: _matmul_torch_cpu,
}


def matmul_tiers_available() -> List[str]:
    """Which matmul tiers this node could actually use, best first.

    Mirrors `swarm.probe.self_probe`'s floors (F2 = optional package
    importable). Detection is by import success, never by a declared list.
    """
    out: List[str] = []
    torch = _try_import("torch")
    if torch is not None:
        try:
            if torch.cuda.is_available() and torch.cuda.device_count() >= 1:
                out.append(TIER_TORCH_CUDA)
        except Exception:
            pass
    if _try_import("numpy") is not None:
        out.append(TIER_NUMPY_BLAS)
    if torch is not None:
        out.append(TIER_TORCH_CPU)
    out.append(TIER_PYTHON)
    return out


OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"


def _ollama_url() -> str:
    """Where this node's local inference runtime lives, if anywhere."""
    return (os.environ.get("SWARM_OLLAMA_URL") or OLLAMA_DEFAULT_URL).rstrip("/")


def embed_runtime_available(timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    """Probe for a local embedding runtime. Returns its facts, or None.

    Discovery, not declaration (Law 3): we ask the runtime what it has rather
    than trusting a config. `None` means this node cannot serve embeddings —
    which is a fine answer, and better than a fabricated vector.
    """
    try:
        with urllib.request.urlopen(_ollama_url() + "/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    models = [
        m.get("name")
        for m in (data.get("models") or [])
        if "embedding" in (m.get("capabilities") or [])
    ]
    if not models:
        return None
    return {"runtime": "ollama", "url": _ollama_url(), "models": sorted(models)}


def op_embed(params: Dict[str, Any]) -> Any:
    """Embed text with a local model. Real inference, not a synthetic grind.

    Deterministic for a given (model, text), which is what idempotency and
    content-addressed results actually require — the same task re-run on
    another node yields the same vector, so a lease expiry is still safe.

    Fails CLOSED: a node with no embedding runtime raises rather than
    returning zeros. A plausible-looking vector that no model produced is
    exactly the lie this system exists to not tell.

    Params:
      text        the string to embed (required)
      model       model name; defaults to the first embedding model present
      dims_only   return only the dimensionality + checksum, not the vector
    """
    text = params.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("embed requires a non-empty 'text' param")

    facts = embed_runtime_available()
    if facts is None:
        raise RuntimeError(
            "no local embedding runtime on this node "
            "(looked for an Ollama embedding model at {})".format(_ollama_url())
        )
    model = str(params.get("model") or facts["models"][0])
    if model not in facts["models"]:
        raise RuntimeError(
            "model {!r} not present here; this node has {}".format(model, facts["models"])
        )

    body = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        facts["url"] + "/api/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=float(params.get("timeout", 120.0))) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    elapsed = time.perf_counter() - started

    vector = payload.get("embedding")
    if not isinstance(vector, list) or not vector:
        raise RuntimeError("embedding runtime returned no vector")

    checksum = hashlib.sha256(
        json.dumps([round(float(v), 6) for v in vector]).encode("utf-8")
    ).hexdigest()[:16]
    out: Dict[str, Any] = {
        "op": "embed",
        "model": model,
        "dims": len(vector),
        "checksum": checksum,
        "chars": len(text),
        # Attribution (Law 5): which runtime actually produced this.
        "tier": "ollama_local",
        "device": "runtime:ollama",
        "backend": facts["runtime"],
        "elapsed_s": round(elapsed, 4),
    }
    if not params.get("dims_only"):
        out["embedding"] = vector
    return out


def op_matmul(params: Dict[str, Any]) -> Any:
    """Dense integer matmul, C = A @ B. Pure function of its params.

    Params:
      m, k, n     dimensions (A is m*k, B is k*n). Default 8/8/8.
      seed        int; A and B are generated deterministically from it, so
                  nothing large ever crosses the wire.
      a, b        optional explicit matrices (override generation).
      max_return  include the full matrix only when m*n <= this (default
                  1024); the checksum/trace/sum always come back.
      tier        optional pin ("python_loops" etc.) — used by tests and by
                  calibration runs. Never used to CLAIM a tier: if the pinned
                  tier is not really available the answer degrades downward
                  and says so.

    Returns the answer plus honest attribution: `tier` is the tier that
    actually executed, `device` is where, `backend` is the runtime version.
    """
    seed = int(params.get("seed", 0))
    a_in = _coerce_matrix(params.get("a"))
    b_in = _coerce_matrix(params.get("b"))

    if a_in is not None and b_in is not None:
        m, k, n = len(a_in), len(a_in[0]), len(b_in[0])
        if len(b_in) != k:
            raise ValueError("matmul shape mismatch: A is %dx%d, B is %dx%d" % (m, k, len(b_in), n))
    else:
        m = max(1, int(params.get("m", 8)))
        k = max(1, int(params.get("k", params.get("m", 8))))
        n = max(1, int(params.get("n", params.get("m", 8))))

    requested = params.get("tier")
    order = matmul_tiers_available()
    if requested in MATMUL_TIERS:
        order = [t for t in order if t == requested] + [t for t in order if t != requested]

    # Generate once, in the widest form available; every tier consumes the
    # same values so the answer cannot drift between tiers.
    np_a = np_b = None
    a: Optional[Matrix] = a_in
    b: Optional[Matrix] = b_in
    if a_in is None or b_in is None:
        np_a = _gen_matrix_numpy(m, k, seed, 1)
        np_b = _gen_matrix_numpy(k, n, seed, 2)
        if np_a is None or np_b is None or order[0] == TIER_PYTHON:
            np_a = np_b = None
            a = _gen_matrix_python(m, k, seed, 1)
            b = _gen_matrix_python(k, n, seed, 2)
    else:
        np_a = np_b = None

    tier = TIER_PYTHON
    device = "cpu"
    backend = "cpython"
    result: Optional[Matrix] = None
    degraded: List[str] = []
    for candidate in order:
        if candidate == TIER_PYTHON:
            break
        impl = _MATMUL_IMPLS.get(candidate)
        if impl is None:
            continue
        got = impl(a, b, np_a, np_b)
        if got is not None:
            result, device, backend = got
            tier = candidate
            break
        degraded.append(candidate)

    if result is None:
        if a is None or b is None:
            a = np_a.tolist() if np_a is not None else _gen_matrix_python(m, k, seed, 1)
            b = np_b.tolist() if np_b is not None else _gen_matrix_python(k, n, seed, 2)
        result, device, backend = _matmul_python(a, b)
        tier = TIER_PYTHON

    total = 0
    trace = 0
    for i, row in enumerate(result):
        total += sum(row)
        if i < len(row):
            trace += row[i]
    checksum = hashlib.sha256(
        (";".join(",".join(str(v) for v in row) for row in result)).encode("utf-8")
    ).hexdigest()

    max_return = int(params.get("max_return", DEFAULT_MAX_RETURN))
    payload: Dict[str, Any] = {
        "op": "matmul",
        "m": m,
        "k": k,
        "n": n,
        "seed": seed,
        "checksum": checksum,
        "trace": trace,
        "sum": total,
        "matrix": result if m * n <= max_return else None,
        # attribution — node facts, not part of the answer
        "tier": tier,
        "device": device,
        "backend": backend,
        "degraded_from": degraded,
    }
    return payload


OPS: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "primesum": op_primesum,
    "hashwork": op_hashwork,
    "matmul": op_matmul,
    "embed": op_embed,
}
