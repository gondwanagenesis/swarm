"""swarm.agent — the node daemon. Stdlib only; must start on bare Python 3.9.

``Agent`` is imported lazily (PEP 562) so ``python -m swarm.agent.daemon``
does not import the daemon module twice."""

from typing import Any

__all__ = ["Agent"]


def __getattr__(name: str) -> Any:
    if name == "Agent":
        from .daemon import Agent

        return Agent
    raise AttributeError(name)
