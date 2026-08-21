"""Backup: the organism can be cloned from its memory alone.

`snapshot_bytes()` produces a consistent, checksummed copy of the whole sqlite
registry (VACUUM INTO — online, safe under concurrent readers). Nodes fetch it
from /api/backup; operators rotate it offsite. Restoring = putting the bytes too
back and pointing the hub at them. No locks, no torn reads.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict


def snapshot_bytes(conn: sqlite3.Connection) -> bytes:
    """Online, consistent sqlite backup via VACUUM INTO a temp file.
    Works on any sqlite >= 3.27 — no py version dependency."""
    with tempfile.TemporaryDirectory(prefix="swarm-backup-") as tmp:
        target = str(Path(tmp) / "backup.db")
        conn.execute(f"VACUUM INTO '{target}'")
        return Path(target).read_bytes()


def snapshot_with_meta(conn: sqlite3.Connection) -> Dict[str, Any]:
    data = snapshot_bytes(conn)
    return {
        "bytes": data,
        "sha256": hashlib.sha256(data).hexdigest(),
        "at": time.time(),
    }
