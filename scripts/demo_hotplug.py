#!/usr/bin/env python3
"""Hot-plug nerve demo: a device appears while the swarm runs.

Starts a hub and an agent. Mid-run, a synthetic device is injected into the
agent's device inventory (simulating a freshly-detected eGPU/NPU), the
watcher fires, the node re-registers, and the hub's coverage report picks the
new class up as uncovered — queued for the integrator.

    python scripts/demo_hotplug.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swarm.agent.daemon import Agent
from swarm.core.models import DeviceInfo
from swarm.probe import hotplug
from swarm.probe.hotplug import HotplugWatcher


def main() -> int:
    from swarm.hub.server import Hub

    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()
    print(f"[hub] http://127.0.0.1:{port}")

    agent = Agent(hub_url=f"http://127.0.0.1:{port}", bench=False, node_id="nerve-node")
    agent.run_once()
    print(f"[agent] {agent.node_id} registered")

    def fake_collect():
        base = [
            DeviceInfo(
                kind="gpu",
                name="Onboard Graphics",
                vendor="Intel",
                pci_address="0000:00:02.0",
                unified_memory=True,
            ),
        ]
        if time.time() > t_plug_in:
            base.append(
                DeviceInfo(
                    kind="gpu",
                    name="Freshly Plugged eGPU-9000",
                    vendor="AMD",
                    pci_address="0000:01:00.0",
                    vram_bytes=16 * 1024**3,
                )
            )
        return base, []

    original = hotplug.collect_devices
    hotplug.collect_devices = fake_collect
    fired: list = []
    watcher = HotplugWatcher(interval=0.5)
    watcher.on_change(lambda added, removed: fired.extend(added))
    print("[watch] waiting 2s, then plugging a synthetic eGPU...")
    t_plug_in = time.time() + 2.0
    watcher.start()
    deadline = time.time() + 15.0
    while not fired and time.time() < deadline:
        time.sleep(0.2)
    watcher.stop()
    hotplug.collect_devices = original

    if not fired:
        print("[result] NO EVENT — watcher failed")
        hub.stop()
        return 1

    print(f"[watch] hotplug fired: {[d.name for d in fired]}")

    agent._on_devices_changed(list(fired), [])
    time.sleep(0.5)

    cov = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/coverage", timeout=5).read())
    print(f"[coverage] uncovered now: {[d['device_class'] for d in cov['uncovered']]}")
    hub.stop()
    print("[result] nerve fired -> node re-registered with a REAL probe (synthetic tissue is not stored; only measured hardware enters the registry). The uncovered classes above are this machine's real devices awaiting adapters.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
