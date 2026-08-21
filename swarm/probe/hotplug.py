"""Hot-plug watcher: notice hardware that arrives after the agent started.

Polling, not OS-event subscriptions — udev/WMI/IOKit event APIs need native
bindings we deliberately avoid (stdlib only). The device collector is cheap
and idempotent, so diffing two polls is honest and works identically on
Windows, Linux, macOS, and Termux.

Identity for diffing: DeviceInfo.merge_key (PCI address or UUID), falling
back to vendor+name when a source doesn't expose either — flagged as lower
confidence in that case, because name-only identity can alias two identical
devices into one.
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional, Tuple

from ..core.models import DeviceInfo
from .gpu import collect_devices

DEFAULT_INTERVAL_S = 5.0
MIN_INTERVAL_S = 2.0
MAX_INTERVAL_S = 60.0


def _diff_key(dev: DeviceInfo) -> str:
    key = dev.merge_key
    if key:
        return key
    return "name:%s/%s" % ((dev.vendor or "?").lower(), (dev.name or "?").lower())


def diff_devices(
    before: List[DeviceInfo], after: List[DeviceInfo]
) -> Tuple[List[DeviceInfo], List[DeviceInfo], List[DeviceInfo]]:
    """Returns (added, removed, identity_degraded) between two inventories."""
    b: Dict[str, DeviceInfo] = {_diff_key(d): d for d in before}
    a: Dict[str, DeviceInfo] = {_diff_key(d): d for d in after}
    added = [a[k] for k in a.keys() - b.keys()]
    removed = [b[k] for k in b.keys() - a.keys()]
    degraded = [d for d in list(added) + list(removed) if d.merge_key is None]
    return added, removed, degraded


class HotplugWatcher:
    """Poll-diff watcher with adaptive backoff: fast tick after a change,
    relaxes toward MAX_INTERVAL_S when the inventory is stable. Polling is the
    portable spine (works identically on Windows/Linux/macOS/Termux); the
    adaptivity keeps the CPU cost near zero while idle."""

    def __init__(self, interval: float = DEFAULT_INTERVAL_S) -> None:
        self.base_interval = interval
        self._stop = threading.Event()
        self._callbacks: List[Callable[[List[DeviceInfo], List[DeviceInfo]], None]] = []
        self._known: List[DeviceInfo] = []
        self._thread: Optional[threading.Thread] = None

    def on_change(self, fn: Callable[[List[DeviceInfo], List[DeviceInfo]], None]) -> None:
        self._callbacks.append(fn)

    def _loop(self) -> None:
        known, anomalies = collect_devices()
        self._known = known
        interval = self.base_interval
        while not self._stop.wait(interval):
            try:
                current, anomalies = collect_devices()
            except Exception:
                continue
            added, removed, degraded = diff_devices(self._known, current)
            if added or removed:
                self._known = current
                interval = MIN_INTERVAL_S
                for fn in self._callbacks:
                    try:
                        fn(added, removed)
                    except Exception:
                        continue
            else:
                interval = min(MAX_INTERVAL_S, interval * 1.5)

    def start(self) -> threading.Thread:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="swarm-hotplug")
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
