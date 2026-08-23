#!/usr/bin/env python3
"""Fabric demo: the compute path end to end, on whatever silicon is here.

Proves the chain the swarm exists for, and is honest about the links it can't
prove on this machine:

  1. probe            what is actually here (measured, never declared)
  2. capability tower what this agent can reach from here
  3. device classes   what this node therefore serves
  4. routed work      a matmul bag that only a capable node may pull
  5. attribution      which tier ACTUALLY executed it, on which device
  6. unservable       a bag no node can serve is loud, not silently queued

    python scripts/demo_fabric.py [--items 24]
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
from swarm.agent.ops import matmul_tiers_available
from swarm.hub.server import Hub


def _post(port: int, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def _get(port: int, path: str) -> dict:
    return json.loads(
        urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=15).read()
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", type=int, default=24)
    parser.add_argument("--size", type=int, default=96)
    args = parser.parse_args()

    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()
    print(f"[hub]   http://127.0.0.1:{port}")

    agent = Agent(hub_url=f"http://127.0.0.1:{port}", bench=False, ignore_welfare=True)
    agent.run_once()
    node = agent.node_id
    print(f"[agent] registered {node[:8]}")

    # --- what this node measured about itself -------------------------------
    tiers = matmul_tiers_available()
    print(f"[tower] matmul tiers reachable here: {tiers}")

    rows = hub.queue.conn.execute(
        "SELECT device_class FROM node_device_classes WHERE node_id=? ORDER BY device_class",
        (node,),
    ).fetchall()
    classes = [r["device_class"] for r in rows]
    print(f"[class] this node serves: {classes}")

    # --- routed work --------------------------------------------------------
    target = "gpu:generic" if "gpu:generic" in classes else "cpu:generic"
    print(f"[route] submitting matmul bag classed {target}")
    bag = _post(
        port,
        "/api/bag/submit",
        {
            "op": "matmul",
            "device_class": target,
            "params_list": [
                {"m": args.size, "k": args.size, "n": args.size, "seed": i}
                for i in range(args.items)
            ],
        },
    )["bag_id"]

    agent.start_worker(poll_seconds=0.15)
    deadline = time.time() + 180.0
    status = None
    while time.time() < deadline:
        status = _get(port, f"/api/bag/{bag}")
        if status.get("done", 0) >= args.items:
            break
        time.sleep(0.5)
    agent.stop_worker()

    done = (status or {}).get("done", 0)
    print(f"[work]  {done}/{args.items} complete")

    # --- attribution: what actually did the work ----------------------------
    tiers_seen: Counter = Counter()
    devices: Counter = Counter()
    for row in hub.queue.conn.execute(
        "SELECT r.payload_json FROM results r JOIN tasks t ON t.result_key = r.result_key"
        " WHERE t.bag_id = ?",
        (bag,),
    ):
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except ValueError:
            continue
        if isinstance(payload, dict):
            tiers_seen[payload.get("tier")] += 1
            devices[payload.get("device")] += 1
    print(f"[attrib] executed by tier: {dict(tiers_seen)}")
    print(f"[attrib] on device:        {dict(devices)}")
    if "torch_cuda" not in tiers_seen:
        print("[attrib] no GPU tier ran here - reported honestly, not claimed")

    # --- the honest failure mode -------------------------------------------
    blocked = _post(
        port,
        "/api/bag/submit",
        {
            "op": "matmul",
            "device_class": "tpu:generic",
            "params_list": [{"m": 4, "k": 4, "n": 4, "seed": 1}],
        },
    )["bag_id"]
    bstat = hub.queue.bag_status(blocked)
    print(
        f"[closed] tpu-classed bag servable={bstat.get('servable')} "
        f"reason={bstat.get('blocked_reason')!r}"
    )

    hub.stop()
    ok = done >= args.items and bstat.get("servable") is False
    print("\n[result] fabric loop " + ("OK" if ok else "INCOMPLETE"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
