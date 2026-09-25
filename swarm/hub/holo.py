"""Holographic hub: any node can become the hub.

The hub used to be the one machine whose loss ended the swarm. This organ
removes that: the hub continuously hands its memory to a few chosen nodes,
tells every node who those successors are, and steps aside if a newer hub
takes over.

The contract
------------
- **swarm_id** — a random id minted once and carried in every replica, so a
  restored hub is recognisably the *same* swarm (node keys, tokens, owner key
  hash all still valid).
- **epoch** — an integer that only grows. A successor that promotes itself
  starts at ``epoch + 1``. Everyone follows the highest epoch they can reach;
  ties break toward the lower successor rank. An old hub that comes back and
  meets a higher epoch demotes itself and redirects callers (HTTP 409
  ``{"moved_to", "epoch"}``) instead of splitting the brain.
- **successors** — up to ``SUCCESSOR_COUNT`` online nodes, ranked: dedicated
  machines first, then longest-standing, then most free RAM. Published in
  every heartbeat reply with the URL each would serve on.
- **replica** — a consistent sqlite snapshot (VACUUM INTO), pruned of old
  results, gzip-compressed, content-hashed. Successors fetch it when the hash
  changes. It holds the owner key's HASH only, never the key: the owner's key
  keeps working on any successor, and no node ever learns it.

Honest limits: a replica is a snapshot, so work submitted after the last one
a successor fetched is not in it (results already written survive; queued
work re-submits). Replication is pull-based over the same authenticated
channel as everything else.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import secrets
import sqlite3
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

SUCCESSOR_COUNT = 3
REPLICA_RESULT_TTL_S = 3 * 86400.0
PEER_CHECK_S = 60.0
DEFAULT_HUB_PORT = 8777


class Settings:
    """Tiny key/value table the hub owns (and replicates)."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self.conn = conn
        self._lock = lock
        with self._lock:
            self.conn.execute("CREATE TABLE IF NOT EXISTS hub_settings (key TEXT PRIMARY KEY, value TEXT)")
            self.conn.commit()

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self.conn.execute("SELECT value FROM hub_settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row and row[0] is not None else default

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO hub_settings (key, value) VALUES (?, ?)", (key, str(value))
            )
            self.conn.commit()


def _is_loopback(host: Optional[str]) -> bool:
    return str(host or "").startswith("127.") or str(host or "") in ("localhost", "::1", "")


class Holo:
    def __init__(self, hub: Any) -> None:
        self.hub = hub
        self.settings = Settings(hub.registry._conn, hub.registry._lock)
        if not self.settings.get("swarm_id"):
            self.settings.set("swarm_id", "swm_" + secrets.token_hex(8))
        if not self.settings.get("epoch"):
            self.settings.set("epoch", 1)
        self.demoted: Optional[Dict[str, Any]] = None
        self._replica_cache: Optional[Dict[str, Any]] = None
        self._replica_lock = threading.Lock()
        self._peer_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- identity -------------------------------------------------------------

    @property
    def swarm_id(self) -> str:
        return str(self.settings.get("swarm_id"))

    @property
    def epoch(self) -> int:
        return int(self.settings.get("epoch") or 1)

    def set_epoch(self, epoch: int) -> None:
        self.settings.set("epoch", int(epoch))

    # -- successors -----------------------------------------------------------

    def successors(self) -> List[Dict[str, Any]]:
        """Ranked successors with the URL each would serve a hub on."""
        now = time.time()
        rows = self.hub.inference._runtime_rows() if hasattr(self.hub, "inference") else []
        by_id = {r["node_id"]: r for r in rows}
        cands = []
        for n in self.hub.registry.list_nodes():
            from .inference import ONLINE_WINDOW_S

            if not n.get("last_seen") or now - n["last_seen"] > ONLINE_WINDOW_S:
                continue
            extra = self.node_extra(n["node_id"])
            if not extra.get("can_hub"):
                continue
            rt = by_id.get(n["node_id"]) or {}
            host = rt.get("reach_host")
            addrs = [a for a in rt.get("addresses") or [] if not _is_loopback(a)]
            if _is_loopback(host) and addrs:
                host = addrs[0]
            if not host:
                continue
            cands.append(
                (
                    1 if extra.get("dedicated") else 0,
                    -(n.get("registered_at") or now),  # older = more proven
                    rt.get("ram_free_bytes") or 0,
                    n["node_id"],
                    host,
                    int(extra.get("hub_port") or DEFAULT_HUB_PORT),
                    n.get("hostname"),
                )
            )
        cands.sort(reverse=True)
        out = []
        for rank, (_, _, _, node_id, host, port, hostname) in enumerate(cands[:SUCCESSOR_COUNT]):
            shown = f"[{host}]" if ":" in str(host) else host
            out.append({"rank": rank, "node_id": node_id, "hostname": hostname, "url": f"http://{shown}:{port}"})
        return out

    def node_extra(self, node_id: str) -> Dict[str, Any]:
        raw = self.settings.get(f"node_extra:{node_id}")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def record_node_extra(self, node_id: str, extra: Dict[str, Any]) -> None:
        self.settings.set(f"node_extra:{node_id}", json.dumps(extra, sort_keys=True))

    def info(self) -> Dict[str, Any]:
        replica = self._replica_cache or {}
        return {
            "swarm_id": self.swarm_id,
            "epoch": self.epoch,
            "rank": self.rank,
            "successors": self.successors(),
            "replica_sha256": replica.get("sha256"),
            "demoted": self.demoted,
        }

    @property
    def rank(self) -> int:
        """-1 for an original hub; a promoted successor's rank otherwise.
        Equal epochs break toward the lower rank."""
        return int(self.settings.get("promoted_rank") or -1)

    # -- replica --------------------------------------------------------------

    def replica(self, max_age_s: float = 30.0) -> Dict[str, Any]:
        """A consistent, pruned, gzipped snapshot. Rebuilt at most every
        `max_age_s` so many successors polling cost one snapshot."""
        with self._replica_lock:
            cached = self._replica_cache
            if cached and time.time() - cached["at"] < max_age_s:
                return cached
            with tempfile.TemporaryDirectory(prefix="swarm-replica-") as tmp:
                target = str(Path(tmp) / "replica.db")
                with self.hub.registry._lock:
                    self.hub.registry._conn.execute(f"VACUUM INTO '{target}'")
                db = sqlite3.connect(target)
                try:
                    cutoff = time.time() - REPLICA_RESULT_TTL_S
                    db.execute(
                        "DELETE FROM results WHERE at < ? AND result_key NOT IN "
                        "(SELECT result_key FROM tasks t JOIN bags b ON b.bag_id=t.bag_id WHERE b.status='open' AND t.result_key IS NOT NULL)",
                        (cutoff,),
                    )
                    db.commit()
                    db.execute("VACUUM")
                finally:
                    db.close()
                raw = Path(target).read_bytes()
            data = gzip.compress(raw, compresslevel=6)
            self._replica_cache = {
                "bytes": data,
                "sha256": hashlib.sha256(data).hexdigest(),
                "raw_bytes": len(raw),
                "at": time.time(),
                "epoch": self.epoch,
            }
            return self._replica_cache

    # -- stepping aside -------------------------------------------------------

    def check_peers_once(self) -> Optional[Dict[str, Any]]:
        """Ask each published successor whether it is running a NEWER hub of
        this swarm. If so, demote: this hub stops handing out work and
        redirects every caller."""
        for s in self.successors() + json.loads(self.settings.get("last_successors") or "[]"):
            try:
                with urllib.request.urlopen(s["url"] + "/api/hubinfo", timeout=3.0) as resp:
                    info = json.loads(resp.read().decode("utf-8"))
            except Exception:
                continue
            peer_epoch = int(info.get("epoch") or 0)
            newer = peer_epoch > self.epoch or (
                peer_epoch == self.epoch and int(info.get("rank", -1)) < self.rank and not info.get("demoted")
            )
            if info.get("swarm_id") == self.swarm_id and newer:
                self.demoted = {"moved_to": s["url"], "epoch": int(info["epoch"])}
                return self.demoted
        current = self.successors()
        if current:
            self.settings.set("last_successors", json.dumps(current))
        return None

    def start_peer_watch(self, interval: Optional[float] = None) -> None:
        """Look for a newer (or better-ranked, same-epoch) hub of this swarm.
        Early checks are fast — right after a failover two successors can race,
        and the loser must step aside in seconds, not a minute later."""
        if self._peer_thread is not None:
            return
        import os

        steady = float(interval if interval is not None else os.environ.get("SWARM_PEER_CHECK_S", PEER_CHECK_S))
        schedule = [3.0, 7.0, 20.0]

        def loop() -> None:
            while True:
                wait = schedule.pop(0) if schedule else steady
                if self._stop.wait(wait):
                    return
                try:
                    if self.demoted is None:
                        self.check_peers_once()
                except Exception:
                    pass

        self._peer_thread = threading.Thread(target=loop, daemon=True, name="swarm-hub-peers")
        self._peer_thread.start()

    def stop(self) -> None:
        self._stop.set()


def restore_replica(gz_bytes: bytes, target_db: Path, new_epoch: int) -> None:
    """Materialise a replica as a hub database with a bumped epoch."""
    target_db.parent.mkdir(parents=True, exist_ok=True)
    tmp = target_db.with_suffix(".restoring")
    tmp.write_bytes(gzip.decompress(gz_bytes))
    db = sqlite3.connect(str(tmp))
    try:
        db.execute("CREATE TABLE IF NOT EXISTS hub_settings (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT OR REPLACE INTO hub_settings (key, value) VALUES ('epoch', ?)", (str(int(new_epoch)),))
        db.execute("INSERT OR REPLACE INTO hub_settings (key, value) VALUES ('promoted_at', ?)", (str(time.time()),))
        db.commit()
    finally:
        db.close()
    for suffix in ("-wal", "-shm"):
        side = Path(str(target_db) + suffix)
        if side.exists():
            side.unlink()
    tmp.replace(target_db)
