#!/usr/bin/env python3
"""Build swarm-agent.pyz (single-file node agent). See swarm.hub.agentbundle."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swarm.hub.agentbundle import main

if __name__ == "__main__":
    raise SystemExit(main())
