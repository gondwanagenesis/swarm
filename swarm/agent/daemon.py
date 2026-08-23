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
from ..bench.pilot import pilot_cpu_fp32
from ..core.models import BenchResult
from ..core.serde import to_dict
from ..probe.discovery import discover_runtimes
from ..probe.hotplug import HotplugWatcher
from ..probe.orchestrator import full_probe
from ..transport.link import LinkProber
from .adapter_runtime import run_spec, task_adapter_spec
from .ops import OPS

HEARTBEAT_SECONDS = 30.0
IDLE_BACKOFF_MAX = 10.0
LEASE_RENEW_FRACTION = 1.0 / 3.0
WELFARE_CHECK_S = 30.0


def bundled_config() -> Dict[str, Any]:
    """When running from a .pyz bundle, the hub may have baked in
    swarm_config.json (hub URL + enrollment token). Env var overrides."""
    import zipfile

    cfg: Dict[str, Any] = {}
    try:
        archive = sys.argv[0] if sys.argv and sys.argv[0].endswith(".pyz") else None
        if archive:
            with zipfile.ZipFile(archive) as zf:
                if "swarm_config.json" in zf.namelist():
                    cfg = json.loads(zf.read("swarm_config.json").decode("utf-8"))
    except Exception:
        pass
    import os

    if os.environ.get("SWARM_TOKEN"):
        cfg["token"] = os.environ["SWARM_TOKEN"]
    if os.environ.get("SWARM_HUB"):
        cfg["hub"] = os.environ["SWARM_HUB"]
    return cfg


class Agent:
    def __init__(
        self,
        hub_url: str,
        bench: bool = True,
        rebench_interval: float = 86400.0,
        node_id: Optional[str] = None,
        token: Optional[str] = None,
        role: str = "node",
        ignore_welfare: bool = False,
        self_update: bool = False,
    ) -> None:
        parsed = urlparse(hub_url if "://" in hub_url else "http://" + hub_url)
        self.hub_host = parsed.hostname or "127.0.0.1"
        self.hub_port = parsed.port or 8777
        self.do_bench = bench
        self.rebench_interval = rebench_interval
        self.node_id_override = node_id
        self.token = token
        self.role = role
        self.registered = False
        self.node_id = ""
        self.last_bench_at = 0.0
        self.ignore_welfare = ignore_welfare
        self.self_update = self_update
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

        pilot: Dict[str, Any] = {}
        bindings: List[Dict[str, Any]] = []
        try:
            pilot = pilot_cpu_fp32()
            bindings, discovery_anomalies = discover_runtimes(profile.devices, capability)
            profile.anomalies.extend(discovery_anomalies)
        except Exception:
            pass

        benches: List[BenchResult] = []
        if self.do_bench:
            benches = run_floor_benchmarks()
            self.last_bench_at = time.time()

        payload = {
            "profile": to_dict(profile),
            "capability": to_dict(capability),
            "benchmarks": [to_dict(b) for b in benches],
            "pilot": pilot,
            "bindings": bindings,
            "token": self.token,
            "role": self.role,
        }
        resp = self._post("/api/register", payload)
        if resp and resp.get("ok"):
            self.registered = True
            self._post("/api/link", to_dict(link))
        return self.registered

    def heartbeat_forever(self) -> None:
        watcher: Optional[HotplugWatcher] = None
        try:
            watcher = HotplugWatcher()
            watcher.on_change(self._on_devices_changed)
            watcher.start()
        except Exception:
            watcher = None
        try:
            while True:
                welfare = None
                try:
                    from .welfare import battery_state, user_idle_seconds

                    welfare = {"battery": battery_state(), "user_idle_s": user_idle_seconds()}
                except Exception:
                    welfare = None
                resp = self._post("/api/heartbeat", {"node_id": self.node_id, "welfare": welfare})
                if resp is None or not resp.get("ok"):
                    self.registered = False
                    self.probe_and_register()
                elif self.do_bench and (time.time() - self.last_bench_at) > self.rebench_interval:
                    run_floor_benchmarks()
                    self.last_bench_at = time.time()
                    self._post("/api/heartbeat", {"node_id": self.node_id})
                time.sleep(HEARTBEAT_SECONDS)
        finally:
            if watcher is not None:
                watcher.stop()

    def _on_devices_changed(self, added: list, removed: list) -> None:
        """New or removed hardware => re-register (idempotent upsert) so the
        hub's coverage view picks it up. Debounced trivially by the watcher's
        interval; full_probe re-runs are cheap on known machines."""
        time.sleep(2.0)
        self.probe_and_register()

    def run_seed(self, offer_hook: Optional[Any] = None) -> None:
        """Spore mode: probe-lite, register role=seed, watch for attachment,
        fire an event to the hub on every arrival. Consent happens on the
        attached device; the seed never self-installs."""
        from .spore import AttachmentWatcher

        watcher = AttachmentWatcher()
        watcher.on_attach(self._on_attachment)
        watcher.start()

        def serve(page: str) -> None:
            import socketserver

            class Handler(
                __import__("http.server", fromlist=["BaseHTTPRequestHandler"]).BaseHTTPRequestHandler
            ):
                def do_GET(self):
                    body = page.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args):
                    return

            class Server(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
                daemon_threads = True

            server = Server(("0.0.0.0", 8788), Handler)
            server.serve_forever()

        hub_url = f"http://{self.hub_host}:{self.hub_port}"
        page = (
            "<html><body style='font-family:sans-serif;padding:2em'>"
            "<h1>Swarm seed detected</h1>"
            "<p>This device attached to a swarm seed. One click joins the swarm — "
            "the agent runs in userspace, never installs without your consent.</p>"
            f"<p><a href='{hub_url}/invite'>Join this swarm</a></p>"
            "</body></html>"
        )
        threading.Thread(target=serve, args=(page,), daemon=True, name="swarm-seed-serve").start()
        from .mdns import announce

        def announce_loop() -> None:
            while not self._stop.is_set():
                try:
                    announce(f"seed-{(self.node_id or 'unknown')[:12]}._swarm._tcp.local")
                except Exception:
                    continue
                self._stop.wait(60.0)

        threading.Thread(target=announce_loop, daemon=True, name="swarm-seed-mdns").start()
        while not self._stop.is_set():
            time.sleep(1.0)
        watcher.stop()

    def _on_attachment(self, channel: str, hint: str) -> None:
        self._post(
            "/api/spore/event",
            {"seed_node_id": self.node_id or "seed", "channel": channel, "peer_hint": hint},
        )

    def run_once(self) -> bool:
        return self.probe_and_register()

    def execute_chunk(self, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Run one leased chunk. Each item is timed for the hub's EWMA;
        a failing op marks that item failed but never kills the chunk.

        An op the registry doesn't know is not automatically a failure: the
        task may carry an adapter (source or id), which runs through
        `adapter_runtime` in its own subprocess. An unknown op with no
        adapter still fails closed, exactly as before."""
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
            else:
                spec = task_adapter_spec(task)
                if spec is not None:
                    outcome = run_spec(spec)  # never raises
                    ok = bool(outcome.get("ok"))
                    payload = outcome
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
        from .welfare import welfare_gate

        idle = 0.0
        blocked_logged = False
        hone = None
        while not self._stop.is_set():
            if not self.ignore_welfare:
                welfare = welfare_gate()
                if not welfare["allowed"]:
                    if not blocked_logged:
                        print(f"[welfare] worker parked: {welfare['reason']}", flush=True)
                        blocked_logged = True
                    time.sleep(max(poll_seconds, WELFARE_CHECK_S))
                    continue
                blocked_logged = False
            resp = self._post("/api/tasks/pull", {"node_id": self.node_id})
            tasks = (resp or {}).get("tasks") or []
            if not tasks:
                # idle rung: burn spare cycles on self-knowledge, not tokens
                from .idle import IdleHone

                if hone is None:
                    hone = IdleHone(self.node_id)
                try:
                    report = hone.sharpen(self)
                    if report.get("ran"):
                        print(f"[idle] sharpened: {report['ran']}", flush=True)
                except Exception:
                    pass
                if self.self_update:
                    from .updater import apply_update

                    try:
                        outcome = apply_update(f"http://{self.hub_host}:{self.hub_port}")
                        if outcome not in ("current", "no-offer"):
                            print(f"[update] {outcome}", flush=True)
                    except Exception as exc:
                        print(f"[update] failed safely: {exc}", flush=True)
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
    cfg = bundled_config()
    parser = argparse.ArgumentParser(description="Swarm node agent")
    parser.add_argument(
        "--hub",
        default=cfg.get("hub"),
        help="hub base URL; omit to discover one on the LAN over mDNS",
    )
    parser.add_argument(
        "--discover-timeout",
        type=float,
        default=5.0,
        help="seconds to look for a hub on the LAN when --hub is not given",
    )
    parser.add_argument("--token", default=cfg.get("token"))
    parser.add_argument("--no-bench", action="store_true", help="probe only, skip benchmarks")
    parser.add_argument("--once", action="store_true", help="register once and exit")
    parser.add_argument("--work", action="store_true", help="also run the pull-based worker loop")
    parser.add_argument("--poll", type=float, default=2.0)
    parser.add_argument(
        "--seed",
        action="store_true",
        help="spore mode: probe-lite, watch for attached devices, serve the one-click invite page",
    )
    parser.add_argument(
        "--self-update",
        action="store_true",
        help="poll hub for updated agent bundles and hot-swap when offered",
    )
    args = parser.parse_args(argv)

    # No --hub given: look for one on the LAN before falling back to the
    # loopback default. Discovery only ever yields a CANDIDATE address —
    # joining still goes through the token/consent path, so finding a hub is
    # not the same as being recruited by one (AGENTS.md: no self-propagation).
    hub_url = args.hub
    if not hub_url:
        from .discovery_loop import discover_hub

        found = discover_hub(args.discover_timeout)
        if found:
            hub_url = found
            print(f"agent: discovered hub on the LAN at {hub_url}")
        else:
            hub_url = "http://127.0.0.1:8777"
            print(
                f"agent: no hub found on the LAN in {args.discover_timeout:.0f}s; "
                f"falling back to {hub_url}",
                file=sys.stderr,
            )

    agent = Agent(
        hub_url=hub_url,
        bench=not args.no_bench,
        token=args.token,
        role="seed" if args.seed else "node",
        self_update=args.self_update,
    )
    t0 = time.time()
    ok = agent.probe_and_register()
    elapsed = time.time() - t0
    if not ok:
        print(
            f"agent: probe ok but hub unreachable at {hub_url}; will retry in background loop",
            file=sys.stderr,
        )
    else:
        print(f"agent: registered as {agent.node_id[:8]} ({agent.role}) in {elapsed:.1f}s")
    if args.seed:
        print("agent: seed posture — watching for attached devices, invite page on :8788")
        agent.run_seed()
        return 0
    if args.once:
        return 0 if ok else 1
    if args.work:
        print("agent: worker loop started (pull-based, chunk sized to measured throughput, welfare-gated)")
        agent.start_worker(poll_seconds=args.poll)
    agent.heartbeat_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
