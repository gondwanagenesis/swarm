"""swarm.agent — the node daemon. Stdlib only; must start on bare Python 3.9."""

from .daemon import Agent

__all__ = ["Agent"]
