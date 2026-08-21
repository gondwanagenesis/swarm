"""Power collector. Trust ladder: wall plug > shunt > RAPL PSYS > RAPL package
> vendor tool > battery (discharging only) > estimated > none.

Rules: integrate energy counters rather than averaging instantaneous samples;
RAPL counters wrap at max_energy_range_uj (~262 J, not 2^32) — always take the
modulo so a wrap never yields a negative delta; a missing reading (Pi 4, most
Windows boxes) is trust=NONE, not an error.
"""

from __future__ import annotations

import glob
import os
import time
from typing import List, Optional, Tuple

from ..core.models import Anomaly, PowerReading, PowerTrust

RAPL_ROOT = "/sys/class/powercap"


def _read_int(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except Exception:
        return None


def rapl_snapshot() -> Optional[Tuple[float, float, float]]:
    """Returns (joules, max_range_joules, timestamp) for the package domain,
    or None. The caller takes deltas and applies the modulo."""
    if not os.path.isdir(RAPL_ROOT):
        return None
    domains = sorted(glob.glob(os.path.join(RAPL_ROOT, "intel-rapl:0")))
    if not domains:
        return None
    energy_uj = _read_int(os.path.join(domains[0], "energy_uj"))
    max_uj = _read_int(os.path.join(domains[0], "max_energy_range_uj"))
    if energy_uj is None:
        return None
    return (energy_uj / 1e6, (max_uj or 0) / 1e6, time.time())


def rapl_watts(
    before: Tuple[float, float, float], after: Tuple[float, float, float]
) -> Optional[float]:
    j0, max_j, t0 = before
    j1, _, t1 = after
    dt = t1 - t0
    if dt <= 0:
        return None
    dj = j1 - j0
    if dj < 0 and max_j > 0:
        dj = dj % max_j
    if dj < 0:
        return None
    return dj / dt


def _linux_battery() -> Optional[PowerReading]:
    for bat in sorted(glob.glob("/sys/class/power_supply/*")):
        if "bat" not in os.path.basename(bat).lower():
            continue
        status = None
        try:
            with open(os.path.join(bat, "status"), "r", encoding="utf-8") as fh:
                status = fh.read().strip().lower()
        except Exception:
            continue
        if status != "discharging":
            continue
        power_uw = _read_int(os.path.join(bat, "power_now"))
        if power_uw is not None:
            return PowerReading(watts=power_uw / 1e6, trust=PowerTrust.BATTERY)
        current = _read_int(os.path.join(bat, "current_now"))
        voltage = _read_int(os.path.join(bat, "voltage_now"))
        if current is not None and voltage is not None:
            return PowerReading(
                watts=(current / 1e6) * (voltage / 1e6), trust=PowerTrust.BATTERY
            )
    return None


def collect_power() -> Tuple[PowerReading, List[Anomaly]]:
    """Best-effort instantaneous reading. Returns trust=NONE when there is no
    visibility — that is an honest answer, not a failure."""
    anomalies: List[Anomaly] = []
    try:
        bat = _linux_battery()
        if bat is not None:
            return bat, anomalies
    except Exception as exc:
        anomalies.append(Anomaly("power.battery", f"collector crashed: {exc}", "error"))
    try:
        snap = rapl_snapshot()
        if snap is not None:
            return PowerReading(
                watts=None, trust=PowerTrust.RAPL_PACKAGE, integrated_joules=snap[0]
            ), anomalies
    except Exception as exc:
        anomalies.append(Anomaly("power.rapl", f"snapshot failed: {exc}", "error"))
    return PowerReading(watts=None, trust=PowerTrust.NONE), anomalies
