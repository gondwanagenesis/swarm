"""Bounded subprocess execution. The probe's contract: never raise, never hang.

Every external command goes through run_bounded. Failures return None and are
recorded as anomalies by callers. Timeouts are mandatory and short.
"""

from __future__ import annotations

import subprocess
from typing import List, Optional


def run_bounded(
    cmd: List[str], timeout: float = 15.0, max_output: int = 2_000_000
) -> Optional[str]:
    """Run cmd with a hard timeout. Returns stdout text or None on any failure.
    Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    except Exception:
        return None
    if proc.stdout is None:
        return None
    try:
        out = proc.stdout[:max_output].decode("utf-8", errors="replace")
    except Exception:
        return None
    return out if out.strip() else None
