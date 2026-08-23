"""Raw mDNS announce + browse so swarm nodes find each other on a LAN with
zero daemons and zero dependencies.

Service type: _swarm._tcp.local. Instances: seed-<node12>._swarm._tcp.local.

Implemented with struct + raw multicast UDP on 224.0.0.251:5353 — no zeroconf.
Only the minimum DNS-writer/reader surface swarm needs (name compression is
only parsed, never emitted).
"""

from __future__ import annotations

import contextlib
import socket
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
SERVICE_TYPE = "_swarm._tcp.local"
TYPEREC_A = 1
TYPEREC_PTR = 12
TYPEREC_TXT = 16
TYPEREC_SRV = 33
CLASS_IN = 1
DEFAULT_TTL = 120

#: Instance-name prefixes. A hub announces ``hub-<id>._swarm._tcp.local``; a
#: seed/agent announces ``seed-<id>._swarm._tcp.local``. Discovery uses the
#: prefix to tell "something that can be joined" from "another worker".
HUB_PREFIX = "hub-"
SEED_PREFIX = "seed-"


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        out += bytes([len(label)]) + label.encode("utf-8")
    return out + b"\x00"


def build_query(name: str, qtype: int = TYPEREC_PTR, qclass: int = 1) -> bytes:
    header = struct.pack(">6H", 0, 0, 1, 0, 0, 0)
    question = _encode_name(name) + struct.pack(">HH", qtype, qclass)
    return header + question


def build_response_ptr(instance: str, service: str = SERVICE_TYPE, ttl: int = 120) -> bytes:
    header = struct.pack(">6H", 0, 0x8400, 0, 1, 0, 0)
    answer = (
        _encode_name(service)
        + struct.pack(">HHIH", TYPEREC_PTR, 1, ttl, len(_encode_name(instance)))
        + _encode_name(instance)
    )
    return header + answer


MAX_NAME_HOPS = 128


def _decode_name(packet: bytes, offset: int) -> Tuple[str, int]:
    labels: List[str] = []
    jumped = False
    hops = 0
    while True:
        # Bounded: a hostile or corrupt packet can point a compression pointer
        # at itself. Well-formed names never come close to the limit; the
        # callers turn the raise into partial results. Never hang.
        hops += 1
        if hops > MAX_NAME_HOPS:
            raise ValueError("name decode exceeded hop limit (compression loop?)")
        length = packet[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            pointer = struct.unpack(">H", packet[offset : offset + 2])[0] & 0x3FFF
            if not jumped:
                offset += 2
                jumped = True
            offset = pointer
            continue
        offset += 1
        labels.append(packet[offset : offset + length].decode("utf-8", errors="replace"))
        offset += length
    return ".".join(labels), offset


def parse_response(packet: bytes) -> List[Tuple[str, str]]:
    """Extract (service, instance) pairs from PTR answers. Never raises."""
    pairs: List[Tuple[str, str]] = []
    try:
        qd, an = struct.unpack(">HH", packet[4:8])
        offset = 12
        for _ in range(qd):
            _, offset = _decode_name(packet, offset)
            offset += 4
        for _ in range(an):
            name, offset = _decode_name(packet, offset)
            rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", packet[offset : offset + 10])
            offset += 10
            if rtype == TYPEREC_PTR:
                instance, _ = _decode_name(packet, offset)
                pairs.append((name, instance))
            offset += rdlen
    except Exception:
        pass
    return pairs


def send_query(name: str = SERVICE_TYPE) -> None:
    data = build_query(name)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.sendto(data, (MDNS_GROUP, MDNS_PORT))
    finally:
        sock.close()


def announce(
    instance: str,
    host: Optional[str] = None,
    port: Optional[int] = None,
    address: Optional[str] = None,
    service: str = SERVICE_TYPE,
    ttl: int = DEFAULT_TTL,
) -> None:
    """Multicast one announcement for ``instance``.

    With no ``host``/``port`` this stays byte-identical to the original
    PTR-only announcement (the daemon's seed loop depends on that). Supply
    ``host``/``port``/``address`` and the packet becomes PTR + SRV + A, i.e. a
    *resolvable* service another machine can actually connect to.
    """
    if host is not None and port is not None:
        payload = build_response_service(
            instance, host=host, port=port, address=address, service=service, ttl=ttl
        )
    else:
        payload = build_response_ptr(instance, service=service, ttl=ttl)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.sendto(payload, (MDNS_GROUP, MDNS_PORT))
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# SRV / A record surface
# ---------------------------------------------------------------------------


def _record(name: str, rtype: int, rdata: bytes, ttl: int = DEFAULT_TTL, rclass: int = CLASS_IN) -> bytes:
    return _encode_name(name) + struct.pack(">HHIH", rtype, rclass, ttl, len(rdata)) + rdata


def build_srv_rdata(port: int, target: str, priority: int = 0, weight: int = 0) -> bytes:
    """SRV rdata: priority, weight, port, then the (uncompressed) target name."""
    return struct.pack(">HHH", priority, weight, port) + _encode_name(target)


def build_a_rdata(address: str) -> bytes:
    """A rdata: four packed bytes of an IPv4 address."""
    return socket.inet_aton(address)


def build_record_srv(
    instance: str, host: str, port: int, ttl: int = DEFAULT_TTL, priority: int = 0, weight: int = 0
) -> bytes:
    return _record(instance, TYPEREC_SRV, build_srv_rdata(port, host, priority, weight), ttl)


def build_record_a(host: str, address: str, ttl: int = DEFAULT_TTL) -> bytes:
    return _record(host, TYPEREC_A, build_a_rdata(address), ttl)


def build_response_service(
    instance: str,
    host: str,
    port: int,
    address: Optional[str] = None,
    service: str = SERVICE_TYPE,
    ttl: int = DEFAULT_TTL,
) -> bytes:
    """One packet that fully resolves ``instance`` -> host:port -> address.

    PTR (service -> instance) + SRV (instance -> host:port) + A (host ->
    address). Names are always written out in full; this writer never emits
    compression pointers (the reader understands them, per RFC 1035).
    """
    answers = [
        _record(service, TYPEREC_PTR, _encode_name(instance), ttl),
        build_record_srv(instance, host, port, ttl),
    ]
    if address:
        # Not a dotted-quad? announce without the A record rather than lying.
        with contextlib.suppress(OSError):
            answers.append(build_record_a(host, address, ttl))
    header = struct.pack(">6H", 0, 0x8400, 0, len(answers), 0, 0)
    return header + b"".join(answers)


# ---------------------------------------------------------------------------
# Structured parsing
# ---------------------------------------------------------------------------


@dataclass
class ServiceInstance:
    """A discovered ``_swarm._tcp`` instance, as far as the packet resolved it."""

    instance: str
    service: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    address: Optional[str] = None
    ttl: int = DEFAULT_TTL

    @property
    def resolved(self) -> bool:
        """True when we know somewhere to actually connect."""
        return self.port is not None and (self.address is not None or self.host is not None)

    @property
    def is_hub(self) -> bool:
        return self.instance.startswith(HUB_PREFIX)

    def base_url(self, scheme: str = "http") -> Optional[str]:
        """Reachable base URL, preferring the A-record address over the name."""
        if self.port is None:
            return None
        target = self.address or self.host
        if not target:
            return None
        return "{}://{}:{}".format(scheme, target.rstrip("."), self.port)


@dataclass
class _Parsed:
    ptr: List[Tuple[str, str]] = field(default_factory=list)
    srv: Dict[str, Tuple[str, int, int]] = field(default_factory=dict)
    addr: Dict[str, str] = field(default_factory=dict)
    order: List[str] = field(default_factory=list)


def _parse_srv_rdata(packet: bytes, offset: int) -> Optional[Tuple[str, int]]:
    _priority, _weight, port = struct.unpack(">HHH", packet[offset : offset + 6])
    target, _ = _decode_name(packet, offset + 6)
    return target, port


def parse_records(packet: bytes) -> List[ServiceInstance]:
    """Structured read of a swarm mDNS packet. Never raises.

    Walks answers + authority + additional (real responders scatter SRV/A into
    the additional section), joins PTR -> SRV -> A, and returns one
    :class:`ServiceInstance` per instance seen. A truncated or corrupt packet
    yields whatever was decodable before the damage — partial data beats a
    plausible-looking lie.
    """
    acc = _Parsed()
    try:
        qd, an, ns, ar = struct.unpack(">4H", packet[4:12])
        offset = 12
        for _ in range(qd):
            _, offset = _decode_name(packet, offset)
            offset += 4
        for _ in range(an + ns + ar):
            name, offset = _decode_name(packet, offset)
            rtype, _rclass, rttl, rdlen = struct.unpack(">HHIH", packet[offset : offset + 10])
            offset += 10
            if offset + rdlen > len(packet):
                raise ValueError("rdata past end of packet")
            if rtype == TYPEREC_PTR:
                instance, _ = _decode_name(packet, offset)
                acc.ptr.append((name, instance))
                if instance not in acc.order:
                    acc.order.append(instance)
            elif rtype == TYPEREC_SRV:
                got = _parse_srv_rdata(packet, offset)
                if got is not None:
                    target, port = got
                    acc.srv[name] = (target, port, int(rttl))
                    if name not in acc.order:
                        acc.order.append(name)
            elif rtype == TYPEREC_A and rdlen == 4:
                acc.addr[name] = socket.inet_ntoa(packet[offset : offset + 4])
            offset += rdlen
    except Exception:
        pass  # keep what we decoded; the caller sees partial truth, not an exception
    return _assemble(acc)


def _assemble(acc: _Parsed) -> List[ServiceInstance]:
    service_of: Dict[str, str] = {}
    for service, instance in acc.ptr:
        service_of.setdefault(instance, service)
    out: List[ServiceInstance] = []
    for instance in acc.order:
        host: Optional[str] = None
        port: Optional[int] = None
        ttl = DEFAULT_TTL
        srv = acc.srv.get(instance)
        if srv is not None:
            host, port, ttl = srv
        address = acc.addr.get(host) if host else None
        if address is None and host is None:
            address = acc.addr.get(instance)
        out.append(
            ServiceInstance(
                instance=instance,
                service=service_of.get(instance),
                host=host.rstrip(".") if host else None,
                port=port,
                address=address,
                ttl=ttl,
            )
        )
    return out


def instance_name(prefix: str, ident: str, service: str = SERVICE_TYPE) -> str:
    """``hub-abc123._swarm._tcp.local`` from ``("hub-", "abc123...")``."""
    safe = "".join(c for c in (ident or "unknown") if c.isalnum() or c in "-_")[:12] or "unknown"
    return "{}{}.{}".format(prefix, safe, service.rstrip("."))


def primary_ip() -> Optional[str]:
    """Best guess at the LAN address other machines can reach us on.

    Uses the connect-a-UDP-socket trick: no packet is sent, the kernel just
    resolves which interface would carry traffic to an off-link destination.
    Returns ``None`` rather than lying with ``127.0.0.1``.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.5)
        sock.connect(("8.8.8.8", 53))
        ip = sock.getsockname()[0]
    except Exception:
        ip = None
    finally:
        sock.close()
    if not ip or ip.startswith("127."):
        return None
    return str(ip)
