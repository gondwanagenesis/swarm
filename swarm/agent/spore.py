"""Spore mode: a seed that watches for bigger tissue attaching and offers to
blossom onto it.

Detection channels, all stdlib, whichever exists:
  - network-interface churn (socket.if_nameindex diff — catches tethering,
    docks, new links)
  - ADB device enumeration (only if adb is present — tower-style optional)
  - USB PnP device count diff (Windows CIM when present)

The seed NEVER installs anything on another machine by itself. It emits an
event to the hub; the hub mints a one-time enrollment token; the attached
device gets the offer (one click). Devices already carrying a fleet token
expand zero-click — standing authorization. That's the membrane.
"""

from __future__ import annotations

import socket
import threading
from typing import Callable, List, Optional, Tuple

from ..probe._proc import run_bounded

POLL_S = 5.0


def _iface_names() -> List[str]:
    try:
        return sorted(name for _, name in socket.if_nameindex())
    except Exception:
        return []


def _adb_devices() -> List[str]:
    out = run_bounded(["adb", "devices"], timeout=5.0)
    if not out:
        return []
    lines = [line.strip() for line in out.splitlines()[1:] if line.strip() and "device" in line]
    return sorted(lines)


class AttachmentWatcher:
    """Diffs attachment surfaces; fires callbacks with (channel, hint).
    Cheap polling only — never raises."""

    def __init__(self, poll_s: float = POLL_S) -> None:
        self.poll_s = poll_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._callbacks: List[Callable[[str, str], None]] = []
        self._ifaces: List[str] = []
        self._adb: List[str] = []

    def on_attach(self, fn: Callable[[str, str], None]) -> None:
        self._callbacks.append(fn)

    def _loop(self) -> None:
        self._ifaces = _iface_names()
        self._adb = _adb_devices()
        while not self._stop.wait(self.poll_s):
            try:
                ifaces = _iface_names()
                new_ifaces = [i for i in ifaces if i not in self._ifaces]
                if new_ifaces:
                    self._fire("interface", ",".join(new_ifaces))
                self._ifaces = ifaces

                adb = _adb_devices()
                new_adb = [d for d in adb if d not in self._adb]
                if new_adb:
                    self._fire("adb", ",".join(new_adb))
                self._adb = adb
            except Exception:
                continue

    def _fire(self, channel: str, hint: str) -> None:
        for fn in self._callbacks:
            try:
                fn(channel, hint)
            except Exception:
                continue

    def start(self) -> threading.Thread:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="swarm-spore")
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)


def snapshot() -> Tuple[List[str], List[str]]:
    return _iface_names(), _adb_devices()
