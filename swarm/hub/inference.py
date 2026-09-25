"""Models as a fleet resource: what can be served, where, and how to pool it.

This is M5 made real. The M5 scaffold (``pipeline.py``) proved the planning
could be honest; this module plans a model onto real machines AND runs it,
by reusing the engine that already exists for exactly this job — llama.cpp's
RPC backend — instead of writing a tensor transport of our own (Law 3:
discover before you generate).

How a model gets served
-----------------------
1. Every node reports, at registration, what it can already run
   (``probe.runtimes``): Ollama models, llama.cpp binaries, GGUF files, and
   the free memory llama.cpp itself measured on each device.
2. A request for a model arrives (``/v1/chat/completions`` or
   ``POST /api/models/deploy``). If it is an Ollama model, it becomes a
   one-task bag routed to a node that holds it (``model:<name>`` class).
3. If it is a GGUF model, :func:`plan_llama` decides the placement:
   - **one node** if the model fits a head node's measured free memory —
     sharding costs latency always and buys only capacity, so it is never
     done for a model that fits (the same law the scaffold enforced);
   - otherwise **the fewest extra nodes** that make it fit, filled largest
     first. Each helper runs ``rpc-server``; the head runs ``llama-server
     --rpc a,b --tensor-split ...`` with the split computed here, per layer,
     from measured free memory minus a host reserve. Why fewest-nodes and
     not the scaffold's min-max: llama.cpp's layer split does not pipeline a
     single decode stream — every token crosses every node in turn — so the
     cost that matters is per-hop network latency, not the slowest stage.
4. The hub writes *desired services*; agents reconcile and report status
   (``agent/services.py``); :meth:`Inference.tick` advances each deployment
   ``placing -> loading -> ready`` or to ``failed`` with a reason.
5. The gateway proxies OpenAI-compatible requests to the ready head.

Honesty labels: weights are measured (file size on disk); free memory is
measured (the runtime's device list, or the probe's RAM reading); the runtime
overhead margin (KV cache, compute buffers) is an ESTIMATE and is reported as
one in every plan.
"""

from __future__ import annotations

import ipaddress
import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ._sync import synchronized

MIB = 1024 * 1024
GIB = 1024 * MIB

ONLINE_WINDOW_S = 90.0
DEFAULT_CTX = 4096
RPC_BASE_PORT = 50052
SERVER_BASE_PORT = 8090
IDLE_UNLOAD_S = float(os.environ.get("SWARM_MODEL_IDLE_S", "1800"))
SERVICE_STALE_S = 120.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS node_runtimes (
    node_id TEXT PRIMARY KEY,
    data_json TEXT,
    reach_host TEXT,
    addresses_json TEXT,
    at REAL
);
CREATE TABLE IF NOT EXISTS services (
    service_id TEXT PRIMARY KEY,
    deployment TEXT,
    node_id TEXT,
    kind TEXT,
    spec_json TEXT,
    desired INTEGER DEFAULT 1,
    status_json TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_services_node ON services(node_id, desired);
CREATE TABLE IF NOT EXISTS deployments (
    model TEXT PRIMARY KEY,
    state TEXT,
    head_node_id TEXT,
    plan_json TEXT,
    endpoint TEXT,
    reason TEXT,
    created_at REAL,
    updated_at REAL,
    last_used REAL
);
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def normalize_model(name: str) -> str:
    return (name or "").strip()


def model_aliases(name: str) -> List[str]:
    """Names a client may use for a model. Ollama's ``foo:latest`` answers to
    ``foo`` too; everything is also matched case-insensitively by callers."""
    out = [name]
    if name.endswith(":latest"):
        out.append(name[: -len(":latest")])
    return out


def usable_bytes(free: Optional[int], kind: str) -> int:
    """Free memory minus a reserve for the host. The machine is yours first:
    RAM keeps max(2 GiB, 25%) back; accelerator memory max(512 MiB, 10%)."""
    if not free or free <= 0:
        return 0
    reserve = max(2 * GIB, int(free * 0.25)) if kind == "cpu" else max(512 * MIB, int(free * 0.1))
    return max(0, int(free) - reserve)


def estimate_need(size_bytes: int, ctx: int = DEFAULT_CTX) -> Tuple[int, str]:
    """Weights (measured) + an overhead margin (estimated) for KV cache and
    compute buffers. Returned with the basis so every plan can say so."""
    overhead = int(size_bytes * 0.10) + 512 * MIB + int(ctx / 4096 * 256 * MIB)
    return int(size_bytes) + overhead, (
        f"weights {int(size_bytes)} B measured from the file; +{overhead} B overhead "
        f"ESTIMATED (10% + 512 MiB + KV margin for ctx {ctx})"
    )


def _largest_remainder(shares: List[float], total: int) -> List[int]:
    """Integer layer counts proportional to `shares`, summing to `total`."""
    s = sum(shares)
    if s <= 0 or total <= 0:
        return [0] * len(shares)
    raw = [x / s * total for x in shares]
    floors = [int(r) for r in raw]
    left = total - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - floors[i], reverse=True)
    for i in order[:left]:
        floors[i] += 1
    return floors


def _is_loopback(host: Optional[str]) -> bool:
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return str(host or "").lower() in ("localhost", "")


def _is_tailscale(host: Optional[str]) -> bool:
    try:
        return ipaddress.ip_address(str(host)) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# the planner (pure — no database, fully testable)
# ---------------------------------------------------------------------------


def node_capacity(node: Dict[str, Any]) -> Tuple[int, str, List[Dict[str, Any]]]:
    """What one node can hold for a model, and on what.

    A node with llama.cpp-visible accelerators offers those (their measured
    free memory). A node without offers its RAM. Never both: on a unified-
    memory laptop the GPU's free memory IS system RAM, and counting it twice
    would be exactly the kind of fantasy sum this organism refuses to print.
    """
    gpus = [d for d in node.get("devices") or [] if d.get("free_bytes")]
    if gpus:
        per = [dict(d, usable=usable_bytes(d["free_bytes"], "gpu")) for d in gpus]
        return sum(d["usable"] for d in per), "gpu", per
    return usable_bytes(node.get("ram_free_bytes"), "cpu"), "cpu", []


def plan_llama(
    model: Dict[str, Any],
    heads: List[Dict[str, Any]],
    helpers: List[Dict[str, Any]],
    force_shard: bool = False,
    ctx: int = DEFAULT_CTX,
) -> Dict[str, Any]:
    """Place a GGUF model. Returns a plan dict with ``feasible`` and a
    ``reason`` either way. Inputs are measured-only node views::

        {"node_id", "hostname", "devices": [{"id", "free_bytes"}],
         "ram_free_bytes"}

    ``heads`` hold the model file and ``llama-server``; ``helpers`` have an
    RPC worker. ``force_shard`` spreads the model over every helper even when
    it fits one node — a demo/proof switch, loudly recorded in the plan.
    """
    name = model.get("name") or "model"
    size = int(model.get("size_bytes") or 0)
    if size <= 0:
        return {"model": name, "feasible": False, "reason": "model size unknown; refusing to guess"}
    need, need_basis = estimate_need(size, ctx)
    n_layers = int(model.get("n_layers") or 0)
    if not heads:
        return {
            "model": name,
            "feasible": False,
            "need_bytes": need,
            "reason": "no online node holds this model file together with llama-server",
        }

    scored = []
    for h in heads:
        cap, kind, per = node_capacity(h)
        scored.append((cap, 1 if kind == "gpu" else 0, h, kind, per))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    head_cap, _, head, head_kind, head_devs = scored[0]

    base = {
        "model": name,
        "need_bytes": need,
        "need_basis": need_basis,
        "ctx": ctx,
        "n_layers": n_layers or None,
        "head": {"node_id": head["node_id"], "hostname": head.get("hostname"), "kind": head_kind,
                 "usable_bytes": head_cap},
        "force_shard": bool(force_shard),
    }

    if not force_shard and head_cap >= need:
        return dict(
            base,
            feasible=True,
            mode="single",
            participants=[{"node_id": head["node_id"], "role": "head", "bytes": need, "layers": n_layers or None}],
            rpc=[],
            tensor_split=None,
            n_gpu_layers=999 if head_kind == "gpu" else 0,
            reason=(
                f"fits on {head.get('hostname') or head['node_id']} alone ({need} B needed, "
                f"{head_cap} B usable after host reserve); not sharding: sharding buys capacity, never latency"
            ),
        )

    pool = []
    head_build = head.get("build")
    mismatched = []
    for h in helpers:
        if h["node_id"] == head["node_id"]:
            continue
        # llama.cpp's RPC protocol changes between builds, and a head silently
        # drops a helper it cannot talk to. Known-different builds never pool;
        # unknown builds (rpc-only nodes) are tried and verified at load.
        if head_build and h.get("build") and h["build"] != head_build:
            mismatched.append(f"{h.get('hostname') or h['node_id'][:8]} (build {h['build']})")
            continue
        cap, kind, per = node_capacity(h)
        if cap > 0:
            device = max(per, key=lambda d: d["usable"])["id"] if per else ("CPU" if h.get("devices") is not None else None)
            pool.append({"node": h, "cap": cap, "kind": kind, "device": device})
    pool.sort(key=lambda p: p["cap"], reverse=True)

    version_note = (
        f"; skipped helpers on a different llama.cpp build than the head's {head_build}: "
        + ", ".join(mismatched) + " (update llama.cpp there to pool them)"
        if mismatched
        else ""
    )
    if force_shard:
        chosen = pool
        if not chosen:
            return dict(
                base,
                feasible=False,
                reason="force_shard asked for a split but no compatible helper node has an RPC worker" + version_note,
            )
    else:
        chosen = []
        total = head_cap
        for p in pool:
            if total >= need:
                break
            chosen.append(p)
            total += p["cap"]
    total_cap = head_cap + sum(p["cap"] for p in chosen)
    if total_cap < need:
        return dict(
            base,
            feasible=False,
            reason=(
                f"needs {need} B; head + {len(chosen)} helper(s) offer {total_cap} B usable after host reserves "
                f"(short by {need - total_cap} B). Add a node, free memory, or pick a smaller quant."
                + version_note
            ),
        )

    # Bytes per participant: fewest-nodes fill (head first, then largest
    # helpers) — or, when forced, proportional to capacity across all.
    caps = [head_cap] + [p["cap"] for p in chosen]
    if force_shard:
        shares = [c / total_cap * need for c in caps]
    else:
        shares = []
        remaining = need
        for c in caps:
            take = min(c, remaining)
            shares.append(take)
            remaining -= take
    layers = _largest_remainder(shares, n_layers) if n_layers else [0] * len(shares)
    if n_layers:
        # A participant rounded down to zero layers is not a participant.
        keep = [0] + [i for i in range(1, len(caps)) if layers[i] > 0]
        if len(keep) != len(caps):
            chosen = [chosen[i - 1] for i in keep[1:]]
            shares = [shares[i] for i in keep]
            layers = [layers[i] for i in keep]
    weights = layers if n_layers else [round(s / MIB) for s in shares]

    # llama.cpp device order: the head's own accelerators first, then each
    # --rpc endpoint in the order given. The split vector follows it exactly.
    if head_kind == "gpu":
        head_weights = _largest_remainder([d["usable"] for d in head_devs], weights[0]) if n_layers else [
            round(weights[0] * d["usable"] / max(1, head_cap)) for d in head_devs
        ]
        tensor_split = head_weights + weights[1:]
        n_gpu_layers = 999
    else:
        tensor_split = weights[1:]
        n_gpu_layers = sum(layers[1:]) if n_layers else 999

    participants = [
        {"node_id": head["node_id"], "role": "head", "bytes": int(shares[0]), "layers": layers[0] if n_layers else None}
    ]
    for p, b, lay in zip(chosen, shares[1:], layers[1:]):
        participants.append(
            {
                "node_id": p["node"]["node_id"],
                "hostname": p["node"].get("hostname"),
                "role": "rpc",
                "bytes": int(b),
                "layers": lay if n_layers else None,
                "device": p["device"],
                "kind": p["kind"],
            }
        )
    how = "spread over every helper (force_shard)" if force_shard else "fewest nodes that fit, largest first"
    return dict(
        base,
        feasible=True,
        mode="pooled",
        participants=participants,
        tensor_split=tensor_split,
        n_gpu_layers=n_gpu_layers,
        reason=(
            f"pooled over {len(participants)} nodes ({how}): {need} B needed vs {total_cap} B usable; "
            f"layer split {[pp['layers'] for pp in participants]} from measured free memory"
            + version_note
        ),
    )


# ---------------------------------------------------------------------------
# the organ
# ---------------------------------------------------------------------------


class Inference:
    """Runtime catalog + deployments + desired services. Shares the hub's
    sqlite connection and RLock like every other hub organ."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock, registry: Any) -> None:
        self.conn = conn
        self._lock = lock
        self.registry = registry
        self._cv = threading.Condition()
        self._version: Dict[str, int] = {}
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self.conn.commit()

    # -- runtime catalog ------------------------------------------------------

    @synchronized
    def record_runtimes(
        self,
        node_id: str,
        data: Optional[Dict[str, Any]],
        reach_host: Optional[str],
        addresses: Optional[List[str]] = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO node_runtimes (node_id, data_json, reach_host, addresses_json, at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(node_id) DO UPDATE SET data_json=excluded.data_json, reach_host=excluded.reach_host,"
            " addresses_json=excluded.addresses_json, at=excluded.at",
            (node_id, json.dumps(data or {}), reach_host, json.dumps(addresses or []), time.time()),
        )
        self.conn.commit()

    @staticmethod
    def device_classes(data: Optional[Dict[str, Any]]) -> List[str]:
        """Routing classes a node earns from what its runtimes reported."""
        out = set()
        for rt in (data or {}).get("runtimes") or []:
            out.add(f"runtime:{str(rt).lower()}")
        for m in (data or {}).get("models") or []:
            for alias in model_aliases(str(m.get("name") or "")):
                if alias:
                    out.add(f"model:{alias}")
        return sorted(out)

    @synchronized
    def _runtime_rows(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT r.node_id AS node_id, r.data_json AS data_json, r.reach_host AS reach_host,"
            " r.addresses_json AS addresses_json, n.hostname AS hostname, n.last_seen AS last_seen,"
            " n.profile_json AS profile_json FROM node_runtimes r JOIN nodes n ON n.node_id = r.node_id"
        ).fetchall()
        out = []
        for r in rows:
            try:
                data = json.loads(r["data_json"] or "{}")
            except ValueError:
                data = {}
            try:
                profile = json.loads(r["profile_json"] or "{}")
            except ValueError:
                profile = {}
            out.append(
                {
                    "node_id": r["node_id"],
                    "hostname": r["hostname"],
                    "last_seen": r["last_seen"],
                    "reach_host": r["reach_host"],
                    "addresses": json.loads(r["addresses_json"] or "[]"),
                    "data": data,
                    "ram_free_bytes": (profile.get("memory") or {}).get("free_bytes"),
                }
            )
        return out

    def online_runtime_nodes(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        now = now or time.time()
        return [r for r in self._runtime_rows() if r["last_seen"] and now - r["last_seen"] <= ONLINE_WINDOW_S]

    def catalog(self) -> List[Dict[str, Any]]:
        """Every model some online node can serve, merged across nodes."""
        merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for node in self.online_runtime_nodes():
            for m in node["data"].get("models") or []:
                key = (str(m.get("name")), str(m.get("kind")))
                entry = merged.setdefault(
                    key,
                    {
                        "name": key[0],
                        "kind": key[1],
                        "runtime": m.get("runtime"),
                        "size_bytes": m.get("size_bytes"),
                        "n_layers": m.get("n_layers"),
                        "architecture": m.get("architecture"),
                        "nodes": [],
                    },
                )
                entry["nodes"].append({"node_id": node["node_id"], "hostname": node["hostname"]})
        deployments = {d["model"]: d for d in self.list_deployments()}
        out = []
        for entry in merged.values():
            dep = deployments.get(entry["name"])
            if dep:
                entry["deployment"] = {"state": dep["state"], "endpoint": dep.get("endpoint")}
            out.append(entry)
        return sorted(out, key=lambda e: (e["kind"], e["name"].lower()))

    def resolve(self, requested: str, kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Find a catalog entry for a client-supplied model name."""
        want = normalize_model(requested).lower()
        entries = [e for e in self.catalog() if kind is None or e["kind"] == kind]
        for e in entries:
            if want in [a.lower() for a in model_aliases(e["name"])]:
                return e
        for e in entries:  # a unique prefix is forgiving without being ambiguous
            if e["name"].lower().startswith(want):
                return e
        return None

    # -- services: desired (hub) vs actual (agent) ---------------------------

    def _bump(self, node_id: str) -> None:
        with self._cv:
            self._version[node_id] = self._version.get(node_id, 0) + 1
            self._cv.notify_all()

    def version(self, node_id: str) -> int:
        with self._cv:
            return self._version.get(node_id, 0)

    def wait_for_change(self, node_id: str, known: int, timeout: float) -> None:
        with self._cv:
            if self._version.get(node_id, 0) == known and timeout > 0 and "__stop__" not in self._version:
                self._cv.wait(timeout)

    @synchronized
    def desired_for(self, node_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT spec_json FROM services WHERE node_id=? AND desired=1 ORDER BY created_at", (node_id,)
        ).fetchall()
        return [json.loads(r["spec_json"]) for r in rows]

    @synchronized
    def report(self, node_id: str, statuses: List[Dict[str, Any]]) -> None:
        now = time.time()
        for st in statuses or []:
            sid = str((st or {}).get("service_id") or "")
            if not sid:
                continue
            self.conn.execute(
                "UPDATE services SET status_json=?, updated_at=? WHERE service_id=? AND node_id=?",
                (json.dumps(st), now, sid, node_id),
            )
        self.conn.commit()

    @synchronized
    def list_services(self, deployment: Optional[str] = None) -> List[Dict[str, Any]]:
        if deployment:
            rows = self.conn.execute(
                "SELECT * FROM services WHERE deployment=? ORDER BY created_at", (deployment,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM services WHERE desired=1 ORDER BY created_at").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["spec"] = json.loads(d.pop("spec_json") or "{}")
            d["status"] = json.loads(d.pop("status_json") or "null")
            out.append(d)
        return out

    def _used_ports(self, node_id: str) -> set:
        rows = self.conn.execute(
            "SELECT spec_json FROM services WHERE node_id=? AND desired=1", (node_id,)
        ).fetchall()
        return {int(json.loads(r["spec_json"]).get("port", 0)) for r in rows}

    def _add_service(self, deployment: str, node_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        base = RPC_BASE_PORT if spec["kind"] == "llama_rpc" else SERVER_BASE_PORT
        used = self._used_ports(node_id)
        port = base
        while port in used:
            port += 1
        spec = dict(spec, service_id="svc-" + uuid.uuid4().hex[:12], port=port)
        now = time.time()
        self.conn.execute(
            "INSERT INTO services (service_id, deployment, node_id, kind, spec_json, desired, created_at, updated_at)"
            " VALUES (?,?,?,?,?,1,?,?)",
            (spec["service_id"], deployment, node_id, spec["kind"], json.dumps(spec), now, now),
        )
        return spec

    # -- deployments ----------------------------------------------------------

    @synchronized
    def list_deployments(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM deployments ORDER BY created_at").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["plan"] = json.loads(d.pop("plan_json") or "null")
            out.append(d)
        return out

    @synchronized
    def deployment(self, model: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM deployments WHERE model=?", (model,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["plan"] = json.loads(d.pop("plan_json") or "null")
        d["services"] = self.list_services(model)
        return d

    def _node_view(self, node: Dict[str, Any]) -> Dict[str, Any]:
        llama = node["data"].get("llama") or {}
        return {
            "node_id": node["node_id"],
            "hostname": node["hostname"],
            "devices": llama.get("devices") if "llama_server" in llama else None,
            "ram_free_bytes": node.get("ram_free_bytes"),
            "build": llama.get("build"),
        }

    def plan(self, model_name: str, force_shard: bool = False, ctx: int = DEFAULT_CTX) -> Dict[str, Any]:
        entry = self.resolve(model_name, kind="chat")
        if entry is None or entry.get("runtime") != "llama_cpp":
            return {"model": model_name, "feasible": False, "reason": "no online node offers this GGUF model"}
        nodes = self.online_runtime_nodes()
        holders = {n["node_id"] for n in entry["nodes"]}
        heads = [
            self._node_view(n)
            for n in nodes
            if n["node_id"] in holders and "llama_server" in (n["data"].get("llama") or {})
        ]
        helpers = [self._node_view(n) for n in nodes if "llama_rpc" in (n["data"].get("llama") or {})]
        plan = plan_llama(entry, heads, helpers, force_shard=force_shard, ctx=ctx)
        plan["model"] = entry["name"]
        return plan

    def _address_for(self, node: Dict[str, Any], peer: Optional[Dict[str, Any]]) -> str:
        """The address `peer` should use to reach `node` (None = the hub)."""
        if peer is not None and peer["node_id"] == node["node_id"]:
            return "127.0.0.1"
        addrs = [a for a in node.get("addresses") or [] if not _is_loopback(a)]
        reach = node.get("reach_host")
        if peer is None:
            # the hub reaches it the way it reached the hub
            return reach or (addrs[0] if addrs else "127.0.0.1")
        peer_addrs = peer.get("addresses") or []
        if any(_is_tailscale(a) for a in peer_addrs):
            ts = [a for a in addrs if _is_tailscale(a)]
            if ts:
                return ts[0]
        if reach and not _is_loopback(reach):
            return reach
        return addrs[0] if addrs else "127.0.0.1"

    @synchronized
    def deploy(self, model_name: str, force_shard: bool = False, ctx: int = DEFAULT_CTX) -> Dict[str, Any]:
        """Start serving a GGUF model. Idempotent: an existing live deployment
        is returned as-is; a failed or stopped one is replaced."""
        existing = self.deployment(model_name)
        if existing and existing["state"] in ("placing", "loading", "ready"):
            return existing
        plan = self.plan(model_name, force_shard=force_shard, ctx=ctx)
        model = plan.get("model") or model_name
        existing = self.deployment(model)
        if existing and existing["state"] in ("placing", "loading", "ready"):
            return existing
        now = time.time()
        state = "placing" if plan.get("feasible") else "failed"
        self.conn.execute(
            "INSERT INTO deployments (model, state, head_node_id, plan_json, endpoint, reason, created_at, updated_at, last_used)"
            " VALUES (?,?,?,?,NULL,?,?,?,?) ON CONFLICT(model) DO UPDATE SET state=excluded.state,"
            " head_node_id=excluded.head_node_id, plan_json=excluded.plan_json, endpoint=NULL,"
            " reason=excluded.reason, created_at=excluded.created_at, updated_at=excluded.updated_at,"
            " last_used=excluded.last_used",
            (model, state, (plan.get("head") or {}).get("node_id"), json.dumps(plan), plan.get("reason"), now, now, now),
        )
        self.conn.execute("UPDATE services SET desired=0 WHERE deployment=?", (model,))
        touched = set()
        if plan.get("feasible"):
            nodes = {n["node_id"]: n for n in self.online_runtime_nodes()}
            head = nodes[plan["head"]["node_id"]]
            for p in plan["participants"]:
                if p["role"] != "rpc":
                    continue
                helper = nodes[p["node_id"]]
                spec: Dict[str, Any] = {
                    "kind": "llama_rpc",
                    "bind": self._address_for(helper, head),
                    "cache": bool((helper["data"].get("llama") or {}).get("rpc_cache")),
                }
                if p.get("device") and p["device"] != "CPU":
                    spec["device"] = p["device"]
                elif p.get("device") == "CPU":
                    spec["device"] = "CPU"
                self._add_service(model, helper["node_id"], spec)
                touched.add(helper["node_id"])
            if plan["mode"] == "single":
                self._start_head(model, plan)
                self.conn.execute("UPDATE deployments SET state='loading' WHERE model=?", (model,))
                touched.add(plan["head"]["node_id"])
        self.conn.commit()
        for nid in touched:
            self._bump(nid)
        return self.deployment(model) or {"model": model, "state": state, "plan": plan}

    def _start_head(self, model: str, plan: Dict[str, Any]) -> None:
        nodes = {n["node_id"]: n for n in self.online_runtime_nodes()}
        head = nodes.get(plan["head"]["node_id"])
        if head is None:
            return
        rpc_endpoints = []
        for svc in self.list_services(model):
            if svc["kind"] == "llama_rpc" and svc["desired"]:
                rpc_endpoints.append(f"{svc['spec']['bind']}:{svc['spec']['port']}")
        spec = {
            "kind": "llama_server",
            "bind": self._address_for(head, None),
            "model": model,
            "alias": model,
            "ctx": int(plan.get("ctx") or DEFAULT_CTX),
            "n_gpu_layers": int(plan.get("n_gpu_layers") or 0),
            "rpc": rpc_endpoints,
        }
        if plan.get("tensor_split"):
            spec["tensor_split"] = plan["tensor_split"]
        self._add_service(model, head["node_id"], spec)

    @synchronized
    def undeploy(self, model: str, reason: str = "stopped by owner") -> bool:
        cur = self.conn.execute(
            "UPDATE deployments SET state='stopped', endpoint=NULL, reason=?, updated_at=? WHERE model=?",
            (reason, time.time(), model),
        )
        nodes = [
            r["node_id"]
            for r in self.conn.execute(
                "SELECT DISTINCT node_id FROM services WHERE deployment=? AND desired=1", (model,)
            ).fetchall()
        ]
        self.conn.execute("UPDATE services SET desired=0 WHERE deployment=?", (model,))
        self.conn.commit()
        for nid in nodes:
            self._bump(nid)
        return cur.rowcount > 0

    @synchronized
    def touch(self, model: str) -> None:
        self.conn.execute("UPDATE deployments SET last_used=? WHERE model=?", (time.time(), model))
        self.conn.commit()

    def _fail(self, model: str, reason: str) -> None:
        self.undeploy(model, reason)
        self.conn.execute("UPDATE deployments SET state='failed' WHERE model=?", (model,))
        self.conn.commit()

    @synchronized
    def tick(self, now: Optional[float] = None) -> None:
        """Advance every live deployment one step. Cheap; called on every
        service sync and by gateway waiters."""
        now = now or time.time()
        online = {n["node_id"] for n in self.online_runtime_nodes(now)}
        for dep in self.list_deployments():
            model, state = dep["model"], dep["state"]
            if state not in ("placing", "loading", "ready"):
                continue
            services = [s for s in self.list_services(model) if s["desired"]]
            for s in services:
                st = s["status"] or {}
                if s["node_id"] not in online:
                    self._fail(model, f"node {s['node_id'][:8]} went offline")
                    break
                if st.get("state") in ("failed", "parked"):
                    detail = st.get("error") or st.get("state")
                    tail = (st.get("log_tail") or "").strip().splitlines()[-3:]
                    self._fail(model, f"{s['kind']} on {s['node_id'][:8]} {st.get('state')}: {detail}"
                               + (f" | {' / '.join(tail)}" if tail else ""))
                    break
            else:
                rpc = [s for s in services if s["kind"] == "llama_rpc"]
                head = [s for s in services if s["kind"] == "llama_server"]
                if state == "placing" and all((s["status"] or {}).get("state") == "running" for s in rpc):
                    self._start_head(model, dep["plan"])
                    self.conn.execute(
                        "UPDATE deployments SET state='loading', updated_at=? WHERE model=?", (now, model)
                    )
                    self.conn.commit()
                    self._bump(dep["head_node_id"])
                elif state == "loading" and head and (head[0]["status"] or {}).get("state") == "running":
                    spec = head[0]["spec"]
                    host = spec["bind"]
                    shown = f"[{host}]" if ":" in host else host
                    self.conn.execute(
                        "UPDATE deployments SET state='ready', endpoint=?, updated_at=?, reason=? WHERE model=?",
                        (f"http://{shown}:{spec['port']}", now, dep.get("reason"), model),
                    )
                    self.conn.commit()
                elif state == "ready" and IDLE_UNLOAD_S > 0 and dep.get("last_used") and now - dep["last_used"] > IDLE_UNLOAD_S:
                    self.undeploy(model, f"idle for {int(now - dep['last_used'])} s; memory handed back to the hosts")

    def wait_ready(self, model: str, timeout: float) -> Optional[Dict[str, Any]]:
        deadline = time.time() + timeout
        while True:
            self.tick()
            dep = self.deployment(model)
            if dep is None or dep["state"] in ("ready", "failed", "stopped"):
                return dep
            remaining = deadline - time.time()
            if remaining <= 0:
                return dep
            with self._cv:
                self._cv.wait(min(remaining, 1.0))

    def notify(self) -> None:
        with self._cv:
            self._cv.notify_all()
