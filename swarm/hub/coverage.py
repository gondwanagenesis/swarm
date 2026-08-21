"""Device coverage: for every sensed device across the fleet, is there a
proven adapter, or is this device uncovered (unknown to the swarm)?

device_class is derived from the measured profile (vendor + device name,
normalized). An adapter covers a class only when it carries a gate_run_id —
an adapter that never passed its contract gate does not cover anything
(Law 4: calibrated verification).

Uncovered devices are not hidden; they surface on the dashboard and through
/api/coverage so the integrator (M4) knows exactly which adapters to write.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from .registry import Registry


def device_class(vendor: Any, name: Any) -> str:
    vendor_slug = re.sub(r"[^a-z0-9]+", "", str(vendor or "unknown").lower())
    name_slug = re.sub(r"[^a-z0-9]+", "_", str(name or "unknown").lower()).strip("_")
    return f"{vendor_slug}:{name_slug}"


def _profile_devices(registry: Registry) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for node in registry.list_nodes():
        detail = registry.node_detail(node["node_id"]) or {}
        try:
            profile = json.loads(detail.get("profile_json") or "{}")
        except ValueError:
            profile = {}
        for dev in profile.get("devices") or []:
            out.append(
                {
                    "node_id": node["node_id"],
                    "hostname": node["hostname"],
                    "kind": dev.get("kind"),
                    "vendor": dev.get("vendor"),
                    "name": dev.get("name"),
                    "device_class": device_class(dev.get("vendor"), dev.get("name")),
                }
            )
    return out


def coverage_report(registry: Registry) -> Dict[str, Any]:
    proven_classes = {a["device_class"] for a in registry.list_adapters() if a.get("gate_run_id")}
    devices = _profile_devices(registry)
    covered = [d for d in devices if d["device_class"] in proven_classes]
    uncovered = [d for d in devices if d["device_class"] not in proven_classes]
    return {
        "total_devices": len(devices),
        "covered": [d["device_class"] for d in covered],
        "uncovered": uncovered,
        "proven_adapter_count": len(proven_classes),
    }
