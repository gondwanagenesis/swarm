"""Link measurement between this node and a hub/peer. Measured, not declared:
RTT percentiles from repeated probes, bandwidth from a real transfer, and the
direct-vs-relayed property tagged only when there is evidence.

Never raises; a dead link yields None fields and an anomaly, not an exception.
"""

from __future__ import annotations

import http.client
import statistics
import time
from typing import List, Optional, Tuple

from ..core.models import Anomaly, LinkMeasurement, MeasurementTrust

RTT_PROBES = 12
BANDWIDTH_PAYLOAD = 2 * 1024 * 1024


def _percentile(samples: List[float], pct: float) -> Optional[float]:
    if not samples:
        return None
    ordered = sorted(samples)
    k = max(0, min(len(ordered) - 1, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[k]


def _is_mesh_or_lan(host: str) -> Optional[bool]:
    parts = host.split(".")
    if len(parts) != 4:
        return None
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if a == 100 and 64 <= b < 128:
        return True
    return bool(host in ("127.0.0.1", "localhost") or a == 10 or (a == 172 and 16 <= b < 32) or (a == 192 and b == 168))


class LinkProber:
    def __init__(self, host: str, port: int, timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.anomalies: List[Anomaly] = []

    def _ping_once(self) -> Optional[float]:
        t0 = time.perf_counter()
        try:
            conn = http.client.HTTPConnection(
                self.host, self.port, timeout=self.timeout
            )
            conn.request("GET", "/api/ping")
            resp = conn.getresponse()
            resp.read()
            conn.close()
        except Exception:
            return None
        return (time.perf_counter() - t0) * 1000.0

    def measure_rtt(
        self, probes: int = RTT_PROBES
    ) -> Tuple[Optional[float], Optional[float]]:
        samples: List[float] = []
        for _ in range(probes):
            rtt = self._ping_once()
            if rtt is not None:
                samples.append(rtt)
            else:
                self.anomalies.append(Anomaly("link.rtt", "probe dropped", "info"))
        if not samples:
            self.anomalies.append(Anomaly("link.rtt", "no responses from host"))
            return None, None
        return statistics.median(samples), _percentile(samples, 95)

    def measure_bandwidth(
        self, payload_bytes: int = BANDWIDTH_PAYLOAD
    ) -> Optional[float]:
        body = b"\xab" * payload_bytes
        t0 = time.perf_counter()
        try:
            conn = http.client.HTTPConnection(
                self.host, self.port, timeout=max(self.timeout, 30.0)
            )
            conn.request(
                "POST",
                "/api/echo",
                body=body,
                headers={"Content-Type": "application/octet-stream"},
            )
            resp = conn.getresponse()
            got = resp.read()
            conn.close()
        except Exception as exc:
            self.anomalies.append(Anomaly("link.bandwidth", f"transfer failed: {exc}"))
            return None
        dt = time.perf_counter() - t0
        if dt <= 0 or len(got) != payload_bytes:
            self.anomalies.append(Anomaly("link.bandwidth", "short or slow transfer"))
            return None
        return payload_bytes * 8 / dt

    def probe(self, src_node: str, dst_node: str = "hub") -> LinkMeasurement:
        p50, p95 = self.measure_rtt()
        bw = self.measure_bandwidth() if p50 is not None else None
        trust = (
            MeasurementTrust.STANDARD
            if p50 is not None
            else MeasurementTrust.THEORETICAL
        )
        return LinkMeasurement(
            src_node=src_node,
            dst_node=dst_node,
            rtt_p50_ms=round(p50, 3) if p50 is not None else None,
            rtt_p95_ms=round(p95, 3) if p95 is not None else None,
            bandwidth_bps=round(bw, 1) if bw is not None else None,
            direct=_is_mesh_or_lan(self.host),
            trust=trust,
            measured_at=time.time(),
        )
