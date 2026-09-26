"""swarm.hub — registry + work queue + dashboard. Runs on one box you control.

Names are imported lazily (PEP 562) so ``python -m swarm.hub.server`` does
not import the server module twice (runpy warns, and two copies of the
module would hold two copies of its state)."""

from typing import Any

__all__ = ["ChunkPlanner", "Hub", "Registry", "WorkQueue"]

_WHERE = {"ChunkPlanner": ".scheduler", "Hub": ".server", "Registry": ".registry", "WorkQueue": ".queue"}


def __getattr__(name: str) -> Any:
    if name in _WHERE:
        import importlib

        return getattr(importlib.import_module(_WHERE[name], __name__), name)
    raise AttributeError(name)
