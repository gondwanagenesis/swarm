"""CPU collector. Linux via /proc + sysfs + cgroup quotas; macOS via sysctl;
Windows via PowerShell CIM and ctypes. Never raises, never hangs.

wmic is deliberately NOT used: it is absent from Windows 11 builds >= ~22H2
(verified gone on build 26100). CIM through powershell.exe is the maintained
path; ctypes is the floor.
"""

from __future__ import annotations

import contextlib
import os
import platform
import re
from typing import List, Optional, Tuple

from ..core.models import Anomaly, CpuInfo, MemoryInfo
from ._proc import run_bounded

HETEROGENEOUS_SPREAD = 0.20


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return None


def _read_int(path: str) -> Optional[int]:
    txt = _read_text(path)
    if txt is None:
        return None
    try:
        return int(txt.strip())
    except (ValueError, AttributeError):
        return None


def _linux_cgroup_cpu_quota() -> Optional[float]:
    quota_us = period_us = None
    v2 = _read_text("/sys/fs/cgroup/cpu.max")
    if v2:
        parts = v2.split()
        if len(parts) >= 2 and parts[0] != "max":
            with contextlib.suppress(ValueError):
                quota_us, period_us = int(parts[0]), int(parts[1])
    if quota_us is None:
        quota_us = _read_int("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        period_us = _read_int("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota_us and period_us and quota_us > 0 and period_us > 0:
        return quota_us / period_us
    return None


def _linux_cgroup_mem_limit() -> Optional[int]:
    v2 = _read_text("/sys/fs/cgroup/memory.max")
    if v2 and v2.strip() != "max":
        try:
            return int(v2.strip())
        except ValueError:
            pass
    return _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")


def _linux_cpu(info: CpuInfo, anomalies: List[Anomaly]) -> None:
    cpuinfo = _read_text("/proc/cpuinfo")
    if cpuinfo:
        models = re.findall(r"^model name\s*:\s*(.+)$", cpuinfo, re.M)
        if models:
            info.model = models[0].strip()
        cores = re.findall(r"^cpu cores\s*:\s*(\d+)$", cpuinfo, re.M)
        if cores:
            with contextlib.suppress(ValueError):
                info.physical_cores = int(cores[0])
        mhz = []
        for raw in re.findall(r"^cpu MHz\s*:\s*([\d.]+)$", cpuinfo, re.M):
            try:
                mhz.append(float(raw) * 1e6)
            except ValueError:
                continue
        if mhz:
            info.max_freq_hz = max(mhz)
    freqs = []
    try:
        base = "/sys/devices/system/cpu"
        for entry in os.listdir(base):
            if re.fullmatch(r"cpu\d+", entry):
                f = _read_int(os.path.join(base, entry, "cpufreq", "cpuinfo_max_freq"))
                if f:
                    freqs.append(f * 1000)
    except Exception:
        pass
    if freqs and min(freqs) > 0:
        spread = (max(freqs) - min(freqs)) / max(freqs)
        info.freq_spread_ratio = round(spread, 4)
        info.max_freq_hz = info.max_freq_hz or max(freqs)
        info.heterogeneous = spread > HETEROGENEOUS_SPREAD
    info.cgroup_quota_cores = _linux_cgroup_cpu_quota()


def _macos_cpu(info: CpuInfo, anomalies: List[Anomaly]) -> None:
    out = run_bounded(["sysctl", "-n", "machdep.cpu.brand_string"], timeout=10.0)
    if out:
        info.model = out.strip()
    ncpu = run_bounded(["sysctl", "-n", "hw.ncpu"], timeout=10.0)
    pcpu = run_bounded(["sysctl", "-n", "hw.physicalcpu"], timeout=10.0)
    freq = run_bounded(["sysctl", "-n", "hw.cpufrequency_max"], timeout=10.0)
    with contextlib.suppress(ValueError, AttributeError):
        info.physical_cores = int(pcpu.strip()) if pcpu else None
    with contextlib.suppress(ValueError, AttributeError):
        info.max_freq_hz = float(freq.strip()) if freq else None
    if platform.machine() == "arm64":
        info.heterogeneous = True
    _ = ncpu


def _windows_cpu(info: CpuInfo, anomalies: List[Anomaly]) -> None:
    ps = (
        "Get-CimInstance Win32_Processor | "
        "Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed | "
        "ConvertTo-Json -Compress"
    )
    out = run_bounded(["powershell.exe", "-NoProfile", "-Command", ps], timeout=15.0)
    if out:
        import json as _json

        try:
            data = _json.loads(out)
            rows = data if isinstance(data, list) else [data]
            if rows:
                r0 = rows[0]
                name = r0.get("Name")
                info.model = name.strip() if isinstance(name, str) else None
                phys = 0
                logical = 0
                speeds = []
                for row in rows:
                    phys += int(row.get("NumberOfCores") or 0)
                    logical += int(row.get("NumberOfLogicalProcessors") or 0)
                    mhz = row.get("MaxClockSpeed")
                    if mhz:
                        speeds.append(float(mhz) * 1e6)
                info.physical_cores = phys or None
                if logical:
                    info.logical_cores = logical
                if speeds:
                    info.max_freq_hz = max(speeds)
        except (ValueError, TypeError) as exc:
            anomalies.append(Anomaly("cpu.windows", f"CIM parse failed: {exc}"))
    else:
        anomalies.append(Anomaly("cpu.windows", "CIM query returned nothing", "info"))


def collect_cpu() -> Tuple[CpuInfo, List[Anomaly]]:
    anomalies: List[Anomaly] = []
    info = CpuInfo()
    try:
        info.logical_cores = os.cpu_count()
    except Exception as exc:
        anomalies.append(Anomaly("cpu", f"os.cpu_count failed: {exc}"))
    system = platform.system()
    try:
        if system == "Linux":
            _linux_cpu(info, anomalies)
        elif system == "Darwin":
            _macos_cpu(info, anomalies)
        elif system == "Windows":
            _windows_cpu(info, anomalies)
        else:
            anomalies.append(Anomaly("cpu", f"no collector for {system}", "info"))
    except Exception as exc:
        anomalies.append(Anomaly("cpu", f"collector crashed: {exc}", "error"))
    return info, anomalies


def _windows_memory(info: MemoryInfo) -> None:
    import ctypes
    from ctypes import byref, c_ulonglong, windll
    from ctypes.wintypes import DWORD

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", DWORD),
            ("dwMemoryLoad", DWORD),
            ("ullTotalPhys", c_ulonglong),
            ("ullAvailPhys", c_ulonglong),
            ("ullTotalPageFile", c_ulonglong),
            ("ullAvailPageFile", c_ulonglong),
            ("ullTotalVirtual", c_ulonglong),
            ("ullAvailVirtual", c_ulonglong),
            ("ullAvailExtendedVirtual", c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if windll.kernel32.GlobalMemoryStatusEx(byref(stat)):
        info.total_bytes = int(stat.ullTotalPhys)
        info.free_bytes = int(stat.ullAvailPhys)


def _linux_memory(info: MemoryInfo) -> None:
    meminfo = _read_text("/proc/meminfo")
    if meminfo:
        total = re.search(r"^MemTotal:\s+(\d+)\s*kB", meminfo, re.M)
        avail = re.search(r"^MemAvailable:\s+(\d+)\s*kB", meminfo, re.M)
        if total:
            info.total_bytes = int(total.group(1)) * 1024
        if avail:
            info.free_bytes = int(avail.group(1)) * 1024
    info.cgroup_limit_bytes = _linux_cgroup_mem_limit()


def _macos_memory(info: MemoryInfo) -> None:
    out = run_bounded(["sysctl", "-n", "hw.memsize"], timeout=10.0)
    if out:
        with contextlib.suppress(ValueError):
            info.total_bytes = int(out.strip())
    vm = run_bounded(["vm_stat"], timeout=10.0)
    page_kb = run_bounded(["sysctl", "-n", "vm.pagesize"], timeout=10.0)
    if vm and page_kb:
        try:
            page = int(page_kb.strip())
            free = re.search(r"Pages free:\s+(\d+)", vm)
            inactive = re.search(r"Pages inactive:\s+(\d+)", vm)
            if free:
                pages = int(free.group(1)) + (int(inactive.group(1)) if inactive else 0)
                info.free_bytes = pages * page
        except (ValueError, AttributeError):
            pass


def collect_memory() -> Tuple[MemoryInfo, List[Anomaly]]:
    anomalies: List[Anomaly] = []
    info = MemoryInfo()
    system = platform.system()
    try:
        if system == "Linux":
            _linux_memory(info)
        elif system == "Darwin":
            _macos_memory(info)
        elif system == "Windows":
            _windows_memory(info)
        else:
            anomalies.append(Anomaly("memory", f"no collector for {system}", "info"))
    except Exception as exc:
        anomalies.append(Anomaly("memory", f"collector crashed: {exc}", "error"))
    if info.total_bytes is None:
        anomalies.append(Anomaly("memory", "total_bytes unknown"))
    if info.free_bytes is None:
        anomalies.append(Anomaly("memory", "free_bytes unknown"))
    return info, anomalies
