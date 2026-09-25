"""Who may talk to the hub, and about what.

Three kinds of caller, three kinds of proof:

- **Owner.** Submits work, reads results, edits the organism, mints invites.
  Proves it with the owner key (``Authorization: Bearer <key>`` — the same
  header every OpenAI client already sends — or ``X-Swarm-Key``, or the
  ``swarm_key`` cookie the dashboard sets after one ``?key=`` visit).
- **Node.** An enrolled machine doing work. Proves it with the per-node key
  the hub issued at registration (``X-Swarm-Node`` + ``X-Swarm-Node-Key``).
  Keys are stored hashed; the plaintext exists once, in the node's state dir.
- **Stranger.** Gets liveness (``/api/ping``), the bandwidth mirror, the
  public agent file, and — with a valid enrollment token — the join scripts
  and the per-invite bundle. Nothing else.

Why this exists: before it, a hub bound to a LAN let anyone who could reach
it submit a bag carrying ``adapter_source`` (arbitrary code, run on every
worker) or propose-and-autopilot a patch to the hub's own source. Consent
that anyone on the Wi-Fi can bypass is not consent.

Loopback-only hubs stay open: nothing off-box can reach them, and the whole
test suite, the demos, and local development run that way. The moment a hub
binds anything else it is *secure* by default. ``--open`` exists for a lab
bench and says so loudly.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})

OWNER_PREFIX = "swo_"
NODE_PREFIX = "swn_"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS node_keys (
    node_id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL,
    created_at REAL,
    revoked INTEGER DEFAULT 0
);
"""


def is_loopback(host: Optional[str]) -> bool:
    return (host or "").strip().lower() in LOOPBACK_HOSTS


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def load_or_create_owner_key(path: Path) -> str:
    """The owner key lives next to the hub database. Created once, 0600
    where the OS honours it. The env var wins so a key can be injected
    without touching disk."""
    env = os.environ.get("SWARM_OWNER_KEY")
    if env:
        return env.strip()
    try:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
    except OSError:
        pass
    key = OWNER_PREFIX + secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    except OSError:
        # Unwritable state dir: the key still guards this process's lifetime.
        pass
    return key


def parse_cookies(header: Optional[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class HubAuth:
    """Owner and node credentials. ``secure=False`` means every check passes
    — the loopback/dev posture — and the class still issues node keys so the
    agent-side flow is identical in both modes."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        lock: Optional[threading.RLock] = None,
        secure: bool = False,
        owner_key: Optional[str] = None,
        owner_key_hash: Optional[str] = None,
    ) -> None:
        self.conn = conn
        self._lock = lock or threading.RLock()
        self.secure = secure
        self.owner_key = owner_key
        # A hub restored from a replica holds only the HASH of the owner key
        # (holo.py): the owner's key keeps working, no node ever learned it.
        self.owner_key_hash = owner_key_hash or (_digest(owner_key) if owner_key else None)
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self.conn.commit()

    # -- owner ------------------------------------------------------------

    def owner_key_from(self, headers: Mapping[str, Any], query: Mapping[str, Any]) -> Optional[str]:
        auth = str(headers.get("Authorization") or "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        if headers.get("X-Swarm-Key"):
            return str(headers.get("X-Swarm-Key")).strip()
        q = query.get("key")
        if isinstance(q, list):
            q = q[0] if q else None
        if q:
            return str(q).strip()
        cookie = parse_cookies(headers.get("Cookie")).get("swarm_key")
        return cookie or None

    def is_owner(self, headers: Mapping[str, Any], query: Mapping[str, Any]) -> bool:
        if not self.secure:
            return True
        presented = self.owner_key_from(headers, query)
        if not presented:
            return False
        if self.owner_key:
            return hmac.compare_digest(presented, self.owner_key)
        if self.owner_key_hash:
            return hmac.compare_digest(_digest(presented), self.owner_key_hash)
        return False

    def node_key_hash(self, node_id: str) -> Optional[str]:
        with self._lock:
            row = self.conn.execute(
                "SELECT key_hash, revoked FROM node_keys WHERE node_id=?", (node_id,)
            ).fetchone()
        return None if row is None or row["revoked"] else str(row["key_hash"])

    # -- nodes ------------------------------------------------------------

    def issue_node_key(self, node_id: str) -> str:
        """Mint (or rotate) this node's key. The plaintext is returned once."""
        key = NODE_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            self.conn.execute(
                "INSERT INTO node_keys (node_id, key_hash, created_at, revoked) VALUES (?,?,?,0)"
                " ON CONFLICT(node_id) DO UPDATE SET key_hash=excluded.key_hash,"
                " created_at=excluded.created_at, revoked=0",
                (node_id, _digest(key), time.time()),
            )
            self.conn.commit()
        return key

    def node_key_valid(self, node_id: str, key: Optional[str]) -> bool:
        if not node_id or not key:
            return False
        with self._lock:
            row = self.conn.execute(
                "SELECT key_hash, revoked FROM node_keys WHERE node_id=?", (node_id,)
            ).fetchone()
        if row is None or row["revoked"]:
            return False
        return hmac.compare_digest(row["key_hash"], _digest(key))

    def is_node(self, headers: Mapping[str, Any], claimed_node_id: Optional[str] = None) -> Optional[str]:
        """Returns the authenticated node id, or None. When the request body
        names a node, it must be the node the key belongs to — a node key
        never speaks for another node."""
        node_id = str(headers.get("X-Swarm-Node") or "")
        key = headers.get("X-Swarm-Node-Key")
        if not self.secure:
            return claimed_node_id or node_id or "local"
        if not self.node_key_valid(node_id, key):
            return None
        if claimed_node_id and claimed_node_id != node_id:
            return None
        return node_id

    def revoke_node(self, node_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute("UPDATE node_keys SET revoked=1 WHERE node_id=?", (node_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def has_key(self, node_id: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT revoked FROM node_keys WHERE node_id=?", (node_id,)
            ).fetchone()
        return row is not None and not row["revoked"]
