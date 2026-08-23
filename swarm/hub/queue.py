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

import contextlib
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
CREATE TABLE IF NOT EXISTS node_device_classes (
    node_id TEXT NOT NULL,
    device_class TEXT NOT NULL,
    at REAL,
    PRIMARY KEY (node_id, device_class)
);
"""

_MIGRATION_V2 = [
    "ALTER TABLE tasks ADD COLUMN lease_started_at REAL",
    "ALTER TABLE tasks ADD COLUMN hedge_count INTEGER DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN hedge_by TEXT",
    "ALTER TABLE tasks ADD COLUMN hedge_expires_at REAL",
    "ALTER TABLE tasks ADD COLUMN hedge_started_at REAL",
    "ALTER TABLE node_stats ADD COLUMN completions INTEGER DEFAULT 0",
    "ALTER TABLE node_stats ADD COLUMN failures INTEGER DEFAULT 0",
    "ALTER TABLE node_stats ADD COLUMN suspended INTEGER DEFAULT 0",
]

_MIGRATION_V3 = [
    "ALTER TABLE bags ADD COLUMN device_class TEXT",
    "ALTER TABLE tasks ADD COLUMN device_class TEXT",
]

_MIGRATIONS = {
    2: _MIGRATION_V2,
    3: _MIGRATION_V3,
}

SCHEMA_VERSION = 3

HEDGE_FRACTION_GATE = 0.75
HEDGE_ELAPSED_MULTIPLE = 1.5
SUSPEND_AFTER_FAILURES = 3

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
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            # Step through every migration the database has not seen yet.
            # ALTERs are idempotent-by-suppression, so a half-applied upgrade
            # (killed mid-flight) heals on the next open.
            for target in range(int(version) + 1, SCHEMA_VERSION + 1):
                for stmt in _MIGRATIONS.get(target, []):
                    with contextlib.suppress(sqlite3.OperationalError):
                        self.conn.execute(stmt)
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    @synchronized
    def submit_bag(
        self,
        op: str,
        params_list: List[Dict[str, Any]],
        idem_keys: List[str],
        device_class: Optional[str] = None,
        task_device_classes: Optional[List[Optional[str]]] = None,
    ) -> str:
        """Submit a bag. `device_class=None` (the default) means any node —
        that is the pre-existing behaviour and stays untouched.

        A bag with a device_class only leases to nodes known to serve that
        class (see `set_node_device_classes` / `pull`). Per-task overrides go
        in `task_device_classes`, positionally aligned with `params_list`."""
        bag_id = new_bag_id()
        now = time.time()
        rows = [
            (
                bag_id,
                seq,
                idem_keys[seq],
                _json(params_list[seq]),
                "queued",
                None,
                None,
                0,
                None,
                _task_class(task_device_classes, seq, device_class),
            )
            for seq in range(len(params_list))
        ]
        with self.conn:
            self.conn.execute(
                "INSERT INTO bags (bag_id, op, total, created_at, device_class) VALUES (?,?,?,?,?)",
                (bag_id, op, len(params_list), now, device_class),
            )
            self.conn.executemany(
                "INSERT INTO tasks (bag_id, seq, idem_key, params_json, status, leased_to, lease_expires_at, attempts, result_key, device_class)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return bag_id

    # ------------------------------------------------------------------
    # node capability index: which device classes a node can actually serve
    # ------------------------------------------------------------------

    @synchronized
    def set_node_device_classes(self, node_id: str, device_classes: List[str]) -> int:
        """Record the device classes a node has PROVEN it serves.

        The hub writes this from measured bindings/verdicts — never from a
        node's own declaration. Replaces the node's whole set."""
        classes = sorted({str(c) for c in device_classes if c})
        now = time.time()
        with self.conn:
            self.conn.execute("DELETE FROM node_device_classes WHERE node_id=?", (node_id,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO node_device_classes (node_id, device_class, at) VALUES (?,?,?)",
                [(node_id, c, now) for c in classes],
            )
        return len(classes)

    @synchronized
    def node_device_classes(self, node_id: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT device_class FROM node_device_classes WHERE node_id=? ORDER BY device_class",
            (node_id,),
        ).fetchall()
        return [r["device_class"] for r in rows]

    @synchronized
    def served_device_classes(self) -> List[str]:
        """Every device class some node in the fleet is known to serve."""
        rows = self.conn.execute(
            "SELECT DISTINCT device_class FROM node_device_classes ORDER BY device_class"
        ).fetchall()
        return [r["device_class"] for r in rows]

    @synchronized
    def sweep_expired(self, now: Optional[float] = None) -> int:
        now = now if now is not None else time.time()
        expired_nodes = self.conn.execute(
            "SELECT DISTINCT leased_to FROM tasks WHERE status='leased' AND lease_expires_at < ? AND leased_to IS NOT NULL",
            (now,),
        ).fetchall()
        cur = self.conn.execute(
            """UPDATE tasks SET status='queued', leased_to=NULL, lease_expires_at=NULL, lease_started_at=NULL, attempts=attempts+1
               WHERE status='leased' AND lease_expires_at < ?""",
            (now,),
        )
        for row in expired_nodes:
            self.note_failure(row["leased_to"])
        self.conn.commit()
        return cur.rowcount

    def _serve_set(
        self, node_id: str, node_device_classes: Optional[Sequence[str]]
    ) -> List[str]:
        """Which device classes this node may be handed work for.

        An explicit argument wins (the caller measured it just now); with no
        argument we fall back to what the hub recorded for this node. A node
        we know nothing about serves nothing device-specific — fail closed,
        never assume a capability."""
        if node_device_classes is not None:
            return sorted({str(c) for c in node_device_classes if c})
        return self.node_device_classes(node_id)

    @synchronized
    def pull(
        self,
        node_id: str,
        n_items: int,
        lease_seconds: float,
        predicted_ms_per_item: float,
        node_device_classes: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Claim up to n_items queued tasks for node_id under a lease.
        Atomically claims rows so no two pullers share a task.

        Tasks whose effective device_class this node cannot serve are simply
        not visible to it. Unrestricted tasks (device_class NULL) are visible
        to everyone, so nodes that predate device routing keep working."""
        self.sweep_expired()
        now = time.time()
        expires = now + max(lease_seconds, 5.0)
        serves = self._serve_set(node_id, node_device_classes)
        where, params = _class_filter(serves)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            rows = self.conn.execute(
                """SELECT t.bag_id AS bag_id, t.seq AS seq FROM tasks t
                   JOIN bags b ON b.bag_id = t.bag_id
                   WHERE t.status='queued' AND """
                + where
                + """
                   ORDER BY t.bag_id, t.seq LIMIT ?""",
                (*params, n_items),
            ).fetchall()
            if rows:
                claim = [(node_id, expires, now, bag, seq) for bag, seq in rows]
                self.conn.executemany(
                    "UPDATE tasks SET status='leased', leased_to=?, lease_expires_at=?, lease_started_at=? WHERE bag_id=? AND seq=? AND status='queued'",
                    claim,
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        if not rows:
            hedges = self.pull_hedges(
                node_id,
                limit=max(1, min(n_items, 2)),
                node_device_classes=node_device_classes,
            )
            return hedges
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
    def pull_hedges(
        self,
        node_id: str,
        limit: int = 2,
        node_device_classes: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Speculative re-execution of stragglers: only once >75% of a bag is
        claimed, only tasks running > 1.5x the bag's median observed item
        duration, at most one hedge per task. The original lease stays alive;
        first finisher wins; the loser's duplicate completion is a no-op."""
        self.sweep_expired()
        now = time.time()
        out: List[Dict[str, Any]] = []
        serves = self._serve_set(node_id, node_device_classes)
        for bag in self.open_bags():
            if len(out) >= limit:
                break
            if not _node_can_serve(bag.get("device_class"), serves):
                continue
            total = bag["total"]
            if total <= 0:
                continue
            claimed = total - (bag["queued"] or 0)
            if claimed / total < HEDGE_FRACTION_GATE:
                continue
            med = self.conn.execute(
                "SELECT duration_s FROM results WHERE bag_id=? ORDER BY duration_s", (bag["bag_id"],)
            ).fetchall()
            if not med:
                continue
            median_s = med[len(med) // 2]["duration_s"]
            if median_s <= 0:
                continue
            threshold_started = now - (median_s * HEDGE_ELAPSED_MULTIPLE)
            task_where, task_params = _class_filter(serves, column="device_class")
            rows = self.conn.execute(
                """SELECT bag_id, seq, idem_key, params_json FROM tasks
                   WHERE bag_id=? AND status='leased' AND hedge_count < 1
                     AND lease_started_at IS NOT NULL AND lease_started_at < ?
                     AND leased_to != ? AND """
                + task_where
                + """
                   LIMIT ?""",
                (bag["bag_id"], threshold_started, node_id, *task_params, limit - len(out)),
            ).fetchall()
            for row in rows:
                lease = max(30.0, median_s * 3.0 + 30.0)
                cur = self.conn.execute(
                    """UPDATE tasks SET hedge_count = hedge_count + 1, hedge_by=?, hedge_started_at=?, hedge_expires_at=?
                       WHERE bag_id=? AND seq=? AND status='leased' AND hedge_count < 1""",
                    (node_id, now, now + lease, row["bag_id"], row["seq"]),
                )
                if cur.rowcount != 1:
                    continue
                out.append(
                    {
                        "bag_id": row["bag_id"],
                        "seq": row["seq"],
                        "idem_key": row["idem_key"],
                        "params": _loads(row["params_json"]),
                        "op": bag["op"],
                        "lease_expires_at": now + lease,
                        "hedge": True,
                    }
                )
        self.conn.commit()
        return out

    @synchronized
    def note_failure(self, node_id: str) -> None:
        """Consecutive-expiry rail: 3 strikes and the node is suspended."""
        self.conn.execute("INSERT OR IGNORE INTO node_stats (node_id) VALUES (?)", (node_id,))
        self.conn.execute(
            "UPDATE node_stats SET failures = failures + 1, suspended = CASE WHEN failures + 1 >= ? THEN 1 ELSE suspended END WHERE node_id=?",
            (SUSPEND_AFTER_FAILURES, node_id),
        )
        self.conn.commit()

    @synchronized
    def is_suspended(self, node_id: str) -> bool:
        row = self.conn.execute("SELECT suspended FROM node_stats WHERE node_id=?", (node_id,)).fetchone()
        return bool(row and row["suspended"])

    @synchronized
    def node_tier(self, node_id: str) -> str:
        """Core / Elastic / Opportunistic / Suspended — earned from track
        record, never declared. Working again resets failures; suspension
        lifts on evidence, not apology."""
        row = self.conn.execute(
            "SELECT samples, failures, suspended, ewma_ms_per_item, ewma_var FROM node_stats WHERE node_id=?",
            (node_id,),
        ).fetchone()
        if row is None:
            return "opportunistic"
        if row["suspended"]:
            return "suspended"
        import math

        cv = (
            math.sqrt(max(row["ewma_var"], 0.0)) / row["ewma_ms_per_item"] if row["ewma_ms_per_item"] else 1.0
        )
        if row["samples"] >= 50 and row["failures"] == 0 and cv < 0.15:
            return "core"
        if row["samples"] >= 10 and row["failures"] <= 1:
            return "elastic"
        return "opportunistic"

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
                "INSERT INTO node_stats (node_id, ewma_ms_per_item, ewma_var, samples, completions) VALUES (?,?,?,1,1)",
                (node_id, ms, 0.0),
            )
            return
        mean = row["ewma_ms_per_item"]
        var = row["ewma_var"]
        samples = row["samples"]
        new_mean = (1 - EWMA_ALPHA) * mean + EWMA_ALPHA * ms
        new_var = (1 - EWMA_ALPHA) * (var + EWMA_ALPHA * (ms - mean) ** 2)
        self.conn.execute(
            "UPDATE node_stats SET ewma_ms_per_item=?, ewma_var=?, samples=?, completions=completions+1, failures=0 WHERE node_id=?",
            (new_mean, new_var, samples + 1, node_id),
        )

    @synchronized
    def node_stats(self, node_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT ewma_ms_per_item, ewma_var, samples, completions, failures, suspended FROM node_stats WHERE node_id=?",
            (node_id,),
        ).fetchone()
        if row:
            out = dict(row)
            out["tier"] = self.node_tier(node_id)
            return out
        return {
            "ewma_ms_per_item": 0.0,
            "ewma_var": 0.0,
            "samples": 0,
            "completions": 0,
            "failures": 0,
            "suspended": 0,
            "tier": "opportunistic",
        }

    @synchronized
    def bag_status(self, bag_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT bag_id, op, total, done, created_at, status, device_class FROM bags WHERE bag_id=?",
            (bag_id,),
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
        out["servable"] = True
        out["blocked_reason"] = None
        # A bag nobody can run must never just sit there looking healthy.
        needed = sorted(
            {
                r["dc"]
                for r in self.conn.execute(
                    "SELECT DISTINCT COALESCE(device_class, ?) AS dc FROM tasks WHERE bag_id=? AND status='queued'",
                    (out.get("device_class"), bag_id),
                ).fetchall()
                if r["dc"]
            }
        )
        if needed:
            served = set(self.served_device_classes())
            missing = [dc for dc in needed if dc not in served]
            if missing:
                out["servable"] = False
                out["blocked_reason"] = "no node serves device_class %s (%d task(s) queued)" % (
                    ", ".join(missing),
                    out["queued"],
                )
        return out

    @synchronized
    def unservable_bags(self) -> List[Dict[str, Any]]:
        """Open bags whose remaining work no known node can run. Fail loud:
        callers surface these instead of letting the bag hang forever."""
        return [bag for bag in self.open_bags() if not bag.get("servable", True)]

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


def _task_class(
    task_device_classes: Optional[List[Optional[str]]], seq: int, bag_class: Optional[str]
) -> Optional[str]:
    """Per-task override, else the bag's class, else unrestricted."""
    if task_device_classes and seq < len(task_device_classes):
        override = task_device_classes[seq]
        if override:
            return str(override)
    return bag_class


def _class_filter(
    serves: Sequence[str], column: str = "COALESCE(t.device_class, b.device_class)"
) -> Tuple[str, List[str]]:
    """SQL fragment: rows this node is allowed to see.

    Unrestricted rows (NULL) are visible to every node. Restricted rows are
    visible only to nodes that serve the class — and a node serving nothing
    sees only unrestricted rows (`IN ()` is not valid SQL, so that case is
    the bare NULL test)."""
    if not serves:
        return ("(%s IS NULL)" % column, [])
    placeholders = ",".join("?" * len(serves))
    return (
        "(%s IS NULL OR %s IN (%s))" % (column, column, placeholders),
        [str(c) for c in serves],
    )


def _node_can_serve(required: Optional[str], serves: Sequence[str]) -> bool:
    return not required or required in set(serves)


def _json(obj: Any) -> str:
    import json

    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _loads(text: str) -> Any:
    import json

    return json.loads(text)
