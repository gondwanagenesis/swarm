"""The node agent. What it does, in order:

  1. climb the capability tower (what can THIS agent do here?)
  2. full hardware probe (never raises, never hangs)
  3. fallback benchmarks at the floor the tower reached
  4. measure the link to the hub (RTT, bandwidth)
  5. discover inference runtimes (Ollama, llama.cpp, GGUF files)
  6. register all of it with the hub, then heartbeat
  7. with --work: long-poll for tasks; with llama.cpp present: reconcile the
     services the hub wants (rpc-server / llama-server for pooled models)

Everything is stdlib. Everything degrades: hub unreachable -> log and keep
breathing; benchmark crash -> anomaly, not exit.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
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
PULL_WAIT_S = 20.0
SERVICE_WAIT_S = 20.0
HONE_INTERVAL_S = 600.0


def _state_dir() -> Path:
    base = Path(os.environ.get("USERPROFILE") or str(Path.home())) if os.name == "nt" else Path.home()
    return base / ".swarm"


def _local_addresses() -> List[str]:
    """Addresses peers might reach this machine on: the LAN route and, if
    present, the Tailscale route. Found by asking the kernel which interface
    would carry traffic (UDP connect; no packet is sent). Never loopback."""
    found: List[str] = []
    for target in (("8.8.8.8", 53), ("100.100.100.100", 53)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.5)
            sock.connect(target)
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127.") and ip not in found:
                found.append(ip)
        except Exception:
            pass
        finally:
            sock.close()
    return found


class _KeyStore:
    """Per-(hub, node) keys the hub issued, in the node's state dir, 0600."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (_state_dir() / "node_keys.json")

    def _load(self) -> Dict[str, str]:
        try:
            return dict(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            return {}

    def get(self, hub: str, node_id: str) -> Optional[str]:
        return self._load().get(f"{hub}|{node_id}")

    def put(self, hub: str, node_id: str, key: str) -> None:
        data = self._load()
        data[f"{hub}|{node_id}"] = key
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(self.path, 0o600)
        except OSError:
            pass


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
        dedicated: bool = False,
        services: bool = False,
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
        self.dedicated = dedicated
        self.services_enabled = services
        self.hub_key = f"{self.hub_host}:{self.hub_port}"
        self.keys = _KeyStore()
        self.node_key: Optional[str] = None
        self.last_status: Optional[int] = None
        self.inference: Dict[str, Any] = {}
        self.service_manager: Any = None
        self._service_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _post(self, path: str, payload: Dict[str, Any], timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        self.last_status = None
        try:
            body = json.dumps(payload).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if self.node_id and self.node_key:
                headers["X-Swarm-Node"] = self.node_id
                headers["X-Swarm-Node-Key"] = self.node_key
            conn = http.client.HTTPConnection(self.hub_host, self.hub_port, timeout=timeout)
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            self.last_status = resp.status
            if resp.status == 401 and path != "/api/register":
                # The hub no longer knows our key (revoked, or a fresh hub db).
                self.registered = False
            if resp.status != 200:
                if resp.status in (401, 403):
                    try:
                        detail = json.loads(raw.decode("utf-8")).get("error")
                    except Exception:
                        detail = raw[:200]
                    print(f"agent: hub refused {path} ({resp.status}): {detail}", file=sys.stderr, flush=True)
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

        try:
            from ..probe.runtimes import discover_inference

            self.inference, inference_anomalies = discover_inference()
            profile.anomalies.extend(inference_anomalies)
        except Exception:
            self.inference = {}

        payload = {
            "profile": to_dict(profile),
            "capability": to_dict(capability),
            "benchmarks": [to_dict(b) for b in benches],
            "pilot": pilot,
            "bindings": bindings,
            "token": self.token,
            "role": self.role,
            "inference": self.inference,
            "addresses": _local_addresses(),
            "dedicated": self.dedicated,
        }
        # A key from an earlier run lets us re-register without a token (the
        # token may have expired long ago; the key is the standing consent).
        self.node_key = self.keys.get(self.hub_key, self.node_id) or self.node_key
        resp = self._post("/api/register", payload, timeout=30.0)
        if resp is None and self.last_status == 401 and self.node_key:
            self.node_key = None  # stale key: fall back to the token
            resp = self._post("/api/register", payload, timeout=30.0)
        if resp and resp.get("ok"):
            self.registered = True
            if resp.get("node_key"):
                self.node_key = str(resp["node_key"])
                self.keys.put(self.hub_key, self.node_id, self.node_key)
            self._post("/api/link", to_dict(link))
            self._ensure_services()
        return self.registered

    # -- services (pooled models) -----------------------------------------------

    def _ensure_services(self) -> None:
        """Start the service reconcile loop once, if this node can run any
        llama.cpp service at all. A node without llama.cpp never polls."""
        if not self.services_enabled or self._service_thread is not None:
            return
        llama = (self.inference or {}).get("llama") or {}
        binaries = {k: v for k, v in llama.items() if k in ("llama_rpc", "llama_server") and v}
        if not binaries:
            return
        from ..probe.runtimes import resolve_local_model
        from .services import ServiceManager
        from .welfare import welfare_gate

        def welfare() -> Dict[str, Any]:
            if self.ignore_welfare:
                return {"allowed": True}
            return welfare_gate(dedicated=self.dedicated)

        self.service_manager = ServiceManager(binaries, resolve_local_model, welfare=welfare)
        self._service_thread = threading.Thread(target=self._service_loop, daemon=True, name="swarm-services")
        self._service_thread.start()

    def _service_loop(self) -> None:
        version: Optional[int] = None
        statuses: List[Dict[str, Any]] = []
        while not self._stop.is_set():
            settling = any(s.get("state") == "starting" for s in statuses)
            payload: Dict[str, Any] = {
                "node_id": self.node_id,
                "services": statuses,
                "version": version,
                "wait_s": 0.0 if settling else SERVICE_WAIT_S,
            }
            resp = self._post("/api/services/sync", payload, timeout=SERVICE_WAIT_S + 15.0)
            if resp is None or not resp.get("ok"):
                self._stop.wait(5.0)
                continue
            version = resp.get("version")
            try:
                statuses = self.service_manager.reconcile(resp.get("desired") or [])
            except Exception as exc:
                print(f"[services] reconcile failed safely: {exc}", flush=True)
                statuses = []
            if any(s.get("state") == "starting" for s in statuses):
                self._stop.wait(1.0)

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
                if resp is None or not resp.get("ok") or not self.registered:
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

        cfg = bundled_config()
        hub_url = cfg.get("hub") or f"http://{self.hub_host}:{self.hub_port}"
        token = str(cfg.get("token") or self.token or "")
        own_bundle: Optional[bytes] = None
        try:
            if sys.argv and sys.argv[0].endswith(".pyz"):
                own_bundle = Path(sys.argv[0]).read_bytes()
        except OSError:
            own_bundle = None
        from .join_scripts import render_posix, render_powershell
        from .mdns import primary_ip

        seed_url = f"http://{primary_ip() or '127.0.0.1'}:8788"

        def serve(page: str) -> None:
            import socketserver

            class Handler(
                __import__("http.server", fromlist=["BaseHTTPRequestHandler"]).BaseHTTPRequestHandler
            ):
                def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self):
                    path = self.path.split("?", 1)[0]
                    if path == "/swarm-agent.pyz" and own_bundle:
                        self._send(own_bundle, "application/octet-stream")
                    elif path == "/join.sh" and own_bundle:
                        self._send(render_posix(hub_url, token, True, seed_url).encode("utf-8"), "text/x-shellscript")
                    elif path == "/join.ps1" and own_bundle:
                        self._send(render_powershell(hub_url, token, False, seed_url).encode("utf-8"), "text/plain")
                    else:
                        self._send(page.encode("utf-8"), "text/html; charset=utf-8")

                def log_message(self, *args):
                    return

            class Server(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
                daemon_threads = True

            server = Server(("0.0.0.0", 8788), Handler)
            server.serve_forever()

        if own_bundle:
            kit = (
                "<h2>Join from this seed</h2>"
                "<p>Linux / Mac / Android-Termux:</p>"
                f"<pre>curl -fsSL {seed_url}/join.sh | sh</pre>"
                "<p>Windows (PowerShell):</p>"
                f"<pre>irm {seed_url}/join.ps1 | iex</pre>"
                f"<p>Or download <a href='/swarm-agent.pyz'>swarm-agent.pyz</a> and run "
                "<code>python swarm-agent.pyz --work</code>.</p>"
            )
        else:
            kit = f"<p><a href='{hub_url}/invite'>Join this swarm</a></p>"
        page = (
            "<html><head><meta name='viewport' content='width=device-width,initial-scale=1'></head>"
            "<body style='font-family:sans-serif;padding:1.5em;max-width:640px'>"
            "<h1>Swarm seed</h1>"
            "<p>This device carries a swarm seed. Running one of the lines below on YOUR device "
            "joins it to the swarm: a userspace agent that measures the machine and shares spare "
            "cycles, backs off when you use it, and prints its own uninstall line. Nothing runs "
            "until you run it.</p>"
            f"{kit}<p style='color:#777;font-size:12px'>hub: {hub_url}</p>"
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
            # A chunk can span bags; every bag's leases get renewed, not just
            # the first one's.
            by_bag: Dict[str, List[int]] = {}
            for t in tasks:
                by_bag.setdefault(t["bag_id"], []).append(int(t["seq"]))
            for bag_id, seqs in by_bag.items():
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
        last_hone = time.time()
        while not self._stop.is_set():
            if not self.ignore_welfare:
                welfare = welfare_gate(dedicated=True) if self.dedicated else welfare_gate()
                if not welfare["allowed"]:
                    if not blocked_logged:
                        print(f"[welfare] worker parked: {welfare['reason']}", flush=True)
                        blocked_logged = True
                    time.sleep(max(poll_seconds, WELFARE_CHECK_S))
                    continue
                blocked_logged = False
            long_poll = idle > 0
            resp = self._post(
                "/api/tasks/pull",
                {"node_id": self.node_id, "wait_s": PULL_WAIT_S if long_poll else 0.0},
                timeout=PULL_WAIT_S + 15.0 if long_poll else 10.0,
            )
            if resp is None and not self.registered:
                self._stop.wait(poll_seconds)
                continue
            tasks = (resp or {}).get("tasks") or []
            if not tasks:
                # idle rung: burn spare cycles on self-knowledge, not tokens
                from .idle import IdleHone

                if hone is None:
                    hone = IdleHone(self.node_id)
                # Paced: a benchmark on every empty poll would turn an idle
                # phone into a hand warmer. Self-knowledge, not self-harm.
                if time.time() - last_hone >= HONE_INTERVAL_S:
                    last_hone = time.time()
                    try:
                        report = hone.sharpen(self)
                        if report.get("ran"):
                            print(f"[idle] sharpened: {report['ran']}", flush=True)
                    except Exception:
                        pass
                if self.self_update:
                    from .updater import apply_update

                    try:
                        headers = (
                            {"X-Swarm-Node": self.node_id, "X-Swarm-Node-Key": self.node_key}
                            if self.node_key
                            else None
                        )
                        outcome = apply_update(
                            f"http://{self.hub_host}:{self.hub_port}", headers=headers, node_id=self.node_id
                        )
                        if outcome not in ("current", "no-offer"):
                            print(f"[update] {outcome}", flush=True)
                    except Exception as exc:
                        print(f"[update] failed safely: {exc}", flush=True)
                idle = min(IDLE_BACKOFF_MAX, idle * 2 + poll_seconds)
                if not (long_poll and resp is not None):
                    # a long-poll already waited at the hub; no extra nap
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


def lower_own_priority() -> bool:
    """Make this agent — and everything it launches — yield to the owner.

    Children inherit it (POSIX nice; Windows BELOW_NORMAL is inherited by
    child processes), so map jobs and llama.cpp never compete with the apps
    you are actually using. Returns whether the OS accepted it."""
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            # The pseudo-handle is -1; without a pointer restype it is
            # truncated to 32 bits on 64-bit Windows and the call fails.
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            handle = ctypes.c_void_p(kernel32.GetCurrentProcess())
            return bool(kernel32.SetPriorityClass(handle, 0x00004000))
        os.nice(10)
        return True
    except Exception:
        return False


class SingleInstance:
    """One agent per (machine, node identity). Launchers restart the agent
    if it dies; this lock is what makes that safe — a second copy (a
    watchdog racing a self-update restart, a double autostart) waits briefly
    for the lock, then exits instead of doubling the node."""

    def __init__(self, name: str = "agent") -> None:
        self.path = _state_dir() / f"{name}.lock"
        self._fh: Any = None

    def acquire(self, wait_s: float = 15.0) -> bool:
        deadline = time.time() + wait_s
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            fh = open(self.path, "a+")  # noqa: SIM115 - held for the process lifetime
            try:
                if os.name == "nt":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fh = fh
                return True
            except OSError:
                fh.close()
                if time.time() >= deadline:
                    return False
                time.sleep(1.0)


def _redirect_output(path: str, max_bytes: int = 5 * 1024 * 1024) -> None:
    """Send prints to a log file, rotating once past `max_bytes` so an
    always-on phone never fills its storage with agent chatter."""
    try:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > max_bytes:
            os.replace(target, target.with_suffix(target.suffix + ".1"))
        fh = open(target, "a", buffering=1, encoding="utf-8", errors="replace")  # noqa: SIM115 - lives as stdout
        sys.stdout = fh
        sys.stderr = fh
        print(f"--- agent start {time.strftime('%Y-%m-%d %H:%M:%S')} ---", flush=True)
    except Exception:
        pass


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
    parser.add_argument(
        "--dedicated",
        action="store_true",
        default=bool(cfg.get("dedicated")),
        help="this machine exists to compute (old phone on a charger, GPU box): "
        "work even while someone is at the keyboard; battery rules still apply",
    )
    parser.add_argument(
        "--node-id",
        default=None,
        help="override the persisted node id (e.g. two agents on one machine for testing)",
    )
    parser.add_argument(
        "--no-services",
        action="store_true",
        help="never run llama.cpp services for pooled models on this node",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="append output to this file (used by autostart, where there is no console)",
    )
    parser.add_argument(
        "--normal-priority",
        action="store_true",
        help="do not lower this agent's CPU priority (default: yield to the owner's apps)",
    )
    args = parser.parse_args(argv)
    if args.log:
        _redirect_output(args.log)
    if not args.normal_priority:
        lower_own_priority()
    if not args.once:
        lock = SingleInstance("agent-" + args.node_id if args.node_id else "agent")
        if not lock.acquire():
            print("agent: another agent already runs this node; exiting", file=sys.stderr, flush=True)
            return 0

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
        node_id=args.node_id,
        token=args.token,
        role="seed" if args.seed else "node",
        self_update=args.self_update,
        dedicated=args.dedicated,
        services=not args.no_services,
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
        inf = agent.inference or {}
        if inf.get("runtimes"):
            names = [m.get("name") for m in inf.get("models") or []]
            print(f"agent: runtimes {inf['runtimes']}; models {names}")
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
