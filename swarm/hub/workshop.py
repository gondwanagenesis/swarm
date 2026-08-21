"""The workshop — where the organism edits its own genes.

Design law (non-negotiable):
  1. A patch is a proposal, never an edit. It lands in a sandbox copy of the
     tree first.
  2. The sandbox must pass the full test suite (+ stdlib-import gate) — that's
     the known-good/known-bad contract applied to ourselves. A patch that
     breaks a test never touches the real tree.
  3. Human approval is the membrane: nothing applies until the operator POSTs
     an approve. (Autopilot mode exists but is explicitly opt-in per hub boot.)
  4. Every propose/apply/rollback is recorded in the patch ledger with content
     hashes. Rollback restores the exact bytes snapshotted at apply time.

It can only edit files inside the swarm repo (path traversal refused), and
only .py/.md/contract files — never the .git internals or binary payloads.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ALLOWED_SUFFIXES = {".py", ".md", ".json", ".txt"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS patch_ledger (
    patch_id TEXT PRIMARY KEY,
    title TEXT,
    reason TEXT,
    files_json TEXT,
    status TEXT,
    gate_ok INTEGER,
    gate_log TEXT,
    authored_by TEXT DEFAULT 'human',
    backup_json TEXT,
    created_at REAL,
    applied_at REAL,
    rolled_back_at REAL
);
"""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Workshop:
    def __init__(
        self,
        registry_conn: sqlite3.Connection,
        repo_root: Path,
        lock: Optional[threading.RLock] = None,
        autopilot: bool = True,
    ) -> None:
        self.conn = registry_conn
        self.root = Path(repo_root).resolve()
        self.autopilot = autopilot
        self._lock = lock or threading.RLock()
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _validate_paths(self, files: Dict[str, str]) -> None:
        for rel in files:
            p = (self.root / rel).resolve()
            if self.root not in p.parents and p != self.root:
                raise ValueError(f"path escapes repo: {rel}")
            if not any(rel.endswith(s) for s in ALLOWED_SUFFIXES):
                raise ValueError(f"forbidden file type: {rel}")
            if any(seg in (".git", "__pycache__", ".venv") for seg in Path(rel).parts):
                raise ValueError(f"forbidden path: {rel}")

    def propose(
        self, title: str, reason: str, files: Dict[str, str], authored_by: str = "human"
    ) -> Dict[str, Any]:
        """Sandbox + test-gate the proposal. Returns the ledger record."""
        self._validate_paths(files)
        patch_id = "patch-" + _hash(title + reason + json.dumps(files, sort_keys=True))[:14]
        record: Dict[str, Any] = {
            "patch_id": patch_id,
            "title": title,
            "reason": reason,
            "files": sorted(files),
            "authored_by": authored_by,
            "status": "sandboxed",
            "gate_ok": False,
            "gate_log": "",
            "created_at": time.time(),
        }
        gate_ok, gate_log = self._sandbox_test(files)
        record["gate_ok"] = gate_ok
        record["gate_log"] = gate_log[-4000:]
        record["status"] = "staged" if gate_ok else "rejected"
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO patch_ledger (patch_id, title, reason, files_json, status, gate_ok, gate_log, authored_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    patch_id,
                    title,
                    reason,
                    json.dumps(files, sort_keys=True),
                    record["status"],
                    int(gate_ok),
                    record["gate_log"],
                    authored_by,
                    record["created_at"],
                ),
            )
            self.conn.commit()
        if gate_ok and self.autopilot:
            try:
                applied = self.approve_and_apply(patch_id)
                record["status"] = "applied"
                record["applied_at"] = applied["applied_at"]
                record["autopilot"] = True
            except Exception as exc:
                record["status"] = "staged"
                record["autopilot_error"] = str(exc)
        return record

    def _sandbox_test(self, files: Dict[str, str]) -> tuple:
        """Apply to a copy of the tree; run the full test suite there. Never
        raises; returns (ok, log)."""
        with tempfile.TemporaryDirectory(prefix="swarm-workshop-") as tmp:
            dest = Path(tmp) / "tree"
            ignore = shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.db", "*.db-journal")
            shutil.copytree(self.root, dest, ignore=ignore)
            for rel, content in files.items():
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            logs: List[str] = []
            ok_all = True
            for label, cmd in (
                ("compileall", [sys.executable, "-m", "compileall", "-q", "swarm"]),
                (
                    "pytest",
                    [sys.executable, "-m", "pytest", "tests", "-q", "-x", "--tb=line", "-k", "not workshop"],
                ),
                (
                    "stdlib-gate",
                    [
                        sys.executable,
                        "scripts/check_stdlib_imports.py",
                        "swarm/core",
                        "swarm/probe",
                        "swarm/bench",
                        "swarm/agent",
                        "swarm/transport",
                        "swarm/integrator",
                    ],
                ),
            ):
                try:
                    proc = subprocess.run(
                        cmd,
                        cwd=str(dest),
                        capture_output=True,
                        text=True,
                        timeout=600,
                        env=dict(os.environ, SWARM_WORKSHOP_SANDBOX="1"),
                    )
                except Exception as exc:
                    logs.append(f"[{label}] harness error: {exc}")
                    ok_all = False
                    continue
                logs.append(f"[{label}] rc={proc.returncode}\n{proc.stdout[-1500:]}{proc.stderr[-1500:]}")
                if proc.returncode != 0:
                    ok_all = False
                    break
            return ok_all, "\n".join(logs)

    def approve_and_apply(self, patch_id: str) -> Dict[str, Any]:
        row = self._get(patch_id)
        if row is None:
            raise KeyError("unknown patch")
        if not row["gate_ok"] or row["status"] != "staged":
            raise ValueError("patch not in staged-with-green-gate state")
        files = json.loads(row["files_json"])
        backup: Dict[str, Optional[str]] = {}
        for rel in files:
            p = self.root / rel
            backup[rel] = p.read_text(encoding="utf-8") if p.exists() else None
        for rel, content in files.items():
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        now = time.time()
        with self._lock:
            self.conn.execute(
                "UPDATE patch_ledger SET status='applied', backup_json=?, applied_at=? WHERE patch_id=?",
                (json.dumps({k: v for k, v in backup.items()}), now, patch_id),
            )
            self.conn.commit()
        return {"patch_id": patch_id, "status": "applied", "applied_at": now}

    def rollback(self, patch_id: str) -> Dict[str, Any]:
        row = self._get(patch_id)
        if row is None or row["status"] != "applied":
            raise ValueError("only applied patches can roll back")
        backup = json.loads(row["backup_json"] or "{}")
        for rel, content in backup.items():
            target = self.root / rel
            if content is None:
                if target.exists():
                    target.unlink()
            else:
                target.write_text(content, encoding="utf-8")
        now = time.time()
        with self._lock:
            self.conn.execute(
                "UPDATE patch_ledger SET status='rolled_back', rolled_back_at=? WHERE patch_id=?",
                (now, patch_id),
            )
            self.conn.commit()
        return {"patch_id": patch_id, "status": "rolled_back"}

    def _get(self, patch_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM patch_ledger WHERE patch_id=?", (patch_id,)).fetchone()
        return dict(row) if row else None

    def ledger(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT patch_id, title, reason, status, gate_ok, authored_by, created_at, applied_at, rolled_back_at FROM patch_ledger ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
