"""Hub registry: the swarm's record of what was measured.

SQLite via stdlib. Profiles are stored as canonical JSON (serde.dumps) so the
stored bytes are exactly what the node sent. The adapter registry schema —
content-hash identity plus the full provenance chain — exists now even though
the integrator (M4) doesn't; tables are cheap, rewrites are not.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..core.models import BenchResult, LinkMeasurement, NodeProfile
from ._sync import synchronized

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    hostname TEXT,
    os TEXT,
    arch TEXT,
    profile_json TEXT,
    capability_json TEXT,
    registered_at REAL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS bench_runs (
    run_id TEXT PRIMARY KEY,
    node_id TEXT,
    name TEXT,
    value REAL,
    unit TEXT,
    trust TEXT,
    variance REAL,
    sustained_ratio REAL,
    duration_s REAL,
    at REAL
);
CREATE TABLE IF NOT EXISTS links (
    src_node TEXT,
    dst_node TEXT,
    rtt_p50_ms REAL,
    rtt_p95_ms REAL,
    bandwidth_bps REAL,
    direct INTEGER,
    trust TEXT,
    at REAL,
    PRIMARY KEY (src_node, dst_node)
);
CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT,
    source TEXT,
    message TEXT,
    severity TEXT,
    at REAL
);
CREATE TABLE IF NOT EXISTS adapters (
    adapter_id TEXT PRIMARY KEY,
    device_class TEXT,
    authored_by TEXT,
    probe_evidence_hash TEXT,
    gate_run_id TEXT,
    exemplar_id TEXT,
    at REAL
);
"""


class Registry:
    def __init__(self, db_path: Union[str, Path] = ":memory:") -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if self.db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @synchronized
    def upsert_node(
        self,
        profile: NodeProfile,
        capability: Any,
        profile_json: str,
        capability_json: str,
    ) -> None:
        now = time.time()
        cur = self._conn.execute("SELECT registered_at FROM nodes WHERE node_id = ?", (profile.node_id,))
        row = cur.fetchone()
        registered_at = row["registered_at"] if row else now
        self._conn.execute(
            """INSERT INTO nodes (node_id, hostname, os, arch, profile_json, capability_json, registered_at, last_seen)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(node_id) DO UPDATE SET
                 hostname=excluded.hostname, os=excluded.os, arch=excluded.arch,
                 profile_json=excluded.profile_json, capability_json=excluded.capability_json,
                 last_seen=excluded.last_seen""",
            (
                profile.node_id,
                profile.hostname,
                profile.os,
                profile.arch,
                profile_json,
                capability_json,
                registered_at,
                now,
            ),
        )
        for anomaly in profile.anomalies:
            self._conn.execute(
                "INSERT INTO anomalies (node_id, source, message, severity, at) VALUES (?,?,?,?,?)",
                (
                    profile.node_id,
                    anomaly.source,
                    anomaly.message,
                    anomaly.severity,
                    now,
                ),
            )
        self._conn.commit()

    @synchronized
    def heartbeat(self, node_id: str) -> bool:
        cur = self._conn.execute("UPDATE nodes SET last_seen = ? WHERE node_id = ?", (time.time(), node_id))
        self._conn.commit()
        return cur.rowcount > 0

    @synchronized
    def record_bench(self, node_id: str, bench: BenchResult) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO bench_runs
               (run_id, node_id, name, value, unit, trust, variance, sustained_ratio, duration_s, at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                bench.benchmark_run_id,
                node_id,
                bench.name,
                bench.value,
                bench.unit,
                bench.trust.value,
                bench.variance,
                bench.sustained_ratio,
                bench.duration_s,
                time.time(),
            ),
        )
        self._conn.commit()

    @synchronized
    def record_link(self, link: LinkMeasurement) -> None:
        self._conn.execute(
            """INSERT INTO links (src_node, dst_node, rtt_p50_ms, rtt_p95_ms, bandwidth_bps, direct, trust, at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(src_node, dst_node) DO UPDATE SET
                 rtt_p50_ms=excluded.rtt_p50_ms, rtt_p95_ms=excluded.rtt_p95_ms,
                 bandwidth_bps=excluded.bandwidth_bps, direct=excluded.direct,
                 trust=excluded.trust, at=excluded.at""",
            (
                link.src_node,
                link.dst_node,
                link.rtt_p50_ms,
                link.rtt_p95_ms,
                link.bandwidth_bps,
                None if link.direct is None else int(link.direct),
                link.trust.value,
                link.measured_at or time.time(),
            ),
        )
        self._conn.commit()

    @synchronized
    def record_adapter(
        self,
        adapter_id: str,
        device_class: str,
        authored_by: str,
        probe_evidence_hash: str = "",
        gate_run_id: str = "",
        exemplar_id: str = "",
    ) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO adapters
               (adapter_id, device_class, authored_by, probe_evidence_hash, gate_run_id, exemplar_id, at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                adapter_id,
                device_class,
                authored_by,
                probe_evidence_hash,
                gate_run_id,
                exemplar_id,
                time.time(),
            ),
        )
        self._conn.commit()

    @synchronized
    def list_nodes(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT node_id, hostname, os, arch, registered_at, last_seen FROM nodes ORDER BY hostname"
        ).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def node_detail(self, node_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
        if not row:
            return None
        return dict(row)

    @synchronized
    def latest_benches(self, node_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT b.* FROM bench_runs b
               JOIN (SELECT name, MAX(at) AS max_at FROM bench_runs WHERE node_id = ? GROUP BY name) latest
                 ON b.name = latest.name AND b.at = latest.max_at
               WHERE b.node_id = ?""",
            (node_id, node_id),
        ).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def list_links(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM links ORDER BY src_node, dst_node").fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def recent_anomalies(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM anomalies ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def list_adapters(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM adapters ORDER BY at DESC").fetchall()
        return [dict(r) for r in rows]
