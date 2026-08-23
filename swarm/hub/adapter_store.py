"""Content-addressed blob store for adapter source code.

A promoted adapter is worthless as a bare hash: the swarm has to be able to
load and execute the code that earned the gate pass. This module is where the
bytes actually live. Keys are the sha256 content hash produced by
`swarm.core.identity.result_hash` — the same hasher the rest of the system
uses for content identity, so a hash computed anywhere addresses the blob here.

Layout follows the hub state-dir convention (`~/.swarm/`, resolved through
`swarm.core.identity._state_dir`). Blobs are sharded by the first two hex
characters to keep directory listings sane:

    <root>/ab/abcdef...python.py

Writes are atomic (temp file + os.replace) and idempotent: storing the same
source twice is a no-op that returns the same hash. Reads never raise — an
unknown, malformed or unreadable hash returns None, in keeping with the
"None is better than a plausible-looking lie" contract.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from ..core.identity import _state_dir, result_hash

_HASH_LEN = 64
_HEX = frozenset("0123456789abcdef")


def default_store_root() -> Path:
    """`~/.swarm/adapters` — same state dir the hub db defaults into."""
    return _state_dir() / "adapters"


def _is_valid_hash(content_hash: str) -> bool:
    if not isinstance(content_hash, str) or len(content_hash) != _HASH_LEN:
        return False
    return all(ch in _HEX for ch in content_hash)


@dataclass
class AdapterStore:
    """Immutable content-addressed store of adapter source text."""

    root: Path = field(default_factory=default_store_root)

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    # -- paths -----------------------------------------------------------

    def path_for(self, content_hash: str) -> Optional[Path]:
        """Filesystem path a hash maps to, or None if the hash is malformed."""
        if not _is_valid_hash(content_hash):
            return None
        return self.root / content_hash[:2] / (content_hash + ".py")

    # -- write -----------------------------------------------------------

    def put(self, source: str) -> str:
        """Store `source`, return its content hash. Idempotent."""
        payload = source.encode("utf-8")
        content_hash = result_hash(payload)
        path = self.path_for(content_hash)
        if path is None:  # pragma: no cover - result_hash always returns hex
            return content_hash
        if path.exists():
            return content_hash
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(tmp_name, str(path))
        except Exception:
            with contextlib.suppress(Exception):
                os.unlink(tmp_name)
            raise
        return content_hash

    # -- read ------------------------------------------------------------

    def get(self, content_hash: str) -> Optional[str]:
        """Return the stored source, or None. Never raises."""
        path = self.path_for(content_hash)
        if path is None:
            return None
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return None

    def exists(self, content_hash: str) -> bool:
        path = self.path_for(content_hash)
        if path is None:
            return False
        try:
            return path.is_file()
        except Exception:
            return False


def store_for(db_path: Union[str, Path]) -> AdapterStore:
    """Pick the blob root that belongs beside a registry db.

    A file-backed registry keeps its blobs next to the db (so the hub default
    `~/.swarm/hub.db` yields `~/.swarm/adapters`). An in-memory registry has no
    directory of its own, so it falls back to the shared state dir.
    """
    text = str(db_path)
    if text == ":memory:" or not text:
        return AdapterStore()
    parent = Path(text).expanduser().resolve().parent
    return AdapterStore(root=parent / "adapters")
