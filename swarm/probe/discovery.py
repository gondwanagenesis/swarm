"""Tier-0 discovery: does an EXISTING runtime already bind this device?

Enumerate before you generate (Law 3). If OpenCL or Vulkan already speaks to
the device, the device can run work TODAY with zero new code — no synthesis,
no adapters invented where a wheel exists. This module only reads what the
capability tower already found; it never installs anything.

Produces RuntimeBinding dicts the hub stores against a device class:
    {node_id, device_class, runtime, evidence, confidence}
evidence carries the exact lines the tool printed. No evidence, no binding.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from ..core.models import AgentCapability, Anomaly, DeviceInfo
from ._proc import run_bounded


def discover_runtimes(
    devices: List[DeviceInfo], capability: AgentCapability
) -> Tuple[List[Dict[str, Any]], List[Anomaly]]:
    bindings: List[Dict[str, Any]] = []
    anomalies: List[Anomaly] = []
    if not devices:
        return bindings, anomalies

    cl: Dict[str, Any] = {}
    if "clinfo" in capability.tools:
        out = run_bounded(["clinfo", "--list"], timeout=15.0)
        if out:
            for line in out.splitlines():
                m = re.match(r"\s*Platform #\d+:\s*(.+)", line)
                if m:
                    cl["platform"] = m.group(1).strip()
                m = re.match(r"\s*(?:Device #\d+:\s*)?(.+?)\s*$", line)
                if m and "Platform" not in line and m.group(1).strip():
                    cl.setdefault("devices_raw", []).append(m.group(1).strip())
        else:
            anomalies.append(Anomaly("discovery.clinfo", "clinfo present but --list empty/failed"))

    vk: Dict[str, Any] = {}
    if "vulkaninfo" in capability.tools:
        out = run_bounded(["vulkaninfo", "--summary"], timeout=15.0)
        if out:
            names = re.findall(r"deviceName\s*=\s*(.+)", out)
            api = re.findall(r"apiVersion\s*=\s*(.+)", out)
            vk = {"device_names": [n.strip() for n in names], "api": api[0].strip() if api else None}
        else:
            anomalies.append(Anomaly("discovery.vulkaninfo", "vulkaninfo present but --summary empty/failed"))

    for dev in devices:
        found_runtime = False
        if "cuda" in (dev.runtimes or []):
            bindings.append(
                {
                    "device_class": _class_of(dev),
                    "runtime": "cuda",
                    "evidence": {"source": "nvidia-smi", "uuid": dev.uuid},
                    "confidence": 0.95,
                }
            )
            found_runtime = True
        dname = (dev.name or "").lower()
        cl_devs = [d.lower() for d in cl.get("devices_raw", []) or []]
        if cl_devs and any(
            dname.split()[0] in cd or cd.split()[0] in dname for cd in cl_devs if cd and dname
        ):
            bindings.append(
                {
                    "device_class": _class_of(dev),
                    "runtime": "opencl",
                    "evidence": {"clinfo_devices": cl_devs},
                    "confidence": 0.7,
                }
            )
            found_runtime = True
        vk_names = [n.lower() for n in vk.get("device_names", [])]
        if vk_names and any(dname and (dname in vn or vn in dname) for vn in vk_names):
            bindings.append(
                {
                    "device_class": _class_of(dev),
                    "runtime": "vulkan",
                    "evidence": {"vulkan": vk},
                    "confidence": 0.7,
                }
            )
            found_runtime = True
        if not found_runtime:
            bindings.append(
                {
                    "device_class": _class_of(dev),
                    "runtime": None,
                    "evidence": {},
                    "confidence": 0.0,
                }
            )
    return bindings, anomalies


def _class_of(dev: DeviceInfo) -> str:
    vendor = re.sub(r"[^a-z0-9]+", "", (dev.vendor or "unknown").lower())
    name = re.sub(r"[^a-z0-9]+", "_", (dev.name or "unknown").lower()).strip("_")
    return f"{vendor}:{name}"
