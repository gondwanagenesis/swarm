"""Agent operation registry: pure functions (params -> payload).

Ops must be pure with respect to their params — that is what makes results
idempotent and content-addressable, and what makes lease requeue safe.
Adding an op is the ONLY way to extend what the swarm computes.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Callable, Dict


def op_primesum(params: Dict[str, Any]) -> Any:
    """Count primes below n by trial division to sqrt. CPU-bound, pure."""
    n = int(params.get("n", 5000))
    if n < 2:
        return {"n": n, "count": 0}
    count = 1
    for candidate in range(3, n, 2):
        limit = int(math.sqrt(candidate))
        composite = False
        d = 3
        while d <= limit:
            if candidate % d == 0:
                composite = True
                break
            d += 2
        if not composite:
            count += 1
    return {"n": n, "count": count}


def op_hashwork(params: Dict[str, Any]) -> Any:
    """Chain sha256 seed-> iterations; a pure compute grind."""
    seed = str(params.get("seed", "swarm"))
    rounds = int(params.get("rounds", 20000))
    digest = seed.encode("utf-8")
    for _ in range(rounds):
        digest = hashlib.sha256(digest).digest()
    return {"seed": seed, "rounds": rounds, "digest": digest.hex()}


OPS: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "primesum": op_primesum,
    "hashwork": op_hashwork,
}
