"""Shared reentrant-lock decorator for the hub's sqlite access.

One connection serves HTTP handler threads, the queue sweeper, and demo
scripts. Serializing method bodies through a shared RLock (held by both
Registry and WorkQueue) is simple and correct; WAL + busy_timeout already
cover cross-process cases.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def synchronized(method: F) -> F:
    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]
