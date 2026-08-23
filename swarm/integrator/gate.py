"""The contract gate (M4/M4.5). Law 4: calibrated verification.

A contract ships a known-good and a known-bad implementation plus reference
cases. The gate must accept the good and REJECT the bad — if the gate cannot
fail its own known-bad, the gate itself does not deploy; that failure is
honest, loud, and recorded.

Two things the gate is not allowed to be:

1. Prime-specific. The entrypoint name, the calling convention and the
   comparison mode all come from the contract. A GPU matmul contract runs
   through the same code path as the toy prime contract.
2. An in-process ``exec``. Restricted ``__builtins__`` is not a boundary
   (``().__class__.__base__.__subclasses__()`` walks straight out of it), and
   the candidate code here is LLM-written. Every candidate runs in a child
   interpreter under a hard timeout; a timeout is a rejection.

Every gate run is content-addressed (adapter source + contract + results)
and persisted. Fail closed: an ambiguous, crashed, unparseable or timed-out
case is a rejection.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.identity import canonical_hash, result_hash

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"

DEFAULT_ENTRYPOINT = "primes"
DEFAULT_COMPARE = "length"
DEFAULT_TIMEOUT_S = 10.0
MAX_TIMEOUT_S = 300.0
DEFAULT_RTOL = 1e-9
DEFAULT_ATOL = 1e-12

# The child interpreter. Reads one JSON job on stdin, writes one marked JSON
# line on stdout. It never trusts the adapter: every case is individually
# guarded, and the value is proven JSON-serializable inside the child so an
# adapter cannot poison the parent's parse. Chatter on stdout is harmless —
# the parent keys off the marker, not the whole stream.
#
# The marker is a per-run nonce delivered over stdin (never argv, which the
# adapter can read). Without it an adapter could print its own marker line
# from an atexit hook and forge a pass.
_RUNNER = r'''
import json
import sys


def _err(exc):
    return (type(exc).__name__ + ": " + str(exc))[:200]


def _main(job):
    entry = job["entrypoint"]
    arg_order = job.get("arg_order") or []
    style = job.get("style") or "positional"
    namespace = {"__name__": "__swarm_adapter__"}
    try:
        exec(compile(job["source"], "<adapter>", "exec"), namespace)
    except BaseException as exc:
        return {"compiled": False, "fatal": "compile", "error": _err(exc)}
    fn = namespace.get(entry)
    if not callable(fn):
        return {
            "compiled": True,
            "fatal": "entrypoint",
            "error": "entrypoint " + repr(entry) + " not defined",
        }
    results = []
    for case in job.get("cases") or []:
        inp = case.get("input") or {}
        try:
            if style == "kwargs":
                out = fn(**inp)
            else:
                out = fn(*[inp[k] for k in arg_order])
            json.dumps(out)
        except BaseException as exc:
            results.append({"error": _err(exc)})
            continue
        results.append({"value": out})
    return {"compiled": True, "results": results}


_job = json.loads(sys.stdin.read())
_mark = _job.pop("nonce")
try:
    payload = _main(_job)
except BaseException as exc:
    payload = {"compiled": False, "fatal": "runner", "error": "runner failure: " + _err(exc)}
sys.stdout.write("\n" + _mark + json.dumps(payload) + "\n")
sys.stdout.flush()
'''


def load_contract(name: str) -> Dict[str, Any]:
    path = CONTRACTS_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# contract -> calling convention
# ---------------------------------------------------------------------------


def _resolve_arg_order(contract: Dict[str, Any], cases: Sequence[Dict[str, Any]]) -> List[str]:
    """Which input keys become positional args, in order.

    An explicit ``signature.arg_order`` wins. Otherwise fall back to the M4
    behaviour (a single ``n``), and only if no case carries ``n`` do we take
    the first non-empty case's key order — JSON objects keep their file order,
    so this stays deterministic.
    """
    sig = contract.get("signature") or {}
    declared = sig.get("arg_order")
    if isinstance(declared, (list, tuple)):
        return [str(k) for k in declared]
    for case in cases:
        if "n" in (case.get("input") or {}):
            return ["n"]
    for case in cases:
        inp = case.get("input") or {}
        if isinstance(inp, dict) and inp:
            return [str(k) for k in inp]
    return []


def _timeout_s(contract: Dict[str, Any]) -> float:
    try:
        raw = float(contract.get("timeout_s", DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    if not math.isfinite(raw) or raw <= 0:
        return DEFAULT_TIMEOUT_S
    return min(raw, MAX_TIMEOUT_S)


# ---------------------------------------------------------------------------
# comparison modes
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def allclose(actual: Any, expected: Any, rtol: float = DEFAULT_RTOL, atol: float = DEFAULT_ATOL) -> bool:
    """Elementwise numeric comparison over arbitrarily nested lists.

    Pure stdlib on purpose: the gate has to run on a Termux phone with bare
    CPython, so numpy is not on the table.
    """
    if isinstance(expected, (list, tuple)):
        if not isinstance(actual, (list, tuple)) or len(actual) != len(expected):
            return False
        return all(allclose(a, e, rtol, atol) for a, e in zip(actual, expected))
    if isinstance(actual, (list, tuple)):
        return False
    if _is_number(actual) and _is_number(expected):
        if math.isnan(actual) or math.isnan(expected):
            return False
        if math.isinf(actual) or math.isinf(expected):
            return actual == expected
        return abs(actual - expected) <= atol + rtol * abs(expected)
    return actual == expected


def _compare(mode: str, out: Any, expected: Any, contract: Dict[str, Any]) -> Tuple[Any, bool]:
    """Returns (value recorded as ``actual``, ok). An unknown mode fails closed."""
    if mode == "length":
        actual = len(out) if isinstance(out, (list, tuple)) else out
        return actual, actual == expected
    if mode == "exact":
        return out, out == expected
    if mode == "allclose":
        try:
            rtol = float(contract.get("rtol", DEFAULT_RTOL))
            atol = float(contract.get("atol", DEFAULT_ATOL))
        except (TypeError, ValueError):
            rtol, atol = DEFAULT_RTOL, DEFAULT_ATOL
        return out, allclose(out, expected, rtol, atol)
    return out, False


# ---------------------------------------------------------------------------
# isolated execution
# ---------------------------------------------------------------------------


def _execute(source: str, contract: Dict[str, Any], cases: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Run one adapter over every case in a child interpreter.

    Never raises. Returns ``{"compiled": bool, "error": str|None, "results":
    [{"value": ...} | {"error": ...}]}``. All cases share one child (a process
    launch per case would dominate the runtime), so a hang or a hard crash
    rejects the whole batch — which is the fail-closed answer anyway.
    """
    timeout = _timeout_s(contract)
    nonce = "__SWARM_GATE_%s__" % uuid.uuid4().hex
    job = {
        "nonce": nonce,
        "source": source,
        "entrypoint": str(contract.get("entrypoint") or DEFAULT_ENTRYPOINT),
        "arg_order": _resolve_arg_order(contract, cases),
        "style": str((contract.get("signature") or {}).get("style") or "positional"),
        "cases": [{"input": c.get("input") or {}} for c in cases],
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _RUNNER],
            input=json.dumps(job),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run kills the child before this propagates. A timeout is
        # a rejection, never a retry and never a pass.
        return {
            "compiled": True,
            "timed_out": True,
            "error": "timeout after %.2fs — adapter killed" % timeout,
            "results": [],
        }
    except Exception as exc:  # launching the child itself failed
        return {
            "compiled": False,
            "fatal": "launch",
            "error": "sandbox launch failed: %s" % str(exc)[:120],
            "results": [],
        }

    line = None
    for candidate in (proc.stdout or "").splitlines():
        if candidate.startswith(nonce):
            line = candidate[len(nonce):]
    if line is None:
        detail = (proc.stderr or "").strip().splitlines()
        tail = detail[-1][:160] if detail else "no output"
        return {
            "compiled": False,
            "fatal": "no_result",
            "error": "adapter produced no parseable result (exit %s): %s" % (proc.returncode, tail),
            "results": [],
        }
    try:
        payload = json.loads(line)
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        return {
            "compiled": False,
            "fatal": "bad_output",
            "error": "adapter result was not a JSON object",
            "results": [],
        }
    payload.setdefault("results", [])
    payload.setdefault("error", None)
    payload.setdefault("compiled", False)
    return payload


def _verify(source: str, contract: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Every reference case, verified. One record per case, always — a missing
    result (crash, kill, truncated output) is a rejected case with a reason."""
    cases = list(contract.get("cases") or [])
    mode = str(contract.get("compare") or DEFAULT_COMPARE)
    outcome = _execute(source, contract, cases)
    results = outcome.get("results") or []
    fallback = outcome.get("error") or "no result from adapter"

    records: List[Dict[str, Any]] = []
    for idx, case in enumerate(cases):
        base = {"input": case.get("input"), "expected": case.get("expected")}
        raw = results[idx] if idx < len(results) else None
        if raw is None or not isinstance(raw, dict):
            records.append({**base, "ok": False, "error": str(fallback)[:200]})
            continue
        if "error" in raw:
            records.append({**base, "ok": False, "error": str(raw["error"])[:200]})
            continue
        actual, ok = _compare(mode, raw.get("value"), case.get("expected"), contract)
        record: Dict[str, Any] = {**base, "actual": actual, "ok": bool(ok)}
        if not ok:
            record["error"] = "compare(%s) mismatch" % mode
        records.append(record)
    return records, outcome


def _run_cases(source: str, contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    """`_verify` without the transport detail — used for the calibration pair."""
    return _verify(source, contract)[0]


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def run_gate(source: str, contract: Dict[str, Any]) -> Dict[str, Any]:
    """Run an adapter source against a contract. Returns the full record,
    including the calibration proof (known-good accepted, known-bad rejected)."""
    started = time.time()
    adapter_id = "ai-" + result_hash(source.encode("utf-8"))[:16]
    record: Dict[str, Any] = {
        "adapter_id": adapter_id,
        "contract_hash": canonical_hash(contract),
        "device_class": contract.get("device_class"),
        "entrypoint": str(contract.get("entrypoint") or DEFAULT_ENTRYPOINT),
        "compare": str(contract.get("compare") or DEFAULT_COMPARE),
        "cases": [],
        "known_good_passed": None,
        "known_bad_rejected": None,
        "passed": False,
        "reason": "",
        "at": started,
    }

    def _finish(reason: str) -> Dict[str, Any]:
        record["reason"] = reason
        record["gate_run_id"] = _gate_run_id(adapter_id, record, started)
        record["duration_s"] = round(time.time() - started, 4)
        return record

    cases, outcome = _verify(source, contract)
    record["cases"] = cases
    fatal = outcome.get("fatal")
    if fatal == "compile":
        return _finish("adapter failed to compile: %s" % outcome.get("error"))
    if fatal:
        return _finish("adapter rejected (%s): %s" % (fatal, outcome.get("error")))
    cases_ok = bool(contract.get("cases")) and bool(cases) and all(c["ok"] for c in cases)

    # --- the calibration proof: the load-bearing part of this file (Law 4) ---
    kg = contract.get("known_good") or {}
    kb = contract.get("known_bad") or {}

    if kg.get("source"):
        good_cases = _run_cases(kg["source"], contract)
        record["known_good_passed"] = bool(good_cases) and all(c["ok"] for c in good_cases)
    else:
        record["known_good_passed"] = None

    bad_rejected: Optional[bool] = None
    if kb.get("source"):
        bad_cases = _run_cases(kb["source"], contract)
        # Rejected = the bad implementation does NOT clear every case.
        bad_rejected = not (bool(bad_cases) and all(c["ok"] for c in bad_cases))
    record["known_bad_rejected"] = bad_rejected

    calib_ok = (record["known_good_passed"] is not False) and (bad_rejected is not False)
    record["passed"] = bool(cases_ok and calib_ok)
    if record["passed"]:
        return _finish("all cases pass and calibration holds")

    reasons = []
    if not cases_ok:
        reasons.append("reference case(s) failed")
    if record["known_good_passed"] is False:
        reasons.append("known-good failed")
    if bad_rejected is False:
        reasons.append("known-bad was NOT rejected — gate unfit")
    return _finish("; ".join(reasons) or "gate rejected")


def _gate_run_id(adapter_id: str, record: Dict[str, Any], started: float) -> str:
    return "gate-" + result_hash(
        json.dumps(
            {"adapter": adapter_id, "cases": record["cases"], "passed": record["passed"], "at": started},
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    )[:16]
