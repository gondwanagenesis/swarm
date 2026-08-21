"""Bag-of-tasks work queue. Pull-based: workers ask, the hub never pushes.

Leases, not assignments: a leased task requeues automatically when its lease
expires — no failure detector, no reclaiming machinery. Results are
content-addressed, so the double-completion a premature requeue causes is a
no-op write, not a correctness event.

Concurrency: claims run inside BEGIN IMMEDIATE with UPDATE ... RETURNING so
exactly one pull wins a row. WAL mode + busy_timeout keep readers (dashboard)
from fighting writers (workers).
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from ._sync import synchronized

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bags (
    bag_id TEXT PRIMARY KEY,
    op TEXT,
    total INTEGER,
    done INTEGER DEFAULT 0,
    created_at REAL,
    status TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS tasks (
    bag_id TEXT,
    seq INTEGER,
    idem_key TEXT,
    params_json TEXT,
    status TEXT DEFAULT 'queued',
    leased_to TEXT,
    lease_expires_at REAL,
    attempts INTEGER DEFAULT 0,
    result_key TEXT,
    PRIMARY KEY (bag_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_tasks_queued ON tasks(bag_id, status, seq);
CREATE INDEX IF NOT EXISTS idx_tasks_lease ON tasks(lease_expires_at) WHERE status = 'leased';
CREATE TABLE IF NOT EXISTS results (
    result_key TEXT PRIMARY KEY,
    idem_key TEXT,
    bag_id TEXT,
    payload_json TEXT,
    node_id TEXT,
    duration_s REAL,
    at REAL
);
CREATE TABLE IF NOT EXISTS node_stats (
    node_id TEXT PRIMARY KEY,
    ewma_ms_per_item REAL DEFAULT 0,
    ewma_var REAL DEFAULT 0,
    samples INTEGER DEFAULT 0
);
"""

DEFAULT_MIN_CHUNK = 2
DEFAULT_MAX_CHUNK = 512
BASE_CHUNK = 8
NEW_NODE_CONFIDENCE_FLOOR = 0.2
EWMA_ALPHA = 0.3


def new_bag_id() -> str:
    return "bag-" + uuid.uuid4().hex[:16]


def new_result_key(payload_json: str) -> str:
    from ..core.identity import result_hash

    return result_hash(payload_json.encode("utf-8"))


class WorkQueue:
    def __init__(self, conn: sqlite3.Connection, lock: Optional[threading.RLock] = None) -> None:
        self.conn = conn
        self._lock: threading.RLock = lock or threading.RLock()
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    @synchronized
    def submit_bag(self, op: str, params_list: List[Dict[str, Any]], idem_keys: List[str]) -> str:
        bag_id = new_bag_id()
        now = time.time()
        rows = [
            (bag_id, seq, idem_keys[seq], _json(params_list[seq]), "queued", None, None, 0, None)
            for seq in range(len(params_list))
        ]
        with self.conn:
            self.conn.execute(
                "INSERT INTO bags (bag_id, op, total, created_at) VALUES (?,?,?,?)",
                (bag_id, op, len(params_list), now),
            )
            self.conn.executemany(
                "INSERT INTO tasks (bag_id, seq, idem_key, params_json, status, leased_to, lease_expires_at, attempts, result_key)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return bag_id

    @synchronized
    def sweep_expired(self, now: Optional[float] = None) -> int:
        now = now if now is not None else time.time()
        cur = self.conn.execute(
            """UPDATE tasks SET status='queued', leased_to=NULL, lease_expires_at=NULL, attempts=attempts+1
               WHERE status='leased' AND lease_expires_at < ?""",
            (now,),
        )
        self.conn.commit()
        return cur.rowcount

    @synchronized
    def pull(
        self,
        node_id: str,
        n_items: int,
        lease_seconds: float,
        predicted_ms_per_item: float,
    ) -> List[Dict[str, Any]]:
        """Claim up to n_items queued tasks for node_id under a lease.
        Atomically claims rows so no two pullers share a task."""
        self.sweep_expired()
        now = time.time()
        expires = now + max(lease_seconds, 5.0)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            rows = self.conn.execute(
                """SELECT bag_id, seq FROM tasks WHERE status='queued'
                   ORDER BY bag_id, seq LIMIT ?""",
                (n_items,),
            ).fetchall()
            if rows:
                claim = [(node_id, expires, bag, seq) for bag, seq in rows]
                self.conn.executemany(
                    "UPDATE tasks SET status='leased', leased_to=?, lease_expires_at=? WHERE bag_id=? AND seq=? AND status='queued'",
                    claim,
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        if not rows:
            return []
        out: List[Dict[str, Any]] = []
        for bag_id, seq in rows:
            row = self.conn.execute(
                "SELECT t.bag_id, t.seq, t.idem_key, t.params_json, b.op FROM tasks t JOIN bags b ON b.bag_id = t.bag_id WHERE t.bag_id=? AND t.seq=?",
                (bag_id, seq),
            ).fetchone()
            if row:
                out.append(
                    {
                        "bag_id": row["bag_id"],
                        "seq": row["seq"],
                        "idem_key": row["idem_key"],
                        "params": _loads(row["params_json"]),
                        "op": row["op"],
                        "lease_expires_at": expires,
                    }
                )
        return out

    @synchronized
    def complete(
        self, node_id: str, results: List[Dict[str, Any]], now: Optional[float] = None
    ) -> Dict[str, int]:
        now = now if now is not None else time.time()
        accepted = dupes = dropped = 0
        with self.conn:
            for item in results:
                bag_id = item["bag_id"]
                seq = item["seq"]
                idem_key = item["idem_key"]
                payload_json = _json(item.get("payload"))
                duration_s = float(item.get("duration_s") or 0.0)
                rkey = new_result_key(payload_json)
                task = self.conn.execute(
                    "SELECT status, leased_to, idem_key FROM tasks WHERE bag_id=? AND seq=?",
                    (bag_id, seq),
                ).fetchone()
                if task is None or task["idem_key"] != idem_key:
                    dropped += 1
                    continue
                self.conn.execute(
                    "INSERT OR IGNORE INTO results (result_key, idem_key, bag_id, payload_json, node_id, duration_s, at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (rkey, idem_key, bag_id, payload_json, node_id, duration_s, now),
                )
                if task["status"] == "done":
                    dupes += 1
                    continue
                self.conn.execute(
                    "UPDATE tasks SET status='done', result_key=? WHERE bag_id=? AND seq=?",
                    (rkey, bag_id, seq),
                )
                self.conn.execute(
                    "UPDATE bags SET done = done + 1, status = CASE WHEN done + 1 >= total THEN 'closed' ELSE status END WHERE bag_id=?",
                    (bag_id,),
                )
                accepted += 1
                self._update_node_stats(node_id, duration_s)
        return {"accepted": accepted, "duplicates": dupes, "dropped": dropped}

    @synchronized
    def renew(self, node_id: str, bag_id: str, seqs: List[int], lease_seconds: float) -> int:
        """Extend leases on tasks this node already holds (⅓-life renewal)."""
        expires = time.time() + max(lease_seconds, 5.0)
        cur = self.conn.execute(
            "UPDATE tasks SET lease_expires_at=? WHERE bag_id=? AND status='leased' AND leased_to=? AND seq IN (%s)"
            % ",".join("?" * len(seqs)),
            [expires, bag_id, node_id, *seqs],
        )
        self.conn.commit()
        return cur.rowcount

    def _update_node_stats(self, node_id: str, duration_s: float) -> None:
        ms = duration_s * 1000.0
        row = self.conn.execute(
            "SELECT ewma_ms_per_item, ewma_var, samples FROM node_stats WHERE node_id=?", (node_id,)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO node_stats (node_id, ewma_ms_per_item, ewma_var, samples) VALUES (?,?,?,1)",
                (node_id, ms, 0.0),
            )
            return
        mean = row["ewma_ms_per_item"]
        var = row["ewma_var"]
        samples = row["samples"]
        new_mean = (1 - EWMA_ALPHA) * mean + EWMA_ALPHA * ms
        new_var = (1 - EWMA_ALPHA) * (var + EWMA_ALPHA * (ms - mean) ** 2)
        self.conn.execute(
            "UPDATE node_stats SET ewma_ms_per_item=?, ewma_var=?, samples=? WHERE node_id=?",
            (new_mean, new_var, samples + 1, node_id),
        )

    @synchronized
    def node_stats(self, node_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT ewma_ms_per_item, ewma_var, samples FROM node_stats WHERE node_id=?", (node_id,)
        ).fetchone()
        return dict(row) if row else {"ewma_ms_per_item": 0.0, "ewma_var": 0.0, "samples": 0}

    @synchronized
    def bag_status(self, bag_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT bag_id, op, total, done, created_at, status FROM bags WHERE bag_id=?", (bag_id,)
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        remaining = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE bag_id=? AND status='queued'", (bag_id,)
        ).fetchone()
        leased = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE bag_id=? AND status='leased'", (bag_id,)
        ).fetchone()
        out["queued"] = remaining["n"]
        out["leased"] = leased["n"]
        return out

    @synchronized
    def open_bags(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT bag_id FROM bags WHERE status='open' ORDER BY created_at").fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            status = self.bag_status(r["bag_id"])
            if status is not None:
                out.append(status)
        return out

    @synchronized
    def results_for_bag(self, bag_id: str) -> List[Dict[str, Any]]:
        """Per-task completion view. Storage is content-addressed (duplicate
        payloads stored once), but every completed task maps to its result."""
        rows = self.conn.execute(
            """SELECT t.idem_key AS idem_key, r.payload_json AS payload_json,
                      r.node_id AS node_id, t.seq AS seq, r.duration_s AS duration_s,
                      r.result_key AS result_key
               FROM tasks t JOIN results r ON r.result_key = t.result_key
               WHERE t.bag_id=? AND t.status='done' ORDER BY t.seq""",
            (bag_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _json(obj: Any) -> str:
    import json

    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _loads(text: str) -> Any:
    import json

    return json.loads(text)
