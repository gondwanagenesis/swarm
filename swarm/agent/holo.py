"""The node's half of the holographic hub: remember, replicate, re-attach.

Every heartbeat reply carries the hub's ``swarm_id``, ``epoch`` and ranked
``successors`` (``hub/holo.py``). The agent keeps that in
``~/.swarm/holo-<node>.json`` so it survives restarts. If this node is a
successor, it also keeps the hub's latest replica on disk.

When the hub stops answering for ``SWARM_FAILOVER_S`` seconds (default 180):

1. Ask each successor, in rank order, whether it is already serving this
   swarm (``/api/hubinfo``, same swarm_id, epoch >= ours). First yes wins.
2. If none is, and THIS node is the best-ranked successor still alive, it
   promotes itself: restores the replica as a hub database at ``epoch + 1``
   and starts a hub process from its own agent file — the bundle carries the
   whole swarm, hub included. Each rank waits ``RANK_GRACE_S`` longer than the
   one above it, and re-checks the higher ranks first, so two successors
   rarely race; if they do, the lower-rank hub steps aside on its next peer
   check (tie-break by rank).
3. The agent switches to the chosen hub; its node key still works there (the
   replica carries every node key's hash).

A node that is not a successor simply follows. Stdlib only.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

RANK_GRACE_S = float(os.environ.get("SWARM_RANK_GRACE_S", "20"))
REPLICA_REFRESH_S = float(os.environ.get("SWARM_REPLICA_REFRESH_S", "60"))


def failover_after_s() -> float:
    try:
        return float(os.environ.get("SWARM_FAILOVER_S", "180"))
    except ValueError:
        return 180.0


def _state_dir() -> Path:
    base = Path(os.environ.get("USERPROFILE") or str(Path.home())) if os.name == "nt" else Path.home()
    return base / ".swarm"


class HoloState:
    """What this node knows about the swarm's hubs, persisted."""

    def __init__(self, node_id: str, state_dir: Optional[Path] = None) -> None:
        self.dir = state_dir or _state_dir()
        self.node_id = node_id
        self.path = self.dir / f"holo-{node_id[:12]}.json"
        self.data: Dict[str, Any] = {}
        with contextlib.suppress(Exception):
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> None:
        with contextlib.suppress(OSError):
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)

    @property
    def epoch(self) -> int:
        return int(self.data.get("epoch") or 0)

    @property
    def successors(self) -> List[Dict[str, Any]]:
        return list(self.data.get("successors") or [])

    def update(self, info: Dict[str, Any], hub_url: str) -> None:
        if not info:
            return
        epoch = int(info.get("epoch") or 0)
        if epoch < self.epoch and info.get("swarm_id") == self.data.get("swarm_id"):
            return  # an older hub's news is not news
        self.data["swarm_id"] = info.get("swarm_id") or self.data.get("swarm_id")
        self.data["epoch"] = epoch
        if info.get("successors") is not None:
            self.data["successors"] = info["successors"]
        self.data["hub_url"] = hub_url
        self.data["replica_sha256_offered"] = info.get("replica_sha256")
        self.save()

    def my_rank(self) -> Optional[int]:
        for s in self.successors:
            if s.get("node_id") == self.node_id:
                return int(s.get("rank", 0))
        return None

    @property
    def replica_path(self) -> Path:
        return self.dir / "replica" / f"{self.data.get('swarm_id') or 'swarm'}.db.gz"


def fetch_replica(state: HoloState, hub_url: str, headers: Dict[str, str]) -> bool:
    """Download the hub's replica if it changed. Atomic on disk."""
    if state.my_rank() is None:
        return False
    if time.time() - float(state.data.get("replica_fetched_at") or 0) < REPLICA_REFRESH_S:
        return False
    offered = state.data.get("replica_sha256_offered")
    if offered and offered == state.data.get("replica_sha256") and state.replica_path.exists():
        return False  # nothing new at the hub
    try:
        req = urllib.request.Request(hub_url.rstrip("/") + "/api/replica", headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            sha = resp.headers.get("X-Replica-SHA256")
            swarm_id = resp.headers.get("X-Swarm-Id")
    except Exception:
        return False
    import hashlib

    if not data or (sha and hashlib.sha256(data).hexdigest() != sha):
        return False
    if swarm_id:
        state.data["swarm_id"] = swarm_id
    target = state.replica_path
    with contextlib.suppress(OSError):
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        state.data["replica_sha256"] = sha
        state.data["replica_fetched_at"] = time.time()
        state.save()
        return True
    return False


def hub_info(url: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/hubinfo", timeout=timeout) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        return info if info.get("ok") else None
    except Exception:
        return None


def serving(url: str, swarm_id: Optional[str], min_epoch: int) -> Optional[Dict[str, Any]]:
    info = hub_info(url)
    if info is None or info.get("demoted"):
        return None
    if swarm_id and info.get("swarm_id") != swarm_id:
        return None
    if int(info.get("epoch") or 0) < min_epoch:
        return None
    return info


def promote(state: HoloState, entry: Dict[str, Any]) -> Optional[subprocess.Popen]:
    """Become the hub: restore the replica at epoch+1, start a hub process
    from this node's own agent file, bound where the old hub said we would
    serve. Returns the process, or None when there is nothing to restore."""
    replica = state.replica_path
    if not replica.exists():
        return None
    from ..hub.holo import restore_replica

    new_epoch = state.epoch + 1
    db = state.dir / "hub" / "hub.db"
    restore_replica(replica.read_bytes(), db, new_epoch)
    with contextlib.suppress(Exception):
        import sqlite3

        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT OR REPLACE INTO hub_settings (key, value) VALUES ('promoted_rank', ?)",
            (str(entry.get("rank", 0)),),
        )
        conn.commit()
        conn.close()
    state.data["epoch"] = new_epoch
    state.data["hub_url"] = entry["url"]
    state.data["promoted_at"] = time.time()
    state.save()
    return launch_hub(state, entry, new_epoch)


def launch_hub(state: HoloState, entry: Dict[str, Any], epoch: int) -> Optional[subprocess.Popen]:
    """Start (or restart) the hub process on this node's hub database.
    Detached: the hub belongs to the swarm and outlives this agent."""
    db = state.dir / "hub" / "hub.db"
    if not db.exists():
        return None
    parsed = urlparse(entry["url"])
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8777
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0.endswith(".pyz"):
        cmd = [sys.executable, argv0, "--run-hub"]
    else:
        cmd = [sys.executable, "-m", "swarm.hub.server"]
    cmd += ["--host", host, "--port", str(port), "--db", str(db), "--epoch", str(epoch)]
    if os.environ.get("SWARM_HUB_SECURE") == "1":
        cmd.append("--secure")
    log = open(state.dir / "hub-promoted.log", "ab")  # noqa: SIM115 - handed to the child
    kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    finally:
        log.close()
    deadline = time.time() + 30
    while time.time() < deadline:
        if hub_info(entry["url"], timeout=1.0):
            return proc
        if proc.poll() is not None:
            return None
        time.sleep(0.5)
    return proc


def choose_hub(state: HoloState, dead_for_s: float) -> Dict[str, Any]:
    """Decide what to do after the hub has been silent `dead_for_s` seconds.

    Returns {"action": "wait"} | {"action": "switch", "url"} |
    {"action": "promote", "entry"}."""
    swarm_id = state.data.get("swarm_id")
    successors = sorted(state.successors, key=lambda s: int(s.get("rank", 0)))
    for s in successors:
        if s.get("node_id") == state.node_id:
            continue
        if serving(s["url"], swarm_id, state.epoch):
            return {"action": "switch", "url": s["url"]}
    rank = state.my_rank()
    if rank is None:
        return {"action": "wait"}
    if dead_for_s < failover_after_s() + rank * RANK_GRACE_S:
        return {"action": "wait"}
    # every better-ranked successor stayed silent past its grace: our turn
    me = next(s for s in successors if s.get("node_id") == state.node_id)
    return {"action": "promote", "entry": me}
