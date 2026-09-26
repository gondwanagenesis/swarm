"""Low profile: a swarm should not advertise itself.

Two halves.

**The hub goes dark to strangers.** Without the owner key, a node key, a
valid invite, or a peer-hub signature, every route answers a plain
``404 Not Found`` — no product name, no swarm id, no hint that anything is
there. The server header is generic. (``/api/ping`` and ``/api/echo`` stay:
new devices measure their link before they have any credential; both answer
in generic JSON / raw bytes.)

**Devices open only with a code.** Each swarm has a *local access code*
derived from the owner key (so the owner can always recompute it and nobody
else can). Devices store only a salted PBKDF2 verifier of it. Typing the
agent's ``status`` command and the code on any device shows everything;
the wrong code shows nothing.

Peer hubs (holographic failover) prove they belong to the same swarm with an
HMAC over a timestamp, keyed by the owner-key HASH every legitimate hub of the
swarm holds (a replica carries it).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Any, Dict, Mapping, Optional

ACCESS_ITERATIONS = 200_000
PEER_WINDOW_S = 300.0
_ALPHABET_FIX = str.maketrans({"0": "8", "1": "9", "O": "Q", "I": "J"})


def derive_access_code(owner_key: str) -> str:
    """Eight unambiguous characters, grouped: ``ABCD-EFGH``. Deterministic,
    so the owner (and only the owner) can always get it back."""
    mac = hmac.new(owner_key.encode("utf-8"), b"swarm-local-access-v1", hashlib.sha256).digest()
    code = base64.b32encode(mac).decode("ascii")[:8].translate(_ALPHABET_FIX)
    return f"{code[:4]}-{code[4:]}"


def _norm(code: str) -> str:
    return code.strip().upper().replace(" ", "").replace("-", "").translate(_ALPHABET_FIX)


def make_verifier(code: str, salt: Optional[bytes] = None, iterations: int = ACCESS_ITERATIONS) -> Dict[str, Any]:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", _norm(code).encode("utf-8"), salt, iterations)
    return {"salt": salt.hex(), "hash": digest.hex(), "iter": iterations}


def check_code(code: str, verifier: Optional[Dict[str, Any]]) -> bool:
    if not verifier:
        return True  # an open (lab) swarm has no code
    try:
        salt = bytes.fromhex(str(verifier["salt"]))
        iterations = int(verifier.get("iter") or ACCESS_ITERATIONS)
        want = bytes.fromhex(str(verifier["hash"]))
    except (KeyError, ValueError):
        return False
    got = hashlib.pbkdf2_hmac("sha256", _norm(code).encode("utf-8"), salt, iterations)
    return hmac.compare_digest(got, want)


def peer_header(owner_key_hash: str, now: Optional[float] = None) -> str:
    ts = str(int(now if now is not None else time.time()))
    mac = hmac.new(owner_key_hash.encode("utf-8"), ts.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{ts}.{mac}"


def peer_ok(headers: Mapping[str, Any], owner_key_hash: Optional[str], now: Optional[float] = None) -> bool:
    raw = str(headers.get("X-Swarm-Peer") or "")
    if not owner_key_hash or "." not in raw:
        return False
    ts, _, mac = raw.partition(".")
    try:
        if abs((now if now is not None else time.time()) - int(ts)) > PEER_WINDOW_S:
            return False
    except ValueError:
        return False
    want = hmac.new(owner_key_hash.encode("utf-8"), ts.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, mac)
