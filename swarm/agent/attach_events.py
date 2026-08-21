"""Event-grade attachment wakeups, platform-native and dependency-free.

Linux: udev kernel uevents + link up/down over raw netlink (AF_NETLINK,
KOBJECT_UEVENT) — no pyudev needed, the events ARE the socket bytes.
Windows: iphlpapi NotifyAddrChange via ctypes — fires on address/interface
change; threads block on it and wake the watcher.
Others: None — the poll loop in spore stays the spine there.

All constructors return None when unavailable; every reader degrades to poll.
"""

from __future__ import annotations

import platform
from typing import Optional


def linux_uevent_socket():
    """AF_NETLINK KOBJECT uevent socket, or None. Stdlib only."""
    if platform.system() != "Linux":
        return None
    import socket

    NETLINK_KOBJECT_UEVENT = 15
    try:
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_KOBJECT_UEVENT)
        sock.bind((-1, 1))
        sock.settimeout(1.0)
        return sock
    except (OSError, PermissionError):
        return None


def read_uevent(sock) -> Optional[bytes]:
    try:
        return sock.recv(4096)
    except Exception:
        return None


def windows_notify_addr_change():
    """Blocks until an interface/address change, then returns. Returns True if
    change observed, None if unsupported. Stdlib ctypes on iphlpapi."""
    if platform.system() != "Windows":
        return None
    import ctypes

    try:
        iphlpapi = ctypes.windll.iphlpapi
        handle = ctypes.wintypes.HANDLE()
    except Exception:
        return None
    try:
        overlapped = ctypes.wintypes.OVERLAPPED()
        overlapped.Internal = 0
        overlapped.InternalHigh = 0
        overlapped.Offset = 0
        overlapped.OffsetHigh = 0
        overlapped.hEvent = ctypes.windll.kernel32.CreateEventW(None, True, False, None)
        ret = iphlpapi.NotifyAddrChange(ctypes.byref(handle), ctypes.byref(overlapped))
        if ret != 0:  # NOERROR
            return None
        ctypes.windll.kernel32.WaitForSingleObject(overlapped.hEvent, 30000)
        ctypes.windll.kernel32.CloseHandle(overlapped.hEvent)
        return True
    except Exception:
        return None
