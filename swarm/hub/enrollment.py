"""Enrollment tokens: one line of consent, signed, revocable.

A token is the owner's signature on a node joining. The hub can run in two
modes: open (any agent may register — lab mode) or token-required
(fleet mode). Tokens are random, URL-safe, expiring, single-or-multi use by
role. Offered invites route through /invite/<token>, which serves the
one-click page; the click is the consent.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS enroll_tokens (
    token TEXT PRIMARY KEY,
    role TEXT DEFAULT 'node',
    label TEXT,
    created_at REAL,
    expires_at REAL,
    used INTEGER DEFAULT 0,
    revoked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS spore_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed_node_id TEXT,
    channel TEXT,
    peer_hint TEXT,
    at REAL
);
"""


class Enrollment:
    def __init__(self, conn: sqlite3.Connection, lock: Optional[threading.RLock] = None) -> None:
        self.conn = conn
        self._lock = lock or threading.RLock()
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def create(
        self, role: str = "node", label: Optional[str] = None, ttl_s: float = 86400.0
    ) -> Dict[str, Any]:
        token = "swk_" + secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self.conn.execute(
                "INSERT INTO enroll_tokens (token, role, label, created_at, expires_at) VALUES (?,?,?,?,?)",
                (token, role, label, now, now + ttl_s),
            )
            self.conn.commit()
        return {"token": token, "role": role, "expires_at": now + ttl_s}

    def validate(self, token: str, consume: bool = False) -> Optional[Dict[str, Any]]:
        with self._lock:
            if consume:
                self.conn.execute(
                    "UPDATE enroll_tokens SET used=used+1 WHERE token=? AND revoked=0 AND expires_at > ?",
                    (token, time.time()),
                )
                self.conn.commit()
            row = self.conn.execute("SELECT * FROM enroll_tokens WHERE token=?", (token,)).fetchone()
            if row is None or row["revoked"] or row["expires_at"] < time.time():
                return None
            return dict(row)

    def revoke(self, token: str) -> bool:
        with self._lock:
            cur = self.conn.execute("UPDATE enroll_tokens SET revoked=1 WHERE token=?", (token,))
            self.conn.commit()
            return cur.rowcount > 0

    def log_spore_event(self, seed_node_id: str, channel: str, peer_hint: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO spore_events (seed_node_id, channel, peer_hint, at) VALUES (?,?,?,?)",
                (seed_node_id, channel, peer_hint, time.time()),
            )
            self.conn.commit()

    def list_tokens(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM enroll_tokens ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def spore_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM spore_events ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
