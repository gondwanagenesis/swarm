#!/usr/bin/env python3
"""M2 demo: three workers pull a primesum bag off one hub; one is killed
mid-run; the sweep requeues its work; the bag still completes exactly once.

    python scripts/demo_m2.py [--items 400]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swarm.agent.daemon import Agent
from swarm.hub.server import Hub


def submit(port: int, n: int) -> str:
    payload = {"op": "primesum", "params_list": [{"n": 3000 + i * 37} for i in range(n)]}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/bag/submit",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read())["bag_id"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", type=int, default=400)
    args = parser.parse_args()

    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()
    print(f"[hub] http://127.0.0.1:{port}")

    agents = [
        Agent(hub_url=f"http://127.0.0.1:{port}", bench=False, node_id=f"w{i}") for i in range(3)
    ]
    threads = []
    for i, agent in enumerate(agents):
        agent.run_once()
        threads.append(agent.start_worker(poll_seconds=0.15))
        print(f"[agent {i}] {agent.node_id[:8]} registered and working")

    bag_id = submit(port, args.items)
    print(f"[bag] {bag_id}: {args.items} primesum items")

    time.sleep(2.0)
    print(f"[kill] stopping agent 2 ({agents[2].node_id[:8]}) mid-run")
    agents[2].stop_worker()

    deadline = time.time() + 120.0
    final = None
    while time.time() < deadline:
        status = hub.queue.bag_status(bag_id)
        if status and status["status"] == "closed":
            final = status
            break
        print(f"[watch] done={status['done']} queued={status['queued']} leased={status['leased']}")
        time.sleep(1.0)
    for agent in agents:
        agent.stop_worker()
    for thread in threads:
        thread.join(timeout=5)

    if not final:
        print("[result] FAILED: bag did not close in 120s")
        hub.stop()
        return 1

    results = hub.queue.results_for_bag(bag_id)
    by_node = Counter(r["node_id"] for r in results)
    idem = {r["idem_key"] for r in results}
    print(f"[result] bag closed: {final['done']}/{final['total']} done, {len(idem)} unique results")
    print(f"[result] distribution (honest, measured share): {dict(by_node)}")
    assert len(idem) == args.items
    hub.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
