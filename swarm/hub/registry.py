"""Hub registry: the swarm's record of what was measured.

SQLite via stdlib. Profiles are stored as canonical JSON (serde.dumps) so the
stored bytes are exactly what the node sent. The adapter registry schema —
content-hash identity plus the full provenance chain — exists now even though
the integrator (M4) doesn't; tables are cheap, rewrites are not.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..core.models import BenchResult, LinkMeasurement, NodeProfile
from ._sync import synchronized
from .adapter_store import AdapterStore, store_for

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
    source_hash TEXT,
    at REAL
);
CREATE TABLE IF NOT EXISTS runtime_bindings (
    node_id TEXT,
    device_class TEXT,
    runtime TEXT,
    confidence REAL,
    evidence_json TEXT,
    at REAL,
    PRIMARY KEY (node_id, device_class, runtime)
);
CREATE TABLE IF NOT EXISTS device_verdicts (
    node_id TEXT,
    device_class TEXT,
    verdict TEXT,
    reason TEXT,
    at REAL,
    PRIMARY KEY (node_id, device_class)
);
CREATE TABLE IF NOT EXISTS gate_runs (
    gate_run_id TEXT PRIMARY KEY,
    adapter_id TEXT,
    device_class TEXT,
    passed INTEGER,
    detail_json TEXT,
    at REAL
);
"""

# Additive column migrations, applied to databases created before the column
# existed. `PRAGMA user_version` on this connection belongs to WorkQueue (it
# shares the Registry connection), so presence is detected per-column via
# PRAGMA table_info instead of a version counter.
_COLUMN_MIGRATIONS = (
    ("adapters", "source_hash", "TEXT"),
)


class Registry:
    def __init__(
        self,
        db_path: Union[str, Path] = ":memory:",
        store: Optional[AdapterStore] = None,
    ) -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if self.db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()
        self.store = store if store is not None else store_for(self.db_path)

    def _migrate(self) -> None:
        """Add columns missing from databases created by an older schema."""
        for table, column, coltype in _COLUMN_MIGRATIONS:
            rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            if not rows:
                continue
            present = {r["name"] for r in rows}
            if column in present:
                continue
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

    def close(self) -> None:
        # Under the shared lock: closing while a handler thread is mid-query
        # is an access violation in sqlite, not a Python exception. Callers
        # arriving after this get a clean ProgrammingError instead.
        with self._lock:
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
        source: str = "",
    ) -> None:
        """Record an adapter. When `source` is given the code itself is kept.

        A proven adapter that nothing can load is not proof of anything, so the
        source bytes go into the content-addressed store and the row carries the
        hash that addresses them.
        """
        source_hash = self.store.put(source) if source else None
        self._conn.execute(
            """INSERT OR IGNORE INTO adapters
               (adapter_id, device_class, authored_by, probe_evidence_hash, gate_run_id,
                exemplar_id, source_hash, at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                adapter_id,
                device_class,
                authored_by,
                probe_evidence_hash,
                gate_run_id,
                exemplar_id,
                source_hash,
                time.time(),
            ),
        )
        if source_hash:
            # INSERT OR IGNORE silently drops a re-record of a known adapter;
            # backfill the source for rows registered before the code arrived.
            self._conn.execute(
                "UPDATE adapters SET source_hash=? WHERE adapter_id=? AND "
                "(source_hash IS NULL OR source_hash='')",
                (source_hash, adapter_id),
            )
        self._conn.commit()

    @synchronized
    def get_adapter_source(self, adapter_id: str) -> Optional[str]:
        """Source code of a recorded adapter, or None if none was stored."""
        row = self._conn.execute(
            "SELECT source_hash FROM adapters WHERE adapter_id = ?", (adapter_id,)
        ).fetchone()
        if not row:
            return None
        source_hash = row["source_hash"]
        if not source_hash:
            return None
        return self.store.get(source_hash)

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

    @synchronized
    def record_binding(
        self,
        node_id: str,
        device_class: str,
        runtime: Optional[str],
        confidence: float,
        evidence: Dict[str, Any],
    ) -> None:
        self._conn.execute(
            """INSERT INTO runtime_bindings (node_id, device_class, runtime, confidence, evidence_json, at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(node_id, device_class, runtime) DO UPDATE SET
                 confidence=excluded.confidence, evidence_json=excluded.evidence_json, at=excluded.at""",
            (node_id, device_class, runtime, confidence, json.dumps(evidence, sort_keys=True), time.time()),
        )
        self._conn.commit()

    @synchronized
    def record_verdict(self, node_id: str, device_class: str, verdict: str, reason: str) -> None:
        self._conn.execute(
            """INSERT INTO device_verdicts (node_id, device_class, verdict, reason, at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(node_id, device_class) DO UPDATE SET
                 verdict=excluded.verdict, reason=excluded.reason, at=excluded.at""",
            (node_id, device_class, verdict, reason, time.time()),
        )
        self._conn.commit()

    @synchronized
    def list_verdicts(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM device_verdicts ORDER BY at DESC").fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def record_gate_run(self, record: Dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO gate_runs (gate_run_id, adapter_id, device_class, passed, detail_json, at) VALUES (?,?,?,?,?,?)",
            (
                record.get("gate_run_id"),
                record.get("adapter_id"),
                record.get("device_class"),
                int(bool(record.get("passed"))),
                json.dumps(record, sort_keys=True),
                record.get("at", time.time()),
            ),
        )
        self._conn.commit()

    @synchronized
    def list_gate_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT gate_run_id, adapter_id, device_class, passed, at FROM gate_runs ORDER BY at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def promote_adapter(self, adapter_id: str, gate_run_id: str) -> None:
        self._conn.execute(
            "UPDATE adapters SET gate_run_id=? WHERE adapter_id=?",
            (gate_run_id, adapter_id),
        )
        self._conn.commit()

    @synchronized
    def list_bindings(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM runtime_bindings ORDER BY at DESC").fetchall()
        return [dict(r) for r in rows]
