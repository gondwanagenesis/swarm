"""Self-update: nodes pull approved bundles, verify, atomically swap, restart.

Channel: hub /api/self-update advertises {sha256,url,size}; /api/bundle/latest
serves the bytes. Update only happens when running as a .pyz bundle; dev
checkouts are left alone. The swap is atomic via os.replace; restart is an
os.execv of the same interpreter on the new file. Verify-first: wrong hash
discards the download.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Optional


def current_sha() -> Optional[str]:
    argv0 = sys.argv[0]
    if not argv0.endswith(".pyz"):
        return None
    try:
        return hashlib.sha256(Path(argv0).read_bytes()).hexdigest()
    except Exception:
        return None


def check_for_update(hub_url: str, timeout: float = 10.0) -> Optional[dict]:
    url = hub_url.rstrip("/") + "/api/self-update"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"}),
            timeout=timeout,
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if not data.get("ok"):
        return None
    return data


def download_and_verify(hub_url: str, expect_sha: str, timeout: float = 60.0) -> Optional[bytes]:
    url = hub_url.rstrip("/") + "/api/bundle/latest"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read()
            header_sha = resp.headers.get("X-Bundle-SHA256")
    except Exception:
        return None
    sha = hashlib.sha256(data).hexdigest()
    if sha != expect_sha or (header_sha and header_sha != expect_sha):
        return None
    return data


def apply_update(hub_url: str) -> str:
    """Returns one of: 'not-bundled', 'current', 'no-offer', 'hash-mismatch',
    'applied-restarting'."""
    argv0 = sys.argv[0]
    me_sha = current_sha()
    if me_sha is None:
        return "not-bundled"
    offer = check_for_update(hub_url)
    if offer is None:
        return "no-offer"
    remote_sha = offer.get("sha256")
    if not remote_sha:
        return "no-offer"
    if remote_sha == me_sha:
        return "current"
    blob = download_and_verify(hub_url, remote_sha)
    if blob is None:
        return "hash-mismatch"
    target = Path(argv0)
    tmp = target.with_suffix(target.suffix + ".next")
    tmp.write_bytes(blob)
    os.replace(str(tmp), str(target))
    os.execv(sys.executable, [sys.executable, str(target), *sys.argv[1:]])
    return "applied-restarting"
