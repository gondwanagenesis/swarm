"""The node agent. What it does, in order:

  1. climb the capability tower (what can THIS agent do here?)
  2. full hardware probe (never raises, never hangs)
  3. fallback benchmarks at the floor the tower reached
  4. measure the link to the hub (RTT, bandwidth)
  5. register all of it with the hub, then heartbeat

Everything is stdlib. Everything degrades: hub unreachable -> log and keep
breathing; benchmark crash -> anomaly, not exit.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ..bench.fallback import run_floor_benchmarks
from ..core.models import BenchResult
from ..core.serde import to_dict
from ..probe.orchestrator import full_probe
from ..transport.link import LinkProber
from .ops import OPS

HEARTBEAT_SECONDS = 30.0
IDLE_BACKOFF_MAX = 10.0
LEASE_RENEW_FRACTION = 1.0 / 3.0


class Agent:
    def __init__(
        self,
        hub_url: str,
        bench: bool = True,
        rebench_interval: float = 86400.0,
        node_id: Optional[str] = None,
    ) -> None:
        parsed = urlparse(hub_url if "://" in hub_url else "http://" + hub_url)
        self.hub_host = parsed.hostname or "127.0.0.1"
        self.hub_port = parsed.port or 8777
        self.do_bench = bench
        self.rebench_interval = rebench_interval
        self.node_id_override = node_id
        self.registered = False
        self.node_id = ""
        self.last_bench_at = 0.0
        self._stop = threading.Event()

    def _post(self, path: str, payload: Dict[str, Any], timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        try:
            body = json.dumps(payload).encode("utf-8")
            conn = http.client.HTTPConnection(self.hub_host, self.hub_port, timeout=timeout)
            conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            if resp.status != 200:
                return None
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def probe_and_register(self) -> bool:
        profile, capability, ctx = full_probe(node_id=self.node_id_override)
        self.node_id = profile.node_id
        link = LinkProber(self.hub_host, self.hub_port).probe(profile.node_id)

        benches: List[BenchResult] = []
        if self.do_bench:
            benches = run_floor_benchmarks()
            self.last_bench_at = time.time()

        payload = {
            "profile": to_dict(profile),
            "capability": to_dict(capability),
            "benchmarks": [to_dict(b) for b in benches],
        }
        resp = self._post("/api/register", payload)
        if resp and resp.get("ok"):
            self.registered = True
            self._post("/api/link", to_dict(link))
        return self.registered

    def heartbeat_forever(self) -> None:
        while True:
            resp = self._post("/api/heartbeat", {"node_id": self.node_id})
            if resp is None or not resp.get("ok"):
                self.registered = False
                self.probe_and_register()
            elif self.do_bench and (time.time() - self.last_bench_at) > self.rebench_interval:
                run_floor_benchmarks()
                self.last_bench_at = time.time()
                self._post("/api/heartbeat", {"node_id": self.node_id})
            time.sleep(HEARTBEAT_SECONDS)

    def run_once(self) -> bool:
        return self.probe_and_register()

    def execute_chunk(self, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Run one leased chunk. Each item is timed for the hub's EWMA;
        a failing op marks that item failed but never kills the chunk."""
        results: List[Dict[str, Any]] = []
        for task in tasks:
            op = OPS.get(task.get("op") or "")
            started = time.perf_counter()
            payload: Any = None
            ok = False
            if op is not None:
                try:
                    payload = op(task.get("params") or {})
                    ok = True
                except Exception as exc:
                    payload = {"error": str(exc)[:200]}
            results.append(
                {
                    "bag_id": task["bag_id"],
                    "seq": task["seq"],
                    "idem_key": task["idem_key"],
                    "payload": payload,
                    "duration_s": time.perf_counter() - started,
                    "ok": ok,
                }
            )
        return results

    def maybe_renew(self, tasks: List[Dict[str, Any]], started_at: float) -> None:
        if not tasks:
            return
        expires = min(float(t.get("lease_expires_at") or 0.0) for t in tasks)
        total_lease = expires - started_at
        if total_lease <= 0:
            return
        if time.time() - started_at >= total_lease * LEASE_RENEW_FRACTION:
            bag_id = tasks[0]["bag_id"]
            seqs = [int(t["seq"]) for t in tasks if t["bag_id"] == bag_id]
            self._post(
                "/api/tasks/renew",
                {
                    "node_id": self.node_id,
                    "bag_id": bag_id,
                    "seqs": seqs,
                    "lease_seconds": total_lease,
                },
            )

    def work_forever(self, poll_seconds: float = 2.0) -> None:
        idle = 0.0
        while not self._stop.is_set():
            resp = self._post("/api/tasks/pull", {"node_id": self.node_id})
            tasks = (resp or {}).get("tasks") or []
            if not tasks:
                idle = min(IDLE_BACKOFF_MAX, idle * 2 + poll_seconds)
                time.sleep(min(idle, poll_seconds))
                continue
            idle = 0.0
            started_at = time.time()
            results = self.execute_chunk(tasks)
            self.maybe_renew(tasks, started_at)
            self._post("/api/tasks/complete", {"node_id": self.node_id, "results": results})

    def start_worker(self, poll_seconds: float = 2.0) -> threading.Thread:
        self._stop.clear()
        thread = threading.Thread(
            target=self.work_forever, args=(poll_seconds,), daemon=True, name="swarm-worker"
        )
        thread.start()
        return thread

    def stop_worker(self) -> None:
        self._stop.set()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Swarm node agent")
    parser.add_argument("--hub", default="http://127.0.0.1:8777")
    parser.add_argument("--no-bench", action="store_true", help="probe only, skip benchmarks")
    parser.add_argument("--once", action="store_true", help="register once and exit")
    parser.add_argument("--work", action="store_true", help="also run the pull-based worker loop")
    parser.add_argument("--poll", type=float, default=2.0)
    args = parser.parse_args(argv)

    agent = Agent(hub_url=args.hub, bench=not args.no_bench)
    t0 = time.time()
    ok = agent.probe_and_register()
    elapsed = time.time() - t0
    if not ok:
        print(
            f"agent: probe ok but hub unreachable at {args.hub}; will retry in background loop",
            file=sys.stderr,
        )
    else:
        print(f"agent: registered as {agent.node_id[:8]} in {elapsed:.1f}s")
    if args.once:
        return 0 if ok else 1
    if args.work:
        print("agent: worker loop started (pull-based, chunk sized to measured throughput)")
        agent.start_worker(poll_seconds=args.poll)
    agent.heartbeat_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
