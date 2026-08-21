"""swarm.probe — hardware + self introspection. Stdlib only.

Never raises, never hangs, always reports what it could not find."""

from .orchestrator import ProbeContext, full_probe
from .self_probe import climb_tower

__all__ = ["ProbeContext", "climb_tower", "full_probe"]
