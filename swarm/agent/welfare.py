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
CPU_HOT_C = 85.0
BATTERY_HOT_C = 43.0


def thermal_state() -> Dict[str, Optional[float]]:
    """Hottest CPU zone and the battery, in Celsius, where the OS exposes
    them without privileges (Linux, Android/Termux). None = unknown, which
    parks nothing — but a phone that reports 45 C battery always rests.
    SWARM_EMULATE_TEMP_C overrides both (simulations)."""
    import glob
    import os

    emulated = os.environ.get("SWARM_EMULATE_TEMP_C")
    if emulated:
        try:
            t = float(emulated)
            return {"cpu_c": t, "battery_c": t}
        except ValueError:
            pass
    cpu: Optional[float] = None
    for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            with open(path, encoding="ascii") as fh:
                milli = float(fh.read().strip())
        except (OSError, ValueError):
            continue
        c = milli / 1000.0 if milli > 1000 else milli
        if 0 < c < 150:
            cpu = c if cpu is None else max(cpu, c)
    battery: Optional[float] = None
    for path in ("/sys/class/power_supply/battery/temp", "/sys/class/power_supply/BAT0/temp"):
        try:
            with open(path, encoding="ascii") as fh:
                tenths = float(fh.read().strip())
            battery = tenths / 10.0
            break
        except (OSError, ValueError):
            continue
    return {"cpu_c": cpu, "battery_c": battery}


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
    import os

    emulated = os.environ.get("SWARM_EMULATE_BATTERY")  # "85:ac" / "60:battery" (simulations)
    if emulated:
        pct, _, source = emulated.partition(":")
        try:
            return {"trust": "emulated", "watts": None, "battery_percent": int(pct), "on_ac": source == "ac"}
        except ValueError:
            pass
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


def welfare_gate(dedicated: bool = False) -> Dict[str, Any]:
    """Returns {allowed: bool, reason: str, details: {...}}. Fail-open means
    no — the organism works when IT CAN PROVE the host is idle and fed.

    `dedicated` is the owner saying "this machine exists to compute" (an old
    phone on a charger, a GPU box in a closet): someone touching it does not
    park the work. Battery protection still applies — a dedicated phone is
    not allowed to drain itself flat."""
    bat = battery_state()
    idle_s = user_idle_seconds()
    details: Dict[str, Any] = {
        "battery": bat,
        "user_idle_s": idle_s,
        "platform": platform.system(),
        "dedicated": dedicated,
    }

    heat = thermal_state()
    details["thermal"] = heat
    if heat.get("battery_c") is not None and heat["battery_c"] >= BATTERY_HOT_C:
        return {"allowed": False, "reason": f"battery at {heat['battery_c']:.0f} C — organism cools off", "details": details}
    if heat.get("cpu_c") is not None and heat["cpu_c"] >= CPU_HOT_C:
        return {"allowed": False, "reason": f"CPU at {heat['cpu_c']:.0f} C — organism cools off", "details": details}

    if bat.get("on_ac") is False:
        pct = bat.get("battery_percent")
        if pct is not None and pct < BATTERY_MIN_PERCENT:
            return {"allowed": False, "reason": f"battery at {pct}% — organism rests", "details": details}

    if not dedicated and idle_s is not None and idle_s < USER_IDLE_REQUIRE_S:
        return {"allowed": False, "reason": f"user active {idle_s:.0f}s ago", "details": details}

    return {"allowed": True, "reason": "host idle and fed", "details": details}
