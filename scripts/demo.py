#!/usr/bin/env python3
"""One-command demo: spin up a hub on an ephemeral port, run one agent
(tower + probe + floor benchmarks + link measurement), print what was
measured, and leave the dashboard URL alive for a few seconds.

    python scripts/demo.py [--hold 20]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swarm.agent.daemon import Agent
from swarm.hub.server import Hub


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hold", type=float, default=20.0, help="seconds to keep dashboard up"
    )
    args = parser.parse_args()

    hub = Hub(port=0)
    host, port = hub.start_background()
    print(f"[hub]   listening on http://{host}:{port}")

    agent = Agent(hub_url=f"http://{host}:{port}", bench=True)
    t0 = time.time()
    ok = agent.run_once()
    dt = time.time() - t0
    if not ok:
        print("[agent] registration FAILED")
        hub.stop()
        return 1

    nodes = hub.registry.list_nodes()
    detail = hub.registry.node_detail(agent.node_id)
    benches = hub.registry.latest_benches(agent.node_id)
    links = hub.registry.list_links()

    node = nodes[0]
    print(
        f"[agent] registered as {agent.node_id[:8]} ({node['hostname']}, {node['os']}/{node['arch']}) in {dt:.1f}s"
    )

    import json

    cap = json.loads(detail["capability_json"]) if detail else {}
    print(
        f"[tower] reached floor F{cap.get('max_floor')} | tools: {sorted((cap.get('tools') or {}).keys()) or 'none'} "
        f"| packages: {sorted((cap.get('packages') or {}).keys()) or 'none'}"
    )
    print("[bench]")
    for b in benches:
        val = "   n/a   " if b["value"] is None else f"{b['value']:8.3f}"
        print(
            f"        {val} {b['unit']:<7} trust={b['trust']:<10} run={b['run_id'][:12]}"
        )
    print("[link]")
    for l in links:
        rtt = "  n/a " if l["rtt_p50_ms"] is None else f"{l['rtt_p50_ms']:6.2f}"
        bw = (
            "n/a"
            if l["bandwidth_bps"] is None
            else f"{l['bandwidth_bps'] / 1e9:.2f} Gb/s"
        )
        print(f"        RTT p50: {rtt} ms | bandwidth: {bw}")
    anomalies = hub.registry.recent_anomalies(10)
    if anomalies:
        print(f"[anomalies] {len(anomalies)} recent (honest gaps, not hides):")
        for a in anomalies[:10]:
            print(f"        [{a['severity']}] {a['source']}: {a['message'][:100]}")

    print(f"\ndashboard: http://{host}:{port}/  (holding {args.hold}s)")
    time.sleep(max(0.0, args.hold))
    hub.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
