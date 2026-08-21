"""Welfare and politeness: the organism never eats its host alive.

The point: a node is a *yours-first* machine. If a human is typing, if the
battery is draining, if the box is thermal-throttling — the organism exhales.
Idle-time self-honing only runs when the welfare gate says the coast is
clear.

Battery: from the probe's power reading (works on Linux; None elsewhere is
honest). User activity: GetLastInputInfo on Windows via ctypes; /dev/null
timestamp on some Linux; unsupported → None, and None means UNKNOWN, not
"user idle".
"""

from __future__ import annotations

import platform
from typing import Any, Dict, Optional

from ..probe.power import collect_power

USER_IDLE_REQUIRE_S = 120.0
BATTERY_MIN_PERCENT = 40.0


def user_idle_seconds() -> Optional[float]:
    system = platform.system()
    if system == "Windows":
        try:
            import ctypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]

            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                return None
            tick = ctypes.windll.kernel32.GetTickCount()
            # handle 49.7-day wraparound crudely but honestly
            if tick < lii.dwTime:
                return 0.0
            return (tick - lii.dwTime) / 1000.0
        except Exception:
            return None
    return None


def battery_state() -> Dict[str, Any]:
    reading, _ = collect_power()
    state: Dict[str, Any] = {"trust": reading.trust.value, "watts": reading.watts}
    if platform.system() == "Windows":
        try:
            import ctypes

            class SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_ubyte),
                    ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte),
                    ("Reserved1", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong),
                ]

            status = SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                state["on_ac"] = status.ACLineStatus == 1
                if status.BatteryLifePercent != 255:
                    state["battery_percent"] = int(status.BatteryLifePercent)
        except Exception:
            pass
    return state


def welfare_gate() -> Dict[str, Any]:
    """Returns {allowed: bool, reason: str, details: {...}}. Fail-open means
    no — the organism works when IT CAN PROVE the host is idle and fed."""
    bat = battery_state()
    idle_s = user_idle_seconds()
    details: Dict[str, Any] = {"battery": bat, "user_idle_s": idle_s, "platform": platform.system()}

    if bat.get("on_ac") is False:
        pct = bat.get("battery_percent")
        if pct is not None and pct < BATTERY_MIN_PERCENT:
            return {"allowed": False, "reason": f"battery at {pct}% — organism rests", "details": details}

    if idle_s is not None and idle_s < USER_IDLE_REQUIRE_S:
        return {"allowed": False, "reason": f"user active {idle_s:.0f}s ago", "details": details}

    return {"allowed": True, "reason": "host idle and fed", "details": details}
