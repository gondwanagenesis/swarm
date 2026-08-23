"""Run per-device adapter code the agent did not write.

The stdlib-only law binds this module's SOURCE (CI scans `swarm/agent/*.py`
with the AST). It does not bind the adapters this module loads: an adapter is
per-device userspace code, shipped at runtime, and MAY import whatever the
capability tower actually found on this node (numpy, torch, pyopencl). That
is the escape hatch the architecture already designed — the agent stays
bootable on a bare Termux phone while a 4090 box runs a torch adapter.

Three rules make that safe:

1. **Subprocess, always.** An adapter is untrusted code, same reasoning as
   the contract gate. It never executes inside the agent process, so a
   segfault in a vendor runtime costs one chunk item, not the node.
2. **JSON in / JSON out** over stdin/stdout. Never pickle (AGENTS.md wire
   format law). The result line is sentinel-framed so an adapter that prints
   to stdout cannot corrupt the protocol.
3. **Never raises.** Timeout, crash, non-zero exit, unparseable output and
   missing adapter all become the same structured failure dict, with a
   `reason` a human can act on. Fail closed, and say why.

Attribution (law 5) rides back with the result: the tier and device the
adapter claims, plus what this node could actually offer, so a claim of
"cuda" from a node whose tower never found cuda is visible as a conflict.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

RESULT_SENTINEL = "<<<SWARM-ADAPTER-RESULT>>>"
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_ENTRYPOINT = "run"
MAX_STDERR_CHARS = 2000

REASON_OK = "ok"
REASON_TIMEOUT = "timeout"
REASON_CRASH = "crash"
REASON_BAD_OUTPUT = "bad_output"
REASON_NOT_FOUND = "adapter_not_found"
REASON_SPAWN_FAILED = "spawn_failed"
REASON_ADAPTER_ERROR = "adapter_error"

_RUNTIME_CACHE: Optional[Dict[str, Any]] = None


@dataclass
class AdapterSpec:
    """What to run. `source` wins; otherwise `adapter_id` is resolved from
    the local adapter directory."""

    adapter_id: str = ""
    source: Optional[str] = None
    entrypoint: str = DEFAULT_ENTRYPOINT
    timeout_s: float = DEFAULT_TIMEOUT_S
    params: Dict[str, Any] = field(default_factory=dict)


# The child harness. A string on purpose: the CI import gate walks THIS
# file's AST, and the harness is data here, code only in the subprocess.
_RUNNER_SOURCE = '''
import importlib.util, json, sys, traceback

SENTINEL = "%s"


def _emit(obj):
    sys.stdout.flush()
    sys.stdout.write("\\n" + SENTINEL + json.dumps(obj, default=str) + "\\n")
    sys.stdout.flush()


def main():
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except Exception as exc:
        _emit({"ok": False, "reason": "bad_output", "error": "unreadable request: %%s" %% exc})
        return 0
    path = req.get("adapter_path")
    entrypoint = req.get("entrypoint") or "run"
    params = req.get("params") or {}
    try:
        spec = importlib.util.spec_from_file_location("swarm_adapter", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["swarm_adapter"] = module
        spec.loader.exec_module(module)
    except Exception:
        _emit({"ok": False, "reason": "adapter_error",
               "error": traceback.format_exc(limit=6)[-1500:]})
        return 0
    fn = getattr(module, entrypoint, None)
    if not callable(fn):
        _emit({"ok": False, "reason": "adapter_error",
               "error": "entrypoint %%r not callable" %% entrypoint})
        return 0
    try:
        payload = fn(params)
    except Exception:
        _emit({"ok": False, "reason": "adapter_error",
               "error": traceback.format_exc(limit=6)[-1500:]})
        return 0
    tier = device = None
    if isinstance(payload, dict):
        tier = payload.get("tier")
        device = payload.get("device")
    _emit({"ok": True, "reason": "ok", "payload": payload,
           "tier": tier, "device": device,
           "runner_python": sys.version.split()[0]})
    return 0


sys.exit(main())
''' % RESULT_SENTINEL


def adapter_dir() -> str:
    """Where adapters delivered to this node live. Overridable for tests."""
    override = os.environ.get("SWARM_ADAPTER_DIR")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".swarm", "adapters")


def resolve_adapter(adapter_id: str) -> Optional[str]:
    """Read an adapter's source by id from the local adapter directory.
    Returns None when it is not there — never guesses, never fetches."""
    if not adapter_id or any(sep in adapter_id for sep in ("/", "\\", "..")):
        return None
    path = os.path.join(adapter_dir(), adapter_id + ".py")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def available_runtimes(refresh: bool = False) -> Dict[str, Any]:
    """What an adapter may rely on here — straight from the capability tower.

    Delegates to `swarm.probe.self_probe.climb_tower`; detection logic is not
    duplicated. Cached, because climbing shells out to system tools.
    """
    global _RUNTIME_CACHE
    if _RUNTIME_CACHE is not None and not refresh:
        return _RUNTIME_CACHE
    out: Dict[str, Any] = {
        "floor": 0,
        "packages": [],
        "tools": [],
        "runtimes": [],
        "python": sys.version.split()[0],
        "error": None,
    }
    try:
        from ..probe.self_probe import climb_tower

        cap, anomalies = climb_tower(bench_instrument=False)
        out["floor"] = cap.max_floor
        out["packages"] = sorted((cap.packages or {}).keys())
        out["tools"] = sorted((cap.tools or {}).keys())
        out["runtimes"] = list(cap.runtimes or [])
        out["anomalies"] = len(anomalies)
    except Exception as exc:  # the tower itself never raises, but be sure
        out["error"] = str(exc)[:200]
    _RUNTIME_CACHE = out
    return out


def _failure(reason: str, error: str, spec: AdapterSpec, stderr: str = "") -> Dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "error": error[:600],
        "payload": None,
        "tier": None,
        "device": None,
        "adapter_id": spec.adapter_id,
        "entrypoint": spec.entrypoint,
        "stderr": stderr[-MAX_STDERR_CHARS:],
    }


def _parse_result(stdout: str) -> Optional[Dict[str, Any]]:
    idx = stdout.rfind(RESULT_SENTINEL)
    if idx < 0:
        return None
    tail = stdout[idx + len(RESULT_SENTINEL) :]
    line = tail.splitlines()[0] if tail.splitlines() else ""
    try:
        parsed = json.loads(line)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def run_adapter(
    source: Optional[str] = None,
    entrypoint: str = DEFAULT_ENTRYPOINT,
    params: Optional[Dict[str, Any]] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    adapter_id: str = "",
) -> Dict[str, Any]:
    """Execute `entrypoint(params)` from adapter source in a subprocess.

    Returns a dict that is ALWAYS shaped the same:
      ok, reason, error, payload, tier, device, adapter_id, entrypoint, stderr
    """
    spec = AdapterSpec(
        adapter_id=adapter_id,
        source=source,
        entrypoint=entrypoint or DEFAULT_ENTRYPOINT,
        timeout_s=float(timeout_s or DEFAULT_TIMEOUT_S),
        params=dict(params or {}),
    )
    return run_spec(spec)


def run_spec(spec: AdapterSpec) -> Dict[str, Any]:
    source = spec.source
    if source is None and spec.adapter_id:
        source = resolve_adapter(spec.adapter_id)
    if not source:
        return _failure(
            REASON_NOT_FOUND,
            "no adapter source for id %r (looked in %s)" % (spec.adapter_id, adapter_dir()),
            spec,
        )

    workdir = tempfile.mkdtemp(prefix="swarm-adapter-")
    try:
        adapter_path = os.path.join(workdir, "adapter_mod.py")
        runner_path = os.path.join(workdir, "runner.py")
        with open(adapter_path, "w", encoding="utf-8") as handle:
            handle.write(source)
        with open(runner_path, "w", encoding="utf-8") as handle:
            handle.write(_RUNNER_SOURCE)
        request = json.dumps(
            {
                "adapter_path": adapter_path,
                "entrypoint": spec.entrypoint,
                "params": spec.params,
            }
        )
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                [sys.executable, runner_path],
                input=request.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(spec.timeout_s, 0.1),
                cwd=workdir,
            )
        except subprocess.TimeoutExpired as exc:
            partial = (exc.stderr or b"").decode("utf-8", "replace")
            return _failure(
                REASON_TIMEOUT,
                "adapter exceeded %.1fs and was killed" % spec.timeout_s,
                spec,
                partial,
            )
        except OSError as exc:
            return _failure(REASON_SPAWN_FAILED, "could not spawn adapter: %s" % exc, spec)

        elapsed = time.perf_counter() - started
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        parsed = _parse_result(stdout)
        if parsed is None:
            reason = REASON_CRASH if proc.returncode != 0 else REASON_BAD_OUTPUT
            return _failure(
                reason,
                "adapter produced no result frame (exit %s after %.2fs)" % (proc.returncode, elapsed),
                spec,
                stderr,
            )
        out: Dict[str, Any] = {
            "ok": bool(parsed.get("ok")),
            "reason": str(parsed.get("reason") or (REASON_OK if parsed.get("ok") else REASON_ADAPTER_ERROR)),
            "error": (str(parsed.get("error"))[:600] if parsed.get("error") else None),
            "payload": parsed.get("payload"),
            "tier": parsed.get("tier"),
            "device": parsed.get("device"),
            "adapter_id": spec.adapter_id,
            "entrypoint": spec.entrypoint,
            "stderr": stderr[-MAX_STDERR_CHARS:],
        }
        return out
    except Exception as exc:  # never raises into the worker loop
        return _failure(REASON_CRASH, "adapter runtime error: %s" % exc, spec)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def task_adapter_spec(task: Dict[str, Any]) -> Optional[AdapterSpec]:
    """Pull an adapter request out of a leased task, or None if it isn't one.

    Looks at the task envelope first, then its params, so a bag can carry the
    adapter for every item or an item can carry its own.
    """
    params = task.get("params") if isinstance(task.get("params"), dict) else {}
    params = params or {}

    def pick(key: str) -> Any:
        if task.get(key) is not None:
            return task.get(key)
        return params.get(key)

    source = pick("adapter_source")
    adapter_id = pick("adapter_id")
    if source is None and not adapter_id:
        return None
    entrypoint = pick("adapter_entrypoint") or DEFAULT_ENTRYPOINT
    timeout = pick("adapter_timeout_s")
    try:
        timeout_s = float(timeout) if timeout is not None else DEFAULT_TIMEOUT_S
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    inner = params.get("adapter_params")
    call_params = dict(inner) if isinstance(inner, dict) else dict(params)
    return AdapterSpec(
        adapter_id=str(adapter_id or ""),
        source=str(source) if source is not None else None,
        entrypoint=str(entrypoint),
        timeout_s=timeout_s,
        params=call_params,
    )


def known_adapters() -> List[str]:
    """Adapter ids present on this node. Empty list when the dir is absent."""
    try:
        names = os.listdir(adapter_dir())
    except OSError:
        return []
    return sorted(n[:-3] for n in names if n.endswith(".py"))
