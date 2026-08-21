"""Identity: stable node IDs and content-addressed artifact IDs.

Node ID: a persisted random UUID. Generated once, stored in
~/.swarm/node_id, read on every subsequent boot. Survives reboot; does not
follow a cloned disk image to another hostname (we re-seed if the persisted
file was clearly produced under a different hostname).
"""

from __future__ import annotations

import hashlib
import os
import platform
import socket
import uuid
from pathlib import Path
from typing import Any, Optional

from .serde import canonical_json


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def result_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def adapter_id(source: str, origin: str = "ai") -> str:
    prefix = "hw-" if origin == "human" else "ai-"
    return prefix + hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def _state_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".swarm"
    else:
        base = Path.home() / ".swarm"
    base.mkdir(parents=True, exist_ok=True)
    return base


def machine_hostname() -> str:
    try:
        return socket.gethostname() or "unknown"
    except Exception:
        return "unknown"


def get_node_id(state_file: Optional[Path] = None) -> str:
    path = state_file or (_state_dir() / "node_id")
    hostname = machine_hostname()
    try:
        if path.exists():
            stored = path.read_text(encoding="utf-8").strip().splitlines()
            if len(stored) == 2 and stored[1] == hostname and uuid.UUID(stored[0]):
                return stored[0]
    except Exception:
        pass
    node_id = str(uuid.uuid4())
    try:
        path.write_text(node_id + "\n" + hostname + "\n", encoding="utf-8")
    except Exception:
        pass
    return node_id


def machine_hint() -> str:
    return canonical_hash(
        {
            "hostname": machine_hostname(),
            "machine": platform.machine(),
            "node": platform.node(),
            "uuid_node": uuid.getnode(),
        }
    )[:16]
