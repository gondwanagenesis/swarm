"""The capability tower: the agent discovers ITSELF before it discovers hardware.

Floor 0 (always): CPython runs, os.cpu_count() works, json works.
Floor 1: system tools on PATH (nvidia-smi, clinfo, vulkaninfo, lspci...).
Floor 2: optional packages importable (numpy, psutil, torch) — probed with
         importlib inside try/except, NEVER imported at module top level.
Floor 3: dedicated benchmark suites (mixbench, clpeak, BabelStream).

The tower also benchmarks the instrument itself: how fast is json on this
machine, how expensive is a subprocess call. A measurement from a slow
instrument is labelled as such downstream.
"""

from __future__ import annotations

import importlib
import json
import platform
import shutil
import statistics
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

from ..core.models import AgentCapability, Anomaly

FLOOR0 = 0
FLOOR1 = 1
FLOOR2 = 2
FLOOR3 = 3

SYSTEM_TOOLS = [
    "nvidia-smi",
    "amd-smi",
    "rocm-smi",
    "clinfo",
    "vulkaninfo",
    "lspci",
    "lsusb",
    "vcgencmd",
    "powermetrics",
    "system_profiler",
    "sensors",
]

OPTIONAL_PACKAGES = ["numpy", "psutil", "torch"]

BENCH_SUITES = ["mixbench", "mixbench-cpu", "clpeak", "babelstream", "stream"]

RUNTIME_HINTS = ["nvidia-smi", "clinfo", "vulkaninfo"]


def _tool_version(tool: str) -> Optional[str]:
    from ._proc import run_bounded

    for flag in ("--version", "-V", "-version", "version"):
        out = run_bounded([tool, flag], timeout=10.0)
        if out:
            first = out.strip().splitlines()[0].strip()
            return first[:120]
    return "present"


def _probe_tools() -> Dict[str, Optional[str]]:
    found: Dict[str, Optional[str]] = {}
    for tool in SYSTEM_TOOLS:
        path = shutil.which(tool)
        if path:
            found[tool] = _tool_version(tool)
    return found


def _probe_packages() -> Dict[str, Optional[str]]:
    found: Dict[str, Optional[str]] = {}
    for pkg in OPTIONAL_PACKAGES:
        try:
            mod = importlib.import_module(pkg)
            found[pkg] = str(getattr(mod, "__version__", "unknown"))
        except Exception:
            continue
    return found


def _probe_bench_suites() -> Dict[str, Optional[str]]:
    found: Dict[str, Optional[str]] = {}
    for suite in BENCH_SUITES:
        if shutil.which(suite):
            found[suite] = "present"
    return found


def _instrument_bench(anomalies: List[Anomaly]) -> Dict[str, Optional[float]]:
    """Benchmark the benchmarker. Results are microseconds/milliseconds."""
    out: Dict[str, Optional[float]] = {}
    try:
        payload = {"k": list(range(1000))}
        times = []
        for _ in range(50):
            t0 = time.perf_counter()
            json.loads(json.dumps(payload))
            times.append((time.perf_counter() - t0) * 1e6)
        out["json_roundtrip_us"] = round(statistics.median(times), 2)
    except Exception as exc:
        out["json_roundtrip_us"] = None
        anomalies.append(Anomaly("self_probe.instrument", f"json bench failed: {exc}"))
    try:
        cmd = [sys.executable, "-c", "pass"]
        times = []
        for _ in range(3):
            t0 = time.perf_counter()
            subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15.0
            )
            times.append((time.perf_counter() - t0) * 1e3)
        out["subprocess_overhead_ms"] = round(statistics.median(times), 2)
    except Exception as exc:
        out["subprocess_overhead_ms"] = None
        anomalies.append(
            Anomaly("self_probe.instrument", f"subprocess bench failed: {exc}")
        )
    return out


def climb_tower(bench_instrument: bool = True) -> Tuple[AgentCapability, List[Anomaly]]:
    """Discover what this agent can do. Never raises."""
    anomalies: List[Anomaly] = []
    cap = AgentCapability()
    try:
        cap.python_version = platform.python_version()
    except Exception as exc:
        anomalies.append(Anomaly("self_probe", f"python version unknown: {exc}"))

    try:
        cap.tools = _probe_tools()
    except Exception as exc:
        cap.tools = {}
        anomalies.append(Anomaly("self_probe.tools", str(exc)))

    try:
        cap.packages = _probe_packages()
    except Exception as exc:
        cap.packages = {}
        anomalies.append(Anomaly("self_probe.packages", str(exc)))

    suites: Dict[str, Optional[str]] = {}
    try:
        suites = _probe_bench_suites()
    except Exception as exc:
        anomalies.append(Anomaly("self_probe.bench_suites", str(exc)))
    if suites:
        cap.tools.update(suites)

    runtimes: List[str] = []
    if "nvidia-smi" in cap.tools:
        runtimes.append("cuda")
    if "clinfo" in cap.tools:
        runtimes.append("opencl")
    if "vulkaninfo" in cap.tools:
        runtimes.append("vulkan")
    if platform.system() == "Darwin":
        runtimes.append("metal")
    cap.runtimes = sorted(set(runtimes))

    cap.max_floor = FLOOR0
    if cap.tools:
        cap.max_floor = FLOOR1
    if cap.packages:
        cap.max_floor = max(cap.max_floor, FLOOR2)
    if suites:
        cap.max_floor = FLOOR3

    cap.instrument = _instrument_bench(anomalies) if bench_instrument else {}
    return cap, anomalies
