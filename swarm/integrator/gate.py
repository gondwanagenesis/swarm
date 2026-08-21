"""The contract gate (M4). Law 4: calibrated verification.

A contract ships a known-good and a known-bad implementation plus reference
cases. The gate must accept the good and REJECT the bad — if the gate cannot
fail its own known-bad, the gate itself does not deploy; that failure is
honest, loud, and recorded.

Every gate run is content-addressed (adapter source + contract + results)
and persisted. Fail closed: an ambiguous or crashed case is a rejection.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..core.identity import canonical_hash, result_hash

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"


def load_contract(name: str) -> Dict[str, Any]:
    path = CONTRACTS_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def _python_callable(source: str) -> Optional[Callable[[int], List[int]]]:
    """Compile a python adapter body into a callable. Sandbox-lite at M4:
    subprocess isolation lands with M4.5; for now the gate runs adapters
    in-process with no builtins beyond math — the contract gate, not a jail."""
    namespace: Dict[str, Any] = {"__builtins__": {"range": range, "len": len, "list": list}}
    try:
        exec(compile(source, "<adapter>", "exec"), namespace)
    except Exception:
        return None
    fn = namespace.get("primes")
    return fn if callable(fn) else None


def _run_case(fn: Callable, case: Dict[str, Any]) -> Dict[str, Any]:
    inp = case["input"]
    expected = case["expected"]
    try:
        out = fn(int(inp["n"]))
    except Exception as exc:
        return {"input": inp, "expected": expected, "ok": False, "error": str(exc)[:120]}
    actual = len(out) if isinstance(out, (list, tuple)) else out
    return {"input": inp, "expected": expected, "actual": actual, "ok": actual == expected}


def run_gate(source: str, contract: Dict[str, Any]) -> Dict[str, Any]:
    """Run an adapter source against a contract. Returns the full record,
    including the calibration proof (known-good accepted, known-bad rejected)."""
    started = time.time()
    adapter_id = "ai-" + result_hash(source.encode("utf-8"))[:16]
    record: Dict[str, Any] = {
        "adapter_id": adapter_id,
        "contract_hash": canonical_hash(contract),
        "device_class": contract.get("device_class"),
        "cases": [],
        "known_good_passed": None,
        "known_bad_rejected": None,
        "passed": False,
        "reason": "",
        "at": started,
    }

    fn = _python_callable(source)
    if fn is None:
        record["reason"] = "adapter failed to compile"
        return record

    cases_ok = True
    for case in contract.get("cases", []):
        res = _run_case(fn, case)
        record["cases"].append(res)
        if not res["ok"]:
            cases_ok = False

    calibration_ok = True
    kg = contract.get("known_good") or {}
    kb = contract.get("known_bad") or {}
    if kg.get("source"):
        good_fn = _python_callable(kg["source"])
        if good_fn is None:
            calibration_ok = False
        else:
            for case in contract.get("cases", []):
                if not _run_case(good_fn, case)["ok"]:
                    calibration_ok = False
    record["known_good_passed"] = calibration_ok if kg else None

    bad_rejected = None
    if kb.get("source"):
        bad_fn = _python_callable(kb["source"])
        if bad_fn is None:
            bad_rejected = True
        else:
            accepted_any = all(_run_case(bad_fn, c)["ok"] for c in contract.get("cases", []))
            bad_rejected = not accepted_any
    record["known_bad_rejected"] = bad_rejected

    calib_ok = (record["known_good_passed"] is not False) and (bad_rejected is not False)
    record["passed"] = bool(cases_ok and calib_ok)
    if record["passed"]:
        record["reason"] = "all cases pass and calibration holds"
    else:
        reasons = []
        if not cases_ok:
            reasons.append("reference case(s) failed")
        if record["known_good_passed"] is False:
            reasons.append("known-good failed")
        if bad_rejected is False:
            reasons.append("known-bad was NOT rejected — gate unfit")
        record["reason"] = "; ".join(reasons)
    record["gate_run_id"] = (
        "gate-"
        + result_hash(
            json.dumps(
                {"adapter": adapter_id, "cases": record["cases"], "passed": record["passed"], "at": started},
                sort_keys=True,
            ).encode("utf-8")
        )[:16]
    )
    record["duration_s"] = round(time.time() - started, 4)
    return record
