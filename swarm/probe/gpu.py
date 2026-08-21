"""GPU / accelerator collector.

Order: vendor-specific first (nvidia-smi, amd-smi, system_profiler), generic
second (clinfo, vulkaninfo, Windows CIM). Records merge on PCI address or UUID;
one physical GPU must become one DeviceInfo, not three. On conflicting fields
the vendor source wins.

Windows AdapterRAM is a signed 32-bit field: values anywhere in the top 32 MiB
of the 2^32 range are clamp artifacts meaning "more VRAM than this API can
express" and MUST be reported as None, never as the clamped value.
"""

from __future__ import annotations

import json
import platform
import re
from typing import Dict, List, Optional, Tuple

from ..core.models import Anomaly, DeviceInfo
from ._proc import run_bounded

ADAPTER_RAM_CEILING = 0x100000000
CLAMP_ZONE_BYTES = 32 * 1024 * 1024
PCI_VENDOR_NAMES = {
    "0x10de": "NVIDIA",
    "0x1002": "AMD",
    "0x8086": "Intel",
}


def _nvidia_smi() -> Tuple[List[DeviceInfo], List[Anomaly]]:
    devices: List[DeviceInfo] = []
    anomalies: List[Anomaly] = []
    out = run_bounded(
        [
            "nvidia-smi",
            "--query-gpu=name,uuid,pci.bus_id,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ],
        timeout=15.0,
    )
    if not out:
        return devices, anomalies
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            anomalies.append(Anomaly("gpu.nvidia-smi", f"unexpected row: {line[:80]}"))
            continue
        name, uuid_, pci, mem_mib, driver = (
            parts[0],
            parts[1],
            parts[2],
            parts[3],
            parts[4],
        )
        vram: Optional[int] = None
        try:
            vram = int(float(mem_mib)) * 1024 * 1024
        except ValueError:
            anomalies.append(Anomaly("gpu.nvidia-smi", f"VRAM parse failed for {name}"))
        devices.append(
            DeviceInfo(
                kind="gpu",
                name=name or None,
                vendor="NVIDIA",
                pci_address=pci.lower() if pci else None,
                uuid=uuid_ or None,
                vram_bytes=vram,
                driver_version=driver or None,
                unified_memory=False,
                runtimes=["cuda"],
                evidence={"vendor": "nvidia-smi"},
            )
        )
    return devices, anomalies


def _windows_cim_gpu() -> Tuple[List[DeviceInfo], List[Anomaly]]:
    devices: List[DeviceInfo] = []
    anomalies: List[Anomaly] = []
    ps = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM,DriverVersion,PNPDeviceID,AdapterCompatibility | "
        "ConvertTo-Json -Compress"
    )
    out = run_bounded(["powershell.exe", "-NoProfile", "-Command", ps], timeout=15.0)
    if not out:
        return devices, anomalies
    try:
        data = json.loads(out)
    except ValueError as exc:
        anomalies.append(Anomaly("gpu.windows", f"CIM parse failed: {exc}", "error"))
        return devices, anomalies
    rows = data if isinstance(data, list) else [data]
    for row in rows:
        pnp = row.get("PNPDeviceID") or ""
        ven = re.search(r"VEN_([0-9A-Fa-f]{4})", pnp)
        vendor_hex = ("0x" + ven.group(1).lower()) if ven else None
        vendor = (PCI_VENDOR_NAMES.get(vendor_hex) if vendor_hex else None) or row.get(
            "AdapterCompatibility"
        )
        raw_ram = row.get("AdapterRAM")
        vram: Optional[int] = None
        if raw_ram is not None:
            try:
                ram = int(raw_ram)
                if ram < 0 or ram >= ADAPTER_RAM_CEILING - CLAMP_ZONE_BYTES:
                    anomalies.append(
                        Anomaly(
                            "gpu.windows",
                            f"AdapterRAM in 32-bit clamp zone for {row.get('Name')}: reporting None",
                            "info",
                        )
                    )
                else:
                    vram = ram
            except (ValueError, TypeError):
                pass
        integrated = vendor == "Intel"
        devices.append(
            DeviceInfo(
                kind="gpu",
                name=row.get("Name") or None,
                vendor=vendor or None,
                driver_version=row.get("DriverVersion") or None,
                vram_bytes=vram,
                unified_memory=True if integrated else None,
                evidence={"generic": "win32_videocontroller", "pnp": pnp[:80]},
            )
        )
    return devices, anomalies


def _macos_system_profiler() -> Tuple[List[DeviceInfo], List[Anomaly]]:
    devices: List[DeviceInfo] = []
    anomalies: List[Anomaly] = []
    out = run_bounded(["system_profiler", "SPDisplaysDataType", "-json"], timeout=15.0)
    if not out:
        return devices, anomalies
    try:
        data = json.loads(out)
    except ValueError as exc:
        anomalies.append(
            Anomaly("gpu.macos", f"system_profiler parse failed: {exc}", "error")
        )
        return devices, anomalies
    for card in data.get("SPDisplaysDataType", []) or []:
        name = card.get("sppci_model") or card.get("_name")
        vendor = card.get("spdisplays_vendor") or ""
        vram_str = card.get("sppci_vram") or card.get("spdisplays_vram") or ""
        vram: Optional[int] = None
        m = re.match(r"(\d+)\s*GB", str(vram_str))
        if m:
            vram = int(m.group(1)) * 1024**3
        is_apple = "Apple" in vendor or platform.machine() == "arm64"
        devices.append(
            DeviceInfo(
                kind="gpu",
                name=name,
                vendor=vendor or None,
                vram_bytes=None if is_apple else vram,
                unified_memory=True if is_apple else None,
                runtimes=["metal"],
                evidence={"vendor": "system_profiler"},
            )
        )
    return devices, anomalies


def merge_devices(
    primary: List[DeviceInfo], extra: List[DeviceInfo]
) -> List[DeviceInfo]:
    """Merge extra into primary by PCI/UUID. Vendor (primary) wins conflicts."""
    by_key: Dict[str, DeviceInfo] = {}
    unkeyed: List[DeviceInfo] = []
    for dev in primary:
        key = dev.merge_key
        if key:
            by_key[key] = dev
        else:
            unkeyed.append(dev)
    for dev in extra:
        key = dev.merge_key
        target = by_key.get(key) if key else None
        if target is None:
            unkeyed.append(dev)
            continue
        for f in ("name", "vendor", "vram_bytes", "driver_version", "unified_memory"):
            if getattr(target, f) is None and getattr(dev, f) is not None:
                setattr(target, f, getattr(dev, f))
        for rt in dev.runtimes:
            if rt not in target.runtimes:
                target.runtimes.append(rt)
        target.evidence.update(dev.evidence)
    return list(by_key.values()) + unkeyed


def collect_devices() -> Tuple[List[DeviceInfo], List[Anomaly]]:
    anomalies: List[Anomaly] = []
    vendor_devices: List[DeviceInfo] = []
    generic: List[DeviceInfo] = []
    system = platform.system()

    try:
        found, errs = _nvidia_smi()
        vendor_devices.extend(found)
        anomalies.extend(errs)
    except Exception as exc:
        anomalies.append(
            Anomaly("gpu.nvidia-smi", f"collector crashed: {exc}", "error")
        )

    if system == "Darwin":
        try:
            found, errs = _macos_system_profiler()
            vendor_devices.extend(found)
            anomalies.extend(errs)
        except Exception as exc:
            anomalies.append(Anomaly("gpu.macos", f"collector crashed: {exc}", "error"))

    if system == "Windows":
        try:
            found, errs = _windows_cim_gpu()
            generic.extend(found)
            anomalies.extend(errs)
        except Exception as exc:
            anomalies.append(
                Anomaly("gpu.windows", f"collector crashed: {exc}", "error")
            )

    merged = merge_devices(vendor_devices, generic)
    if not merged:
        anomalies.append(Anomaly("gpu", "no GPU reported by any source", "info"))
    return merged, anomalies
