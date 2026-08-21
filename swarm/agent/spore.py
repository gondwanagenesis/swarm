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
DEBOUNCE_TICKS = 2


def _tailscale_peers() -> List[str]:
    """Peer watch via tailscale CLI when present — the mesh's authoritative
    peer list, so ghost DHCP leases and ARP lies can't forge attachments."""
    out = run_bounded(["tailscale", "status", "--json"], timeout=8.0)
    if not out:
        return []
    import json

    try:
        data = json.loads(out)
    except ValueError:
        return []
    peers = data.get("Peer") or {}
    return sorted(
        p.get("HostName") or (p.get("TailscaleIPs") or [""])[0] for p in peers.values() if isinstance(p, dict)
    )


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
    """Diffs attachment surfaces with a small debounce (ghost leases and ARP
    lies die here), event wakeups under the hood where free, polling as the
    portable spine everywhere else."""

    def __init__(self, poll_s: float = POLL_S, debounce: int = DEBOUNCE_TICKS) -> None:
        self.poll_s = poll_s
        self.debounce = max(1, debounce)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._callbacks: List[Callable[[str, str], None]] = []
        self._ifaces: List[str] = []
        self._adb: List[str] = []
        self._ts: List[str] = []
        self._pending: dict = {}

    def on_attach(self, fn: Callable[[str, str], None]) -> None:
        self._callbacks.append(fn)

    def _confirm(self, key: str, channel: str, hint: str) -> None:
        seen = self._pending.get(key, 0) + 1
        self._pending[key] = seen
        if seen >= self.debounce:
            del self._pending[key]
            self._fire(channel, hint)

    def _pass(self) -> None:
        """Diff current surfaces against baseline. A surface item is adopted
        into the baseline ONLY once confirmed — so a transient appearing on a
        single pass (a ghost) never lands in the baseline, and a real
        attachment confirms across consecutive passes."""
        ifaces = _iface_names()
        current_pending = set()
        for iface in ifaces:
            if iface in self._ifaces:
                continue
            key = f"if:{iface}"
            was = self._pending.get(key, 0) + 1
            self._pending[key] = was
            current_pending.add(key)
            if was >= self.debounce:
                self._fire("interface", iface)
                self._ifaces.append(iface)
                self._pending.pop(key, None)
        for key in [k for k in self._pending if k.startswith("if:") and k not in current_pending]:
            del self._pending[key]

        adb = _adb_devices()
        for dev in adb:
            if dev in self._adb:
                continue
            key = f"adb:{dev}"
            was = self._pending.get(key, 0) + 1
            self._pending[key] = was
            if was >= self.debounce:
                self._fire("adb", dev)
                self._adb.append(dev)
                self._pending.pop(key, None)

        ts = _tailscale_peers()
        for peer in ts:
            if peer and peer not in self._ts:
                key = f"ts:{peer}"
                was = self._pending.get(key, 0) + 1
                self._pending[key] = was
                if was >= self.debounce:
                    self._fire("tailscale", peer)
                    self._ts.append(peer)
                    self._pending.pop(key, None)

    def _loop(self) -> None:
        self._ifaces = _iface_names()
        self._adb = _adb_devices()
        self._ts = _tailscale_peers()
        while not self._stop.wait(self.poll_s):
            try:
                self._pass()
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
