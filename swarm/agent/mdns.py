"""Raw mDNS announce + browse so swarm nodes find each other on a LAN with
zero daemons and zero dependencies.

Service type: _swarm._tcp.local. Instances: seed-<node12>._swarm._tcp.local.

Implemented with struct + raw multicast UDP on 224.0.0.251:5353 — no zeroconf.
Only the minimum DNS-writer/reader surface swarm needs (name compression is
only parsed, never emitted).
"""

from __future__ import annotations

import socket
import struct
from typing import List, Tuple

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
SERVICE_TYPE = "_swarm._tcp.local"
TYPEREC_PTR = 12
TYPEREC_SRV = 33


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


def _decode_name(packet: bytes, offset: int) -> Tuple[str, int]:
    labels: List[str] = []
    jumped = False
    while True:
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


def announce(instance: str) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.sendto(build_response_ptr(instance), (MDNS_GROUP, MDNS_PORT))
    finally:
        sock.close()
