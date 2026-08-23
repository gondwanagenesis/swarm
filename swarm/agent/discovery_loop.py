"""Browse half of swarm's raw mDNS: listen, query, keep a peer table.

``mdns.py`` could already write and read the packets; nothing ever listened.
This module joins ``224.0.0.251:5353``, sends periodic PTR queries for
``_swarm._tcp.local``, and maintains a TTL-expiring table of the instances
that answered.

**Discovery never enrolls.** This module returns candidate addresses and
nothing else. It does not register, does not authenticate, does not install,
and does not contact a discovered hub in any way. Joining a swarm still goes
through the existing enrollment-token / consent path in ``daemon.py`` and
``hub/enrollment.py``. Finding a machine is not permission to touch it
(AGENTS.md: "No self-propagation").

**Degradation is expected, not exceptional.** Multicast is dropped by plenty
of networks (guest wifi, client isolation, most cloud VPCs, many container
bridges), and ``IP_ADD_MEMBERSHIP`` routinely fails under WSL and on Windows
hosts with no default multicast route. Every socket call here is bounded and
wrapped: failure means "no peers discovered" plus a recorded
:class:`~swarm.core.models.Anomaly` explaining why. It never crashes and it
never hangs.
"""

from __future__ import annotations

import contextlib
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from ..core.models import Anomaly, Severity
from .mdns import (
    MDNS_GROUP,
    MDNS_PORT,
    SERVICE_TYPE,
    ServiceInstance,
    build_query,
    parse_records,
)

SOURCE = "mdns_discovery"
RECV_BUF = 9000
POLL_TIMEOUT_S = 0.5


# ---------------------------------------------------------------------------
# Peer table
# ---------------------------------------------------------------------------


@dataclass
class Peer:
    """A swarm instance seen on the LAN, with the moment we last heard it."""

    instance: str
    host: Optional[str] = None
    port: Optional[int] = None
    address: Optional[str] = None
    ttl: float = 120.0
    last_seen: float = 0.0
    source_ip: Optional[str] = None

    @property
    def expires_at(self) -> float:
        return self.last_seen + self.ttl

    def expired(self, now: Optional[float] = None) -> bool:
        return (time.time() if now is None else now) >= self.expires_at

    @property
    def is_hub(self) -> bool:
        from .mdns import HUB_PREFIX

        return self.instance.startswith(HUB_PREFIX)

    def base_url(self, scheme: str = "http") -> Optional[str]:
        if self.port is None:
            return None
        target = self.address or self.source_ip or self.host
        if not target:
            return None
        return "{}://{}:{}".format(scheme, str(target).rstrip("."), self.port)


class PeerTable:
    """Thread-safe instance -> :class:`Peer` map with TTL expiry."""

    def __init__(self, default_ttl: float = 120.0) -> None:
        self.default_ttl = float(default_ttl)
        self._peers: Dict[str, Peer] = {}
        self._lock = threading.Lock()

    def observe(
        self,
        found: ServiceInstance,
        source_ip: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Peer:
        stamp = time.time() if now is None else now
        ttl = float(found.ttl) if found.ttl else self.default_ttl
        with self._lock:
            peer = self._peers.get(found.instance)
            if peer is None:
                peer = Peer(instance=found.instance)
                self._peers[found.instance] = peer
            # Never let a later, thinner record blank out what we already know.
            peer.host = found.host or peer.host
            peer.port = found.port if found.port is not None else peer.port
            peer.address = found.address or peer.address
            peer.source_ip = source_ip or peer.source_ip
            peer.ttl = ttl
            peer.last_seen = stamp
            return peer

    def prune(self, now: Optional[float] = None) -> List[str]:
        """Drop expired peers. Returns the instance names that were dropped."""
        stamp = time.time() if now is None else now
        with self._lock:
            dead = [name for name, peer in self._peers.items() if peer.expired(stamp)]
            for name in dead:
                del self._peers[name]
        return dead

    def peers(self, now: Optional[float] = None) -> List[Peer]:
        self.prune(now)
        with self._lock:
            return sorted(self._peers.values(), key=lambda p: p.instance)

    def hubs(self, now: Optional[float] = None) -> List[Peer]:
        return [p for p in self.peers(now) if p.is_hub and p.base_url()]

    def clear(self) -> None:
        with self._lock:
            self._peers.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._peers)


# ---------------------------------------------------------------------------
# Socket plumbing — every call bounded, every failure named
# ---------------------------------------------------------------------------


def _anomaly(message: str, severity: str = Severity.WARNING.value) -> Anomaly:
    return Anomaly(source=SOURCE, message=message, severity=severity)


def open_listener(
    group: str = MDNS_GROUP, port: int = MDNS_PORT
) -> Tuple[Optional[socket.socket], List[Anomaly]]:
    """Open a socket joined to the mDNS group, degrading rather than failing.

    Ladder, best first:

    1. bind ``('', 5353)`` + ``IP_ADD_MEMBERSHIP`` — sees multicast responses.
    2. bind ``('', 0)`` + membership — a local responder already holds 5353;
       we still catch unicast replies to our own queries.
    3. no membership — multicast join refused (common under WSL); the socket
       is kept for unicast, and the reason is recorded.

    Returns ``(sock_or_None, anomalies)``. Never raises.
    """
    notes: List[Anomaly] = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    except OSError as exc:
        return None, [_anomaly("udp socket unavailable: {}".format(exc), Severity.ERROR.value)]

    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError as exc:
        notes.append(_anomaly("SO_REUSEADDR rejected: {}".format(exc)))
    reuse_port = getattr(socket, "SO_REUSEPORT", None)
    if reuse_port is not None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, reuse_port, 1)
        except OSError as exc:
            notes.append(_anomaly("SO_REUSEPORT rejected: {}".format(exc)))

    bound = False
    try:
        sock.bind(("", port))
        bound = True
    except OSError as exc:
        notes.append(
            _anomaly("bind :{} refused ({}); falling back to an ephemeral port".format(port, exc))
        )
        try:
            sock.bind(("", 0))
            bound = True
        except OSError as exc2:
            notes.append(_anomaly("ephemeral bind failed: {}".format(exc2), Severity.ERROR.value))

    if not bound:
        with contextlib.suppress(OSError):
            sock.close()
        return None, notes

    try:
        mreq = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError as exc:
        # WSL / restricted hosts / no multicast route. Unicast still works.
        notes.append(_anomaly("IP_ADD_MEMBERSHIP failed ({}); unicast replies only".format(exc)))
    except AttributeError as exc:  # pragma: no cover - exotic platforms
        notes.append(_anomaly("multicast constants missing ({})".format(exc)))

    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    except OSError as exc:
        notes.append(_anomaly("IP_MULTICAST_TTL rejected: {}".format(exc)))

    try:
        sock.settimeout(POLL_TIMEOUT_S)
    except OSError as exc:  # pragma: no cover
        notes.append(_anomaly("settimeout failed: {}".format(exc)))

    return sock, notes


def _send_query(sock: socket.socket, service: str) -> Optional[Anomaly]:
    try:
        sock.sendto(build_query(service), (MDNS_GROUP, MDNS_PORT))
        return None
    except OSError as exc:
        return _anomaly("query send failed ({}); multicast is likely blocked".format(exc))


def _drain(
    sock: socket.socket, deadline: float, on_packet: Callable[[bytes, Optional[str]], None]
) -> List[Anomaly]:
    """Read until ``deadline``. Interruptible, never blocks past the timeout."""
    notes: List[Anomaly] = []
    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(RECV_BUF)
        except socket.timeout:
            continue
        except OSError as exc:
            notes.append(_anomaly("recv failed: {}".format(exc)))
            break
        try:
            on_packet(data, addr[0] if addr else None)
        except Exception as exc:  # a bad packet must never kill the loop
            notes.append(_anomaly("packet handling failed: {}".format(exc)))
    return notes


# ---------------------------------------------------------------------------
# One-shot browse
# ---------------------------------------------------------------------------


def discover_peers(
    timeout_s: float = 3.0,
    service: str = SERVICE_TYPE,
    anomalies: Optional[List[Anomaly]] = None,
) -> List[Peer]:
    """Query once, collect for ``timeout_s``, return what answered.

    Returns ``[]`` (never raises) when multicast is unavailable. Pass a list as
    ``anomalies`` to receive the reasons.
    """
    notes = anomalies if anomalies is not None else []
    table = PeerTable()
    sock, open_notes = open_listener()
    notes.extend(open_notes)
    if sock is None:
        return []
    try:
        sent_err = _send_query(sock, service)
        if sent_err is not None:
            notes.append(sent_err)

        def handle(data: bytes, src: Optional[str]) -> None:
            for found in parse_records(data):
                table.observe(found, source_ip=src)

        notes.extend(_drain(sock, time.time() + max(0.0, float(timeout_s)), handle))
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    return table.peers()


def discover_hub(
    timeout_s: float = 3.0,
    service: str = SERVICE_TYPE,
    anomalies: Optional[List[Anomaly]] = None,
) -> Optional[str]:
    """Base URL of a swarm hub found on the LAN, or ``None``.

    This is what lets an agent start with no ``--hub`` argument. Returning a
    URL is *not* an invitation: the caller still has to enroll through the
    token/consent path. ``None`` when nothing answered, when multicast is
    blocked, or when the only answers were unresolvable — never a guess.
    """
    for peer in discover_peers(timeout_s, service, anomalies):
        if peer.is_hub:
            url = peer.base_url()
            if url:
                return url
    return None


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------


@dataclass
class DiscoveryStatus:
    """Honest snapshot: what we found, and what stopped us finding more."""

    running: bool = False
    listening: bool = False
    queries_sent: int = 0
    packets_seen: int = 0
    peer_count: int = 0
    anomalies: List[Anomaly] = field(default_factory=list)


class DiscoveryLoop:
    """Background mDNS browser keeping a fresh peer table.

    Threading follows the announce loop in ``daemon.py``: a daemon thread and
    a ``threading.Event`` that the body waits on (``self._stop.wait(...)``),
    so ``stop()`` returns promptly instead of sleeping out an interval.

    Discovery only observes. Nothing here enrolls, installs, or recruits.
    """

    def __init__(
        self,
        service: str = SERVICE_TYPE,
        query_interval: float = 30.0,
        default_ttl: float = 120.0,
    ) -> None:
        self.service = service
        self.query_interval = float(query_interval)
        self.table = PeerTable(default_ttl=default_ttl)
        self.anomalies: List[Anomaly] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._listening = False
        self._queries_sent = 0
        self._packets_seen = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> threading.Thread:
        self._stop.clear()
        thread = threading.Thread(target=self._run, daemon=True, name="swarm-mdns-browse")
        self._thread = thread
        thread.start()
        return thread

    def stop(self, join_timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
        self._thread = None

    def __enter__(self) -> "DiscoveryLoop":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- reads -------------------------------------------------------------

    def peers(self) -> List[Peer]:
        return self.table.peers()

    def hubs(self) -> List[Peer]:
        return self.table.hubs()

    def hub_url(self) -> Optional[str]:
        for peer in self.table.hubs():
            url = peer.base_url()
            if url:
                return url
        return None

    def status(self) -> DiscoveryStatus:
        with self._lock:
            return DiscoveryStatus(
                running=self._thread is not None and self._thread.is_alive(),
                listening=self._listening,
                queries_sent=self._queries_sent,
                packets_seen=self._packets_seen,
                peer_count=len(self.table),
                anomalies=list(self.anomalies),
            )

    def _note(self, items: List[Anomaly]) -> None:
        if not items:
            return
        with self._lock:
            seen = {(a.source, a.message) for a in self.anomalies}
            for item in items:
                if (item.source, item.message) not in seen:
                    self.anomalies.append(item)
                    seen.add((item.source, item.message))
            del self.anomalies[:-50]  # bounded; keep the most recent

    # -- body --------------------------------------------------------------

    def _run(self) -> None:
        sock, notes = open_listener()
        self._note(notes)
        if sock is None:
            self._note([_anomaly("discovery disabled: no usable multicast socket")])
            # Nothing to listen on. Idle politely until stopped rather than
            # spinning; the anomaly above is the honest answer to "why no peers".
            self._stop.wait()
            return
        with self._lock:
            self._listening = True

        def handle(data: bytes, src: Optional[str]) -> None:
            with self._lock:
                self._packets_seen += 1
            for found in parse_records(data):
                self.table.observe(found, source_ip=src)

        try:
            next_query = 0.0
            while not self._stop.is_set():
                now = time.time()
                if now >= next_query:
                    err = _send_query(sock, self.service)
                    if err is not None:
                        self._note([err])
                    else:
                        with self._lock:
                            self._queries_sent += 1
                    next_query = now + self.query_interval
                # Bounded read window; recv itself is on a short socket timeout
                # so the stop event is honoured within POLL_TIMEOUT_S.
                window = min(self.query_interval, POLL_TIMEOUT_S * 2)
                deadline = time.time() + window
                while not self._stop.is_set() and time.time() < deadline:
                    try:
                        data, addr = sock.recvfrom(RECV_BUF)
                    except socket.timeout:
                        continue
                    except OSError as exc:
                        self._note([_anomaly("recv failed: {}".format(exc))])
                        self._stop.wait(1.0)
                        break
                    try:
                        handle(data, addr[0] if addr else None)
                    except Exception as exc:
                        self._note([_anomaly("packet handling failed: {}".format(exc))])
                self.table.prune()
        except Exception as exc:  # last-ditch: a browse loop never takes the agent down
            self._note([_anomaly("browse loop aborted: {}".format(exc), Severity.ERROR.value)])
        finally:
            with self._lock:
                self._listening = False
            with contextlib.suppress(OSError):
                sock.close()


def multicast_available() -> bool:
    """True when a listener socket can be opened at all. For guards and tests."""
    sock, _notes = open_listener()
    if sock is None:
        return False
    with contextlib.suppress(OSError):
        sock.close()
    return True
