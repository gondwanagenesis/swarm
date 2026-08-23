"""Hub server. Stdlib http.server + sqlite registry. Endpoints:

GET  /api/ping       liveness (also the RTT probe target)
POST /api/echo       byte mirror (the bandwidth measurement target)
POST /api/register   node registration: {profile, capability, benchmarks}
POST /api/heartbeat  {node_id}
POST /api/link       link measurement report
GET  /api/nodes      node registry summary
GET  /api/nodes/<id> full stored profile
GET  /api/links      link matrix
GET  /api/anomalies  recent anomalies
GET  /               dashboard
"""

from __future__ import annotations

import argparse
import contextlib
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..core.identity import canonical_hash
from ..core.models import BenchResult, LinkMeasurement, NodeProfile
from ..core.serde import from_dict
from ..integrator.llm import from_env as llm_from_env
from .dashboard import render_dashboard
from .enrollment import Enrollment
from .queue import WorkQueue
from .registry import Registry
from .scheduler import ChunkPlanner

MAX_BODY = 64 * 1024 * 1024


def _swarm_version() -> str:
    try:
        import swarm

        return swarm.__version__
    except Exception:
        return "unknown"


class Hub:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8777,
        db_path: str = ":memory:",
        require_token: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.require_token = require_token
        self.registry = Registry(db_path)
        self.queue = WorkQueue(self.registry._conn, lock=self.registry._lock)
        self.enrollment = Enrollment(self.registry._conn, lock=self.registry._lock)
        self.planner = ChunkPlanner(self.registry)
        self.llm_config = llm_from_env()
        from ..integrator.policy import BrainRouter, local_brain_config_from_env

        self.brain = BrainRouter(
            self.registry,
            frontier=self.llm_config,
            local=local_brain_config_from_env(),
        )
        import os

        from .workshop import Workshop

        autopilot_env = os.environ.get("SWARM_AUTOPILOT", "1")
        self.workshop = Workshop(
            self.registry._conn,
            Path(__file__).resolve().parents[2],
            lock=self.registry._lock,
            autopilot=autopilot_env not in ("0", "false", "no"),
        )
        self.agent_payload: Optional[bytes] = None
        self.latest_bundle_hash: Optional[str] = None
        self.started_at = time.time()
        self._httpd = None
        self._mdns_stop: Optional[Any] = None  # threading.Event once LAN mode is on
        self._mdns_thread: Optional[Any] = None
        self.advertised_url: Optional[str] = None

    # -- LAN discovery ----------------------------------------------------

    def start_lan_announce(
        self, address: Optional[str] = None, interval: float = 60.0
    ) -> Optional[str]:
        """Announce this hub over mDNS so agents on the LAN can find it.

        Opt-in only — never called by ``__init__``. Advertises the *reachable*
        address (never loopback) so a discovered instance resolves to something
        another machine can actually connect to. Returns the advertised base
        URL, or ``None`` when no LAN address could be determined.

        Announcing is not recruiting: agents that discover this hub still have
        to enroll through the token/consent path (AGENTS.md, no self-propagation).
        """
        import threading

        from ..agent.mdns import (
            HUB_PREFIX,
            announce,
            instance_name,
            primary_ip,
        )

        ip = address or primary_ip()
        if not ip:
            return None
        # self.port is only the REAL port after the socket binds (serve_forever
        # / start_background overwrite it). With Hub(port=0) an early caller
        # would otherwise advertise port 0, which resolves to nothing.
        port = int(self.port)
        if self._httpd is not None:
            with contextlib.suppress(Exception):
                port = int(self._httpd.server_address[1])
        if not port:
            # Advertising an unbound hub is advertising a lie. Say nothing.
            return None
        node = getattr(self.registry, "hub_id", None) or "{}".format(port)
        instance = instance_name(HUB_PREFIX, str(node))
        host = "swarm-hub-{}.local".format(str(node)[:12])
        stop = threading.Event()
        self._mdns_stop = stop

        def announce_loop() -> None:
            while not stop.is_set():
                # A blocked multicast network is not a hub failure.
                with contextlib.suppress(Exception):
                    announce(instance, host=host, port=port, address=ip)
                stop.wait(interval)

        thread = threading.Thread(target=announce_loop, daemon=True, name="swarm-hub-mdns")
        thread.start()
        self._mdns_thread = thread
        self.advertised_url = "http://{}:{}".format(ip, port)
        return self.advertised_url

    def stop_lan_announce(self) -> None:
        if self._mdns_stop is not None:
            self._mdns_stop.set()
        self._mdns_thread = None

    def set_agent_payload(self, payload: bytes) -> None:
        import hashlib

        self.agent_payload = payload
        self.latest_bundle_hash = hashlib.sha256(payload).hexdigest()

    def make_handler(self) -> type:
        hub = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "SwarmHub/0.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_json(self, obj: Any, status: int = 200) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, html: str, status: int = 200) -> None:
                body = html.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise ValueError("body too large")
                return self.rfile.read(length)

            def _read_json(self) -> Dict[str, Any]:
                raw = self._read_body()
                if not raw:
                    return {}
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("expected JSON object")
                return data

            def _bad(self, msg: str) -> None:
                self._send_json({"ok": False, "error": msg}, status=400)

            def do_GET(self) -> None:
                try:
                    path = self.path.split("?", 1)[0]
                    if path == "/api/ping":
                        self._send_json({"ok": True, "ts": time.time()})
                    elif path == "/api/bundle/latest":
                        if hub.agent_payload is None:
                            self._send_json({"ok": False, "error": "no bundle"}, status=404)
                        else:
                            body = hub.agent_payload
                            self.send_response(200)
                            self.send_header("Content-Type", "application/octet-stream")
                            self.send_header("X-Bundle-SHA256", hub.latest_bundle_hash or "")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                    elif path == "/api/config":
                        self._send_json(
                            {
                                "llm": hub.llm_config.status(),
                                "version": _swarm_version(),
                                "uptime_s": round(time.time() - hub.started_at, 1),
                            }
                        )
                    elif path == "/agent.pyz":
                        if hub.agent_payload is None:
                            self._send_json(
                                {"ok": False, "error": "agent bundle not built on this hub"},
                                status=404,
                            )
                        else:
                            body = hub.agent_payload
                            self.send_response(200)
                            self.send_header("Content-Type", "application/octet-stream")
                            self.send_header("Content-Disposition", "attachment; filename=swarm-agent.pyz")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                    elif path == "/api/nodes":
                        nodes = hub.registry.list_nodes()
                        for n in nodes:
                            stats = hub.queue.node_stats(n["node_id"])
                            n["tier"] = stats["tier"]
                            n["suspended"] = bool(stats["suspended"])
                        self._send_json({"nodes": nodes})
                    elif path.startswith("/api/nodes/"):
                        node_id = path.rsplit("/", 1)[-1]
                        detail = hub.registry.node_detail(node_id)
                        if detail is None:
                            self._send_json({"ok": False, "error": "unknown node"}, status=404)
                        else:
                            detail["benches"] = hub.registry.latest_benches(node_id)
                            self._send_json(detail)
                    elif path == "/api/links":
                        self._send_json({"links": hub.registry.list_links()})
                    elif path == "/api/anomalies":
                        self._send_json({"anomalies": hub.registry.recent_anomalies()})
                    elif path == "/api/coverage":
                        from .coverage import coverage_report

                        self._send_json(coverage_report(hub.registry))
                    elif path == "/api/fleet-power":
                        from .fleet_power import fleet_power

                        self._send_json(fleet_power(hub.registry))
                    elif path == "/api/verdicts":
                        self._send_json({"verdicts": hub.registry.list_verdicts()})
                    elif path == "/api/workshop":
                        self._send_json({"patches": hub.workshop.ledger()})
                    elif path == "/api/bindings":
                        self._send_json({"bindings": hub.registry.list_bindings()})
                    elif path == "/invite" or path.startswith("/invite/"):
                        token = path.rsplit("/", 1)[-1] if path != "/invite" else ""
                        if not token or hub.enrollment.validate(token) is None:
                            minted = hub.enrollment.create(role="node", label="invite-page")
                            token = minted["token"]
                        self._send_html(hub._invite_page(token))
                    elif path == "/bundle.pyz":
                        from urllib.parse import parse_qs, urlparse

                        q = parse_qs(urlparse(self.path).query)
                        token = (q.get("token") or [""])[0]
                        if hub.enrollment.validate(token) is None:
                            self._send_json({"ok": False, "error": "invalid or expired token"}, status=403)
                            return
                        from .agentbundle import build_agent_pyz

                        bundle = build_agent_pyz(
                            config={"hub": f"http://{hub.host}:{hub.port}", "token": token}
                        )
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Disposition", "attachment; filename=swarm-agent.pyz")
                        self.send_header("Content-Length", str(len(bundle)))
                        self.end_headers()
                        self.wfile.write(bundle)
                    elif path == "/api/spore/events":
                        self._send_json({"events": hub.enrollment.spore_events()})
                    elif path == "/api/backup":
                        from .backup import snapshot_with_meta

                        snap = snapshot_with_meta(hub.registry._conn)
                        body = snap["bytes"]
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header(
                            "Content-Disposition",
                            f'attachment; filename="swarm-backup-{int(snap["at"])}.db"',
                        )
                        self.send_header("X-Backup-SHA256", snap["sha256"])
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    elif path == "/api/tokens":
                        self._send_json({"tokens": hub.enrollment.list_tokens()})
                    elif path == "/api/brain":
                        self._send_json(hub.brain_status())
                    elif path == "/api/bags":
                        self._send_json({"bags": hub.queue.open_bags()})
                    elif path.startswith("/api/bag/"):
                        bag_id = path.rsplit("/", 1)[-1]
                        status = hub.queue.bag_status(bag_id)
                        if status is None:
                            self._send_json({"ok": False, "error": "unknown bag"}, status=404)
                        else:
                            self._send_json(status)
                    elif path in ("/", "/index.html"):
                        self._send_html(render_dashboard(hub.registry, hub.queue))
                    else:
                        self._send_json({"ok": False, "error": "not found"}, status=404)
                except BrokenPipeError:
                    pass
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        self._send_json({"ok": False, "error": str(exc)}, status=500)

            def do_POST(self) -> None:
                try:
                    path = self.path.split("?", 1)[0]
                    if path == "/api/echo":
                        body = self._read_body()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    elif path == "/api/register":
                        payload = self._read_json()
                        node_id = hub.handle_register(payload)
                        self._send_json({"ok": True, "node_id": node_id})
                    elif path == "/api/heartbeat":
                        payload = self._read_json()
                        known = hub.registry.heartbeat(str(payload.get("node_id", "")))
                        self._send_json({"ok": known})
                    elif path == "/api/link":
                        payload = self._read_json()
                        link = from_dict(LinkMeasurement, payload)
                        hub.registry.record_link(link)
                        self._send_json({"ok": True})
                    elif path == "/api/bag/submit":
                        payload = self._read_json()
                        bag_id = hub.handle_submit(payload)
                        self._send_json({"ok": True, "bag_id": bag_id})
                    elif path == "/api/tasks/pull":
                        payload = self._read_json()
                        pull = hub.handle_pull(str(payload.get("node_id", "")))
                        self._send_json({"ok": True, **pull})
                    elif path == "/api/tasks/complete":
                        payload = self._read_json()
                        outcome = hub.queue.complete(
                            str(payload.get("node_id", "")),
                            list(payload.get("results") or []),
                        )
                        self._send_json({"ok": True, **outcome})
                    elif path == "/api/pipeline/plan":
                        payload = self._read_json()
                        from .pipeline import ModelStage, plan_pipeline

                        stages = [
                            ModelStage(
                                name=str(s.get("name") or f"stage-{i}"),
                                flops=float(s.get("flops") or 0.0),
                                peak_mem_bytes=int(s.get("peak_mem_bytes") or 0),
                            )
                            for i, s in enumerate(payload.get("stages") or [])
                        ]
                        plan = plan_pipeline(hub.registry, str(payload.get("model") or "unnamed"), stages)
                        self._send_json(
                            {
                                "model": plan.model_name,
                                "feasible": plan.feasible,
                                "reason": plan.reason,
                                "estimated_end_to_end_ms": plan.estimated_end_to_end_ms,
                                "assignments": [
                                    {"stage": a.stage, "node_id": a.node_id, "est_ms": a.est_ms}
                                    for a in plan.assignments
                                ],
                            }
                        )
                    elif path == "/api/brain/admin":
                        payload = self._read_json()
                        self._send_json(hub.handle_brain_admin(payload))
                    elif path == "/api/workshop/propose":
                        payload = self._read_json()
                        record = hub.workshop.propose(
                            title=str(payload.get("title") or ""),
                            reason=str(payload.get("reason") or ""),
                            files=dict(payload.get("files") or {}),
                            authored_by=str(payload.get("authored_by") or "human"),
                        )
                        self._send_json(record)
                    elif path == "/api/workshop/apply":
                        payload = self._read_json()
                        try:
                            out = hub.workshop.approve_and_apply(str(payload.get("patch_id") or ""))
                            self._send_json({"ok": True, **out})
                        except (KeyError, ValueError) as exc:
                            self._bad(str(exc))
                    elif path == "/api/workshop/rollback":
                        payload = self._read_json()
                        try:
                            out = hub.workshop.rollback(str(payload.get("patch_id") or ""))
                            self._send_json({"ok": True, **out})
                        except (KeyError, ValueError) as exc:
                            self._bad(str(exc))
                    elif path == "/api/spore/event":
                        payload = self._read_json()
                        hub.enrollment.log_spore_event(
                            str(payload.get("seed_node_id", "")),
                            str(payload.get("channel", "")),
                            str(payload.get("peer_hint", "")),
                        )
                        self._send_json({"ok": True})
                    elif path == "/api/sharpen":
                        payload = self._read_json()
                        hub.handle_sharpen(payload)
                        self._send_json({"ok": True})
                    elif path == "/api/self-update":
                        payload = self._read_json()
                        if hub.latest_bundle_hash is None:
                            self._send_json({"ok": False, "error": "no bundle available"})
                        else:
                            self._send_json(
                                {
                                    "ok": True,
                                    "sha256": hub.latest_bundle_hash,
                                    "url": "/api/bundle/latest",
                                    "size": len(hub.agent_payload or b""),
                                }
                            )
                    elif path == "/api/integrator/synthesize":
                        payload = self._read_json()
                        contract_name = str(payload.get("contract") or "prime_contract.json")
                        try:
                            from ..integrator.gate import load_contract
                            from ..integrator.synthesize import SynthesisLoop

                            contract = load_contract(contract_name)
                            # An explicit difficulty from the caller wins; with
                            # none, estimate it from the contract rather than
                            # falling back to a constant that escalated
                            # everything to the frontier lane.
                            raw = payload.get("difficulty")
                            if raw is None:
                                difficulty = hub.brain.estimate_difficulty(contract)
                                difficulty_source = "estimated"
                            else:
                                try:
                                    difficulty = float(raw)
                                    difficulty_source = "caller"
                                except (TypeError, ValueError):
                                    difficulty = hub.brain.estimate_difficulty(contract)
                                    difficulty_source = "estimated"
                            route = hub.brain.route(
                                difficulty, difficulty_source=difficulty_source
                            )
                            client = hub.brain.client_for(route["lane"])
                            if client is None:
                                self._send_json(
                                    {
                                        "ok": False,
                                        "reason": f"lane {route['lane_name']} unarmed",
                                        "route": route,
                                    }
                                )
                                return
                            loop = SynthesisLoop(hub.registry, client, contract)
                            out = loop.run()
                            out["route"] = route
                            self._send_json({"ok": bool(out.get("passed")), **out})
                        except FileNotFoundError:
                            self._bad(f"unknown contract {contract_name}")
                    else:
                        self._send_json({"ok": False, "error": "not found"}, status=404)
                except ValueError as exc:
                    self._bad(str(exc))
                except BrokenPipeError:
                    pass
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        self._send_json({"ok": False, "error": str(exc)}, status=500)

        return Handler

    def _invite_page(self, token: str) -> str:
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Join the swarm</title>
<style>body{{font-family:system-ui,sans-serif;background:#0f1215;color:#dde2e7;display:flex;justify-content:center;padding:10vh 1em 0}}
.card{{max-width:520px;background:#171c22;border:1px solid #2a3038;border-radius:10px;padding:2em}}
h1{{font-size:20px;color:#3cc492;margin-top:0}} code{{background:#0f1215;padding:2px 6px;border-radius:4px;font-size:12px}}
a.btn{{display:inline-block;background:#3cc492;color:#0f1215;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;margin-top:1em}}</style></head>
<body><div class="card"><h1>Join this swarm</h1>
<p>Your device was detected near an enrolled swarm seed. Joining runs a userspace agent that measures this machine's compute and shares spare cycles.</p>
<p><strong>No root. No kernel. No startup persistence without consent.</strong> You can revoke anytime.</p>
<p>Enrollment token: <code>{token[:12]}&hellip;</code></p>
<a class="btn" href="/bundle.pyz?token={token}">Download swarm-agent.pyz</a>
<p style="color:#556069;font-size:12px;margin-top:1.5em">Then run: <code>python swarm-agent.pyz --work</code> (bare Python 3.9+, no dependencies)</p>
</div></body></html>"""

    def handle_register(self, payload: Dict[str, Any]) -> str:
        if self.require_token:
            token = str(payload.get("token") or "")
            valid = self.enrollment.validate(token)
            if valid is None:
                raise ValueError("missing or invalid enrollment token (hub is in token-required mode)")
        profile_data = payload.get("profile") or {}
        capability_data = payload.get("capability") or {}
        profile = from_dict(NodeProfile, profile_data)
        self.registry.upsert_node(
            profile,
            capability_data,
            json.dumps(profile_data, sort_keys=True),
            json.dumps(capability_data, sort_keys=True),
        )
        for bench_data in payload.get("benchmarks") or []:
            bench = from_dict(BenchResult, bench_data)
            self.registry.record_bench(profile.node_id, bench)
        self._apply_verdicts(profile, payload)
        self._index_device_classes(profile)
        return profile.node_id

    def _index_device_classes(self, profile: NodeProfile) -> None:
        """Record which device classes this node can serve, from its MEASURED
        profile — this is what turns `device_class` work routing on.

        Two granularities per device, because contracts are written at both:
        the specific class (`intel:intel_r_iris_r_xe_graphics`) that an adapter
        is gated against, and the coarse kind (`gpu:generic`) a capability
        contract targets. `cpu:generic` is unconditional — every node that
        registered got here by running code on a CPU.

        Only devices the probe actually found are indexed. A class this node
        does not have is a class it does not serve; unrestricted work (no
        device_class) still reaches every node.
        """
        from .coverage import device_class

        classes = {"cpu:generic"}
        for dev in getattr(profile, "devices", None) or []:
            vendor = getattr(dev, "vendor", None)
            name = getattr(dev, "name", None)
            kind = getattr(dev, "kind", None)
            classes.add(device_class(vendor, name))
            if kind:
                classes.add("{}:generic".format(str(kind).strip().lower()))
        with contextlib.suppress(Exception):
            self.queue.set_node_device_classes(profile.node_id, sorted(classes))

    def _apply_verdicts(self, profile: NodeProfile, payload: Dict[str, Any]) -> None:
        """Worth-it gate + Tier-0 bindings land on registration (and any
        re-register after a hotplug). Every verdict is stored with its reason."""
        from .coverage import device_class
        from .verdicts import decide_verdict

        bindings = list(payload.get("bindings") or [])
        for b in bindings:
            self.registry.record_binding(
                profile.node_id,
                str(b.get("device_class") or "unknown:unknown"),
                b.get("runtime"),
                float(b.get("confidence") or 0.0),
                dict(b.get("evidence") or {}),
            )
        pilot = payload.get("pilot") or {}
        pilot_score = pilot.get("score_gflops")
        covered = {a["device_class"] for a in self.registry.list_adapters() if a.get("gate_run_id")}
        power = profile.power
        classes = {b.get("device_class") for b in bindings}
        for dev in profile.devices or []:
            dc = device_class(dev.vendor, dev.name)
            classes.add(dc)
        for dc in classes:
            if not dc:
                continue
            dc_bindings = [b for b in bindings if b.get("device_class") == dc]
            verdict, reason = decide_verdict(
                str(dc),
                dc_bindings,
                pilot_score,
                power.trust.value if power else None,
                power.watts if power else None,
                covered,
            )
            self.registry.record_verdict(profile.node_id, str(dc), verdict, reason)

    def handle_submit(self, payload: Dict[str, Any]) -> str:
        op = str(payload.get("op") or "")
        params_list = payload.get("params_list") or []
        if not op or not isinstance(params_list, list) or not params_list:
            raise ValueError("submit requires op and non-empty params_list")
        idem_keys = [canonical_hash({"op": op, "params": params}) for params in params_list]
        # device_class is what makes a bag hardware-targeted. Dropping it here
        # silently turned every classed submission into unrestricted work —
        # the HTTP path is the only one real agents use.
        device_class = payload.get("device_class")
        return self.queue.submit_bag(
            op,
            params_list,
            idem_keys,
            device_class=str(device_class) if device_class else None,
        )

    def handle_pull(self, node_id: str) -> Dict[str, Any]:
        if self.queue.is_suspended(node_id):
            return {"tasks": [], "suspended": True}
        open_bags = self.queue.open_bags()
        if not open_bags:
            return {"tasks": self.queue.pull_hedges(node_id), "suspended": False}
        status = open_bags[0]
        self.planner.touch_worker(node_id)
        stats = self.queue.node_stats(node_id)
        chunk, lease, predicted_ms = self.planner.plan(
            node_id=node_id,
            op=status["op"],
            bag_total=status["total"],
            bag_remaining=status["queued"],
            queue_stats=stats,
            active_worker_count=self.planner.active_count(),
        )
        items = self.queue.pull(node_id, chunk, lease, predicted_ms)
        return {"tasks": items, "suspended": False}

    def brain_status(self) -> Dict[str, Any]:
        status = self.brain.status()
        rows = (
            self.registry._conn.execute(
                "SELECT lane_name, difficulty, at FROM brain_routes ORDER BY at DESC LIMIT 100"
            ).fetchall()
            if self._table_exists("brain_routes")
            else []
        )
        status["recent_routes"] = [dict(r) for r in rows]
        return status

    def _table_exists(self, name: str) -> bool:
        return (
            self.registry._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            is not None
        )

    def handle_brain_admin(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        action = str(payload.get("action") or "")
        if action == "enable":
            self.brain.set_enabled(True)
        elif action == "disable":
            self.brain.set_enabled(False)
        elif action == "kill_on":
            self.brain.set_kill_switch(True)
        elif action == "kill_off":
            self.brain.set_kill_switch(False)
        elif action == "sensitivity":
            self.brain.set_sensitivity(float(payload.get("value", 0.7)))
        elif action == "autopilot_on":
            self.workshop.autopilot = True
        elif action == "autopilot_off":
            self.workshop.autopilot = False
        else:
            return {"ok": False, "error": f"unknown action {action!r}"}
        return {"ok": True, **self.brain.status()}

    def handle_sharpen(self, payload: Dict[str, Any]) -> None:
        """A node finished an idle-hone rung; fold fresh measurements into the
        registry. Pilot scores land as a low-trust bench so confidence grows
        with volume."""
        from ..core.models import BenchResult, MeasurementTrust

        node_id = str(payload.get("node_id") or "")
        if not node_id:
            return
        pilot = payload.get("pilot") or {}
        score = pilot.get("score_gflops")
        if score is not None:
            self.registry.record_bench(
                node_id,
                BenchResult(
                    name="cpu_fp32_gflops",
                    value=float(score),
                    unit="GFLOPS",
                    trust=MeasurementTrust.FALLBACK,
                    benchmark_run_id="sharpen-" + str(int(time.time())),
                ),
            )
        for b in payload.get("benchmarks") or []:
            self.registry.record_bench(node_id, from_dict(BenchResult, b))

    def serve_forever(self) -> Tuple[str, int]:
        handler = self.make_handler()
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = self._httpd.server_address[1]
        self._httpd.serve_forever()
        return self.host, self.port

    def start_background(self) -> Tuple[str, int]:
        import threading

        handler = self.make_handler()
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = self._httpd.server_address[1]
        thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        thread.start()
        return self.host, self.port

    def stop(self) -> None:
        self.stop_lan_announce()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        self.registry.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Swarm hub")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address; loopback by default so a hub is never exposed by accident",
    )
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument(
        "--lan",
        action="store_true",
        help=(
            "opt in to LAN mode: bind all interfaces and announce this hub over mDNS "
            "so agents can find it without --hub. Discovery only; joining still "
            "requires an enrollment token."
        ),
    )
    parser.add_argument(
        "--db",
        default=None,
        help="sqlite path; default ~/.swarm/hub.db (pass ':memory:' for ephemeral)",
    )
    parser.add_argument(
        "--serve-agent",
        action="store_true",
        help="build and serve the single-file agent at /agent.pyz",
    )
    args = parser.parse_args()
    if args.db is None:
        state_dir = Path.home() / ".swarm"
        state_dir.mkdir(parents=True, exist_ok=True)
        db = str(state_dir / "hub.db")
    else:
        db = args.db
    host = args.host
    if args.lan and host == "127.0.0.1":
        host = "0.0.0.0"  # explicit operator opt-in via --lan
    hub = Hub(host=host, port=args.port, db_path=db)
    if args.serve_agent:
        from .agentbundle import build_agent_pyz

        payload = build_agent_pyz()
        hub.set_agent_payload(payload)
        print(f"agent bundle ready at /agent.pyz ({len(payload)} bytes)")
    print(f"swarm hub listening on http://{host}:{args.port} (dashboard at /, db={db})")
    if args.lan:
        url = hub.start_lan_announce()
        if url:
            print(f"LAN mode: announcing {url} over mDNS (_swarm._tcp.local)")
            print("join from another machine on this network:")
            print(f"  python -m swarm.agent.daemon --hub {url}")
            print("  python -m swarm.agent.daemon            # finds this hub via mDNS")
            print("discovery advertises this hub; joining still needs an enrollment token.")
        else:
            print(
                "LAN mode: no non-loopback address found, so nothing is being announced. "
                "Pass --host <ip> explicitly."
            )
    try:
        hub.serve_forever()
    except KeyboardInterrupt:
        hub.stop()


if __name__ == "__main__":
    main()
