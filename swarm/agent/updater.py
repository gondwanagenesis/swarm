"""Self-update: nodes pull approved bundles, verify, atomically swap, restart.

Channel: hub /api/self-update (a node-authenticated call) advertises
{sha256, code_hash, url, size}; /api/bundle/latest serves the bytes. Update
only happens when running as a .pyz bundle; dev checkouts are left alone.

"Is there an update?" compares CODE hashes (``swarm_build.json``), not file
hashes: a per-invite bundle carries its own ``swarm_config.json`` (hub URL,
token) and so never matches the generic bundle byte-for-byte. Comparing file
hashes made every joined node "update" to the generic bundle and lose the
config that told it where its hub was.

Verify-first: the downloaded bytes must match the advertised sha256. Then the
node's own config is carried into the new archive, the swap is atomic
(os.replace), and the agent restarts on the new file (os.execv).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, Optional


def current_sha() -> Optional[str]:
    argv0 = sys.argv[0]
    if not argv0.endswith(".pyz"):
        return None
    try:
        return hashlib.sha256(Path(argv0).read_bytes()).hexdigest()
    except Exception:
        return None


def own_code_hash() -> Optional[str]:
    argv0 = sys.argv[0]
    if not argv0.endswith(".pyz"):
        return None
    try:
        with zipfile.ZipFile(argv0) as zf:
            return str(json.loads(zf.read("swarm_build.json").decode("utf-8"))["code_hash"])
    except Exception:
        return None


def _own_config() -> Optional[bytes]:
    try:
        with zipfile.ZipFile(sys.argv[0]) as zf:
            if "swarm_config.json" in zf.namelist():
                return zf.read("swarm_config.json")
    except Exception:
        pass
    return None


def with_config(blob: bytes, config: Optional[bytes]) -> bytes:
    """Rewrite a bundle so it carries `config` as swarm_config.json."""
    if not config:
        return blob
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(blob)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            if item.filename != "swarm_config.json":
                dst.writestr(item, src.read(item.filename))
        dst.writestr("swarm_config.json", config)
    return out.getvalue()


def signature_ok(offer: dict, headers: Optional[Dict[str, str]]) -> bool:
    """A hub that knows this node's key signs the bundle hash with it (HMAC
    over sha256(node key)). An impostor that does not know the key cannot
    make this node install anything. Open (keyless) hubs send no signature."""
    key = (headers or {}).get("X-Swarm-Node-Key")
    sig = offer.get("sig")
    if not key:
        return True
    if not sig:
        return False
    import hmac

    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    want = hmac.new(key_hash.encode("utf-8"), str(offer.get("sha256")).encode("utf-8"), "sha256").hexdigest()
    return hmac.compare_digest(want, str(sig))


def check_for_update(
    hub_url: str, timeout: float = 10.0, headers: Optional[Dict[str, str]] = None, node_id: str = ""
) -> Optional[dict]:
    url = hub_url.rstrip("/") + "/api/self-update"
    try:
        body = json.dumps({"node_id": node_id}).encode("utf-8")
        req_headers = {"Content-Type": "application/json"}
        req_headers.update(headers or {})
        with urllib.request.urlopen(
            urllib.request.Request(url, data=body, headers=req_headers),
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


def apply_update(hub_url: str, headers: Optional[Dict[str, str]] = None, node_id: str = "") -> str:
    """Returns one of: 'not-bundled', 'current', 'no-offer', 'hash-mismatch',
    'applied-restarting'."""
    argv0 = sys.argv[0]
    me_sha = current_sha()
    if me_sha is None:
        return "not-bundled"
    offer = check_for_update(hub_url, headers=headers, node_id=node_id)
    if offer is None:
        return "no-offer"
    remote_sha = offer.get("sha256")
    if not remote_sha:
        return "no-offer"
    if not signature_ok(offer, headers):
        return "bad-signature"
    remote_code = offer.get("code_hash")
    mine = own_code_hash()
    if remote_code and mine and remote_code == mine:
        return "current"
    if not remote_code and remote_sha == me_sha:
        return "current"
    blob = download_and_verify(hub_url, remote_sha)
    if blob is None:
        return "hash-mismatch"
    blob = with_config(blob, _own_config())
    target = Path(argv0)
    tmp = target.with_suffix(target.suffix + ".next")
    tmp.write_bytes(blob)
    os.replace(str(tmp), str(target))
    os.execv(sys.executable, [sys.executable, str(target), *sys.argv[1:]])
    return "applied-restarting"
