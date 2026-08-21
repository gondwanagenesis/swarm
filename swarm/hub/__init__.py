"""swarm.hub — registry + dashboard. Runs on one box you control."""

from .registry import Registry
from .server import Hub

__all__ = ["Hub", "Registry"]
