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

DEFAULT_INTERVAL_S = 30.0


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
    """Polls collect_devices on an interval; fires callbacks with the diff.
    Daemon thread; stopping is cooperative."""

    def __init__(self, interval: float = DEFAULT_INTERVAL_S) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._callbacks: List[Callable[[List[DeviceInfo], List[DeviceInfo]], None]] = []
        self._known: List[DeviceInfo] = []
        self._thread: Optional[threading.Thread] = None

    def on_change(self, fn: Callable[[List[DeviceInfo], List[DeviceInfo]], None]) -> None:
        self._callbacks.append(fn)

    def _loop(self) -> None:
        known, anomalies = collect_devices()
        self._known = known
        while not self._stop.wait(self.interval):
            try:
                current, anomalies = collect_devices()
            except Exception:
                continue
            added, removed, degraded = diff_devices(self._known, current)
            if added or removed:
                self._known = current
                for fn in self._callbacks:
                    try:
                        fn(added, removed)
                    except Exception:
                        continue

    def start(self) -> threading.Thread:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="swarm-hotplug")
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
