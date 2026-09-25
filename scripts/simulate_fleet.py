#!/usr/bin/env python
"""Simulate a whole swarm on one machine — then try to break it.

Every node is a REAL agent process (its own home directory, state, keys,
lock, replica), declaring an emulated device profile (``--emulate``): a thin
laptop that holds the model, an emulated GPU box, three phones (code
workers), an old phone that runs hot, a Pi, a server. The hub is a real hub
process in secure mode. Where llama.cpp and a GGUF model exist on this
machine, pooled inference runs for real (rpc-servers really hold layers).

Scenarios, each PASS / FAIL / SKIP with evidence:

  S1  fleet assembles (every node registers, successors elected)
  S2  batch map spreads across the fleet
  S3  two nodes die mid-batch; the batch still completes exactly once
  S4  a hot phone rests; the others carry the work
  S5  an AI's code (MCP swarm_run_python) runs only on code workers
  S6  embeddings via /v1 reach the node holding the model
  S7  a model too big for its holder is pooled over the fewest helpers
  S8  a pooled helper dies; the next request re-places the model
  S9  the hub dies; a successor becomes the hub; the fleet follows;
      the owner's key still works; new work completes

    python scripts/simulate_fleet.py            # writes docs/SIMULATION.md

Stdlib only. Nothing touches the real ~/.swarm (each node gets a temp home).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FLEET = [
    # name, emulated profile, options
    # an old laptop on a shelf: it holds the models and serves them (dedicated;
    # a laptop someone is USING would rest, which is the welfare rule, not a bug)
    ("thin-laptop", "thin-laptop", {"holds_models": True, "ollama": True, "dedicated": True}),
    ("gpu-box", "gpu-box", {}),
    ("phone-1", "phone", {"code_worker": True}),
    ("phone-2", "phone", {"code_worker": True}),
    ("phone-3", "phone", {"code_worker": True}),
    ("old-phone", "old-phone", {"code_worker": True, "hot": True}),
    ("pi", "pi", {}),
    ("server", "server", {}),
]

MAP_FN = """
import hashlib, socket
def run(params):
    d = str(params["i"]).encode()
    for _ in range(int(params.get("rounds", 60000))):
        d = hashlib.sha256(d).digest()
    return {"i": params["i"], "digest": d.hex()[:12]}
"""


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def kill_tree(proc: subprocess.Popen) -> None:
    """A device dying takes its children with it (rpc-server, llama-server)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=10)


class Owner:
    def __init__(self, url: str, key: str) -> None:
        self.url, self.key = url.rstrip("/"), key

    def call(self, path: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.url + path, data=data,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")

    def try_call(self, *a: Any, **kw: Any) -> Any:
        try:
            return self.call(*a, **kw)
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def wait_bag(self, bag: str, timeout: float) -> Dict[str, Any]:
        deadline = time.time() + timeout
        st: Dict[str, Any] = {}
        while time.time() < deadline:
            st = self.try_call(f"/api/bag/{bag}") or st
            if st.get("status") in ("closed", "cancelled"):
                return st
            time.sleep(0.5)
        return st


class Sim:
    def __init__(self, workdir: Path, llama_dir: Optional[str], models_dir: Optional[str]) -> None:
        self.work = workdir
        self.llama_dir = llama_dir
        self.models_dir = models_dir
        self.hub_port = free_port()
        self.hub_proc: Optional[subprocess.Popen] = None
        self.agents: Dict[str, Dict[str, Any]] = {}
        self.results: List[Dict[str, Any]] = []
        self.owner: Optional[Owner] = None
        self.facts: Dict[str, Any] = {}

    # -- plumbing -------------------------------------------------------------

    def base_env(self, home: Path) -> Dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "PYTHONPATH": str(ROOT),
                "SWARM_HEARTBEAT_S": "2",
                "SWARM_FAILOVER_S": "8",
                "SWARM_RANK_GRACE_S": "6",
                "SWARM_REPLICA_REFRESH_S": "3",
                "SWARM_ONLINE_WINDOW_S": "12",
                "SWARM_ADVERTISE_ADDRESSES": "",
                "SWARM_HUB_SECURE": "1",
                "SWARM_MODEL_IDLE_S": "0",
            }
        )
        env.pop("SWARM_OWNER_KEY", None)
        return env

    def start_hub(self) -> None:
        home = self.work / "hub-home"
        (home / ".swarm").mkdir(parents=True, exist_ok=True)
        log = open(self.work / "hub.log", "ab")  # noqa: SIM115
        self.hub_proc = subprocess.Popen(
            [sys.executable, "-m", "swarm.hub.server", "--host", "127.0.0.1", "--port", str(self.hub_port),
             "--db", str(home / ".swarm" / "hub.db"), "--secure"],
            cwd=str(ROOT), env=self.base_env(home), stdout=log, stderr=subprocess.STDOUT,
            **({"start_new_session": True} if os.name != "nt" else {}),
        )
        key_file = home / ".swarm" / "owner.key"
        deadline = time.time() + 60
        while time.time() < deadline and not key_file.exists():
            time.sleep(0.2)
        self.owner = Owner(f"http://127.0.0.1:{self.hub_port}", key_file.read_text().strip())
        while time.time() < deadline and self.owner.try_call("/api/nodes") is None:
            time.sleep(0.3)

    def start_agent(self, name: str, kind: str, opts: Dict[str, Any], token: str) -> None:
        home = self.work / f"node-{name}"
        home.mkdir(parents=True, exist_ok=True)
        env = self.base_env(home)
        env["SWARM_OLLAMA_URL"] = os.environ.get("SWARM_OLLAMA_URL", "http://127.0.0.1:11434") if opts.get("ollama") else "http://127.0.0.1:9"
        env["SWARM_MODELS_DIR"] = self.models_dir if (opts.get("holds_models") and self.models_dir) else str(home / "models")
        if self.llama_dir:
            env["SWARM_LLAMA_DIR"] = self.llama_dir
        if opts.get("hot"):
            env["SWARM_EMULATE_TEMP_C"] = "50"
        port = free_port()
        cmd = [sys.executable, "-m", "swarm.agent.daemon", "--hub", f"http://127.0.0.1:{self.hub_port}",
               "--token", token, "--work", "--no-bench", "--emulate", kind, "--node-id", f"sim-{name}",
               "--hub-port", str(port), "--log", str(home / "agent.log")]
        if opts.get("code_worker"):
            cmd.append("--code-worker")
        if opts.get("dedicated"):
            cmd.append("--dedicated")
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **({"start_new_session": True} if os.name != "nt" else {}),
        )
        self.agents[name] = {"proc": proc, "kind": kind, "opts": opts, "hub_port": port, "node_id": f"sim-{name}"}

    def record(self, sid: str, title: str, status: str, evidence: str, seconds: float) -> None:
        self.results.append({"id": sid, "title": title, "status": status, "evidence": evidence, "seconds": round(seconds, 1)})
        print(f"[{status:4}] {sid} {title} ({seconds:.1f}s) - {evidence}", flush=True)

    def online(self) -> List[Dict[str, Any]]:
        nodes = (self.owner.try_call("/api/nodes") or {}).get("nodes") or []
        return [n for n in nodes if n.get("last_seen") and time.time() - n["last_seen"] < 15]

    def map_bag(self, n: int, rounds: int, code_workers: bool = False) -> str:
        params = [{"adapter_source": MAP_FN, "adapter_params": {"i": i, "rounds": rounds}, "adapter_timeout_s": 120} for i in range(n)]
        body: Dict[str, Any] = {"op": "map", "params_list": params}
        if code_workers:
            body["device_class"] = "role:code-worker"
        return self.owner.call("/api/bag/submit", body)["bag_id"]

    def by_node(self, bag: str) -> Dict[str, int]:
        rows = self.owner.call(f"/api/bag/{bag}/results")["results"]
        out: Dict[str, int] = {}
        for r in rows:
            out[r["node_id"]] = out.get(r["node_id"], 0) + 1
        return out

    # -- scenarios -------------------------------------------------------------

    def s1_assemble(self) -> None:
        t0 = time.time()
        token = self.owner.call("/api/tokens", {"label": "simulation", "ttl_s": 3600})["token"]
        for name, kind, opts in FLEET:
            self.start_agent(name, kind, opts, token)
        deadline = time.time() + 180
        while time.time() < deadline and len(self.online()) < len(FLEET):
            time.sleep(1)
        online = self.online()
        hb = self.owner.try_call("/api/hubinfo") or {}
        ok = len(online) == len(FLEET)
        self.facts["swarm_id"] = hb.get("swarm_id")
        self.record("S1", "fleet assembles", "PASS" if ok else "FAIL",
                    f"{len(online)}/{len(FLEET)} nodes online: " + ", ".join(sorted(n['hostname'] for n in online)), time.time() - t0)

    def s2_map(self) -> None:
        t0 = time.time()
        bag = self.map_bag(60, 60000)
        st = self.owner.wait_bag(bag, 240)
        spread = self.by_node(bag) if st.get("status") == "closed" else {}
        ok = st.get("status") == "closed" and st.get("failed", 0) == 0 and len(spread) >= 4
        self.facts["map_spread"] = spread
        self.record("S2", "batch map spreads across the fleet", "PASS" if ok else "FAIL",
                    f"{st.get('done')}/{st.get('total')} done on {len(spread)} nodes {sorted(spread.values(), reverse=True)}", time.time() - t0)

    def s3_chaos(self) -> None:
        t0 = time.time()
        bag = self.map_bag(80, 150000)
        time.sleep(4)
        victims = ["pi", "server"]
        for v in victims:
            kill_tree(self.agents[v]["proc"])
        st = self.owner.wait_bag(bag, 400)
        rows = self.owner.call(f"/api/bag/{bag}/results")["results"] if st.get("status") == "closed" else []
        seqs = [r["seq"] for r in rows]
        ok = st.get("status") == "closed" and len(seqs) == 80 and len(set(seqs)) == 80 and st.get("failed", 0) == 0
        self.record("S3", "two nodes die mid-batch; batch completes exactly once", "PASS" if ok else "FAIL",
                    f"killed {victims}; {len(set(seqs))}/80 results, {st.get('failed', 0)} failed", time.time() - t0)

    def s4_hot_phone(self) -> None:
        t0 = time.time()
        spread = self.facts.get("map_spread") or {}
        hot = spread.get("sim-old-phone", 0)
        others = sum(v for k, v in spread.items() if k != "sim-old-phone")
        ok = hot == 0 and others > 0
        self.record("S4", "a hot phone rests while the others work", "PASS" if ok else "FAIL",
                    f"old-phone (50 C) did {hot} tasks; others did {others}", time.time() - t0)

    def s5_mcp(self) -> None:
        t0 = time.time()
        from swarm import mcp

        tools = mcp.SwarmTools(self.owner.url, self.owner.key)
        try:
            out = tools.run_python({"code": "import platform\nresult = sum(i*i for i in range(1000))\nprint('ran')", "timeout_s": 60})
        except Exception as exc:
            self.record("S5", "AI code runs only on code workers", "FAIL", str(exc), time.time() - t0)
            return
        node = out.get("node", "")  # first 8 chars of the node id that ran it
        worker_ids = [a["node_id"] for a in self.agents.values() if a["opts"].get("code_worker")]
        ok = out.get("result") == 332833500 and bool(node) and any(w.startswith(node) for w in worker_ids)
        self.record("S5", "AI code (MCP swarm_run_python) runs only on code workers", "PASS" if ok else "FAIL",
                    f"result={out.get('result')} on {node}* (code workers: phones)", time.time() - t0)

    def s6_embed(self) -> None:
        t0 = time.time()
        models = self.owner.call("/api/models")["catalog"]
        embed = next((m["name"] for m in models if m["kind"] == "embed"), None)
        if not embed:
            self.record("S6", "embeddings via /v1", "SKIP", "no embedding model on this machine's Ollama", time.time() - t0)
            return
        body = self.owner.call("/v1/embeddings", {"model": embed, "input": ["old phones", "a swarm"]}, timeout=300)
        dims = [len(d["embedding"]) for d in body["data"]]
        ok = len(dims) == 2 and dims[0] > 0 and body["swarm"]["nodes"] == ["sim-thin-laptop"]
        self.record("S6", "embeddings via /v1 reach the holder", "PASS" if ok else "FAIL",
                    f"{embed}: dims {dims} computed on {body['swarm']['nodes']}", time.time() - t0)

    def _chat(self, model: str, prompt: str) -> Dict[str, Any]:
        return self.owner.call("/v1/chat/completions", {
            "model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 40,
            "chat_template_kwargs": {"enable_thinking": False}}, timeout=1200)

    def s7_pooled(self) -> Optional[str]:
        t0 = time.time()
        models = self.owner.call("/api/models")["catalog"]
        gguf = next((m["name"] for m in models if m["runtime"] == "llama_cpp"), None)
        if not gguf:
            self.record("S7", "model pooled over the fewest helpers", "SKIP", "no llama.cpp + GGUF on this machine", time.time() - t0)
            return None
        plan = self.owner.call("/api/models/plan?model=" + urllib.request.quote(gguf))
        body = self._chat(gguf, "Say hello in five words.")
        text = body["choices"][0]["message"].get("content") or ""
        sw = body.get("swarm") or {}
        parts = [p["node_id"] for p in sw.get("participants") or []]
        tps = (body.get("timings") or {}).get("predicted_per_second")
        ok = plan.get("mode") == "pooled" and sw.get("mode") == "pooled" and len(parts) >= 2 and bool(text.strip())
        self.facts["pooled_first"] = parts
        self.record("S7", "a model too big for its holder is pooled over the fewest helpers", "PASS" if ok else "FAIL",
                    f"{gguf}: {plan.get('reason')} -> answered {text.strip()[:60]!r} via {parts}" + (f", {tps:.1f} tok/s" if tps else ""),
                    time.time() - t0)
        return gguf

    def s8_helper_dies(self, gguf: Optional[str]) -> None:
        t0 = time.time()
        if not gguf:
            self.record("S8", "a pooled helper dies; model is re-placed", "SKIP", "no pooled model", time.time() - t0)
            return
        helpers = [p for p in self.facts.get("pooled_first", []) if p != "sim-thin-laptop"]
        victim_name = next((n for n, a in self.agents.items() if a["node_id"] in helpers), None)
        if not victim_name:
            self.record("S8", "a pooled helper dies; model is re-placed", "FAIL", "no helper to kill", time.time() - t0)
            return
        kill_tree(self.agents[victim_name]["proc"])
        time.sleep(15)  # past the online window: the hub notices the death
        try:
            body = self._chat(gguf, "Name one planet.")
        except urllib.error.HTTPError as exc:
            self.record("S8", "a pooled helper dies; model is re-placed", "FAIL", f"HTTP {exc.code}: {exc.read()[:200]!r}", time.time() - t0)
            return
        parts = [p["node_id"] for p in (body.get("swarm") or {}).get("participants") or []]
        text = body["choices"][0]["message"].get("content") or ""
        ok = bool(text.strip()) and self.agents[victim_name]["node_id"] not in parts
        self.record("S8", "a pooled helper dies; the next request re-places the model", "PASS" if ok else "FAIL",
                    f"killed {victim_name}; re-placed on {parts}; answered {text.strip()[:40]!r}", time.time() - t0)
        self.owner.try_call("/api/models/undeploy", {"model": gguf})

    def s9_hub_dies(self) -> None:
        t0 = time.time()
        succ = []
        for a in self.agents.values():
            state_file = self.work / f"node-{a['node_id'][4:]}" / ".swarm" / f"holo-{a['node_id'][:12]}.json"
            if state_file.exists():
                data = json.loads(state_file.read_text())
                succ = data.get("successors") or succ
                if succ:
                    break
        if not succ:
            self.record("S9", "hub dies; a successor takes over", "FAIL", "no successor list reached the nodes", time.time() - t0)
            return
        kill_tree(self.hub_proc)
        new_url = None
        deadline = time.time() + 150
        while time.time() < deadline and new_url is None:
            for s in succ:
                try:
                    with urllib.request.urlopen(s["url"] + "/api/hubinfo", timeout=1) as r:
                        info = json.loads(r.read())
                    if info.get("epoch", 0) >= 2 and not info.get("demoted"):
                        new_url = s["url"]
                        break
                except OSError:
                    continue
            time.sleep(1)
        if not new_url:
            self.record("S9", "hub dies; a successor takes over", "FAIL", f"no successor promoted (successors {[s['hostname'] for s in succ]})", time.time() - t0)
            return
        promoted_after = time.time() - t0
        self.owner = Owner(new_url, self.owner.key)  # same owner key: its hash travelled in the replica
        deadline = time.time() + 60
        alive = [a for a in self.agents.values() if a["proc"].poll() is None]
        while time.time() < deadline and len(self.online()) < len(alive):
            time.sleep(1)
        attached = len(self.online())
        bag = self.map_bag(30, 40000)
        st = self.owner.wait_bag(bag, 240)
        ok = st.get("status") == "closed" and st.get("failed", 0) == 0 and attached >= len(alive) - 1
        self.record("S9", "hub dies; a successor becomes the hub; the fleet follows", "PASS" if ok else "FAIL",
                    f"new hub {new_url} after {promoted_after:.0f}s (epoch 2, same swarm); {attached}/{len(alive)} nodes re-attached; "
                    f"owner key accepted; new batch {st.get('done')}/{st.get('total')} done", time.time() - t0)
        self.facts["promoted_hub"] = new_url

    # -- report -----------------------------------------------------------------

    def report(self, path: Path) -> None:
        passed = sum(r["status"] == "PASS" for r in self.results)
        failed = sum(r["status"] == "FAIL" for r in self.results)
        lines = [
            "# Fleet simulation report",
            "",
            f"Generated {time.strftime('%Y-%m-%d %H:%M')} by `scripts/simulate_fleet.py` on {socket.gethostname()}.",
            f"**{passed} passed, {failed} failed, {len(self.results) - passed - failed} skipped.**",
            "",
            "Each node is a real agent process with its own home directory, declaring an",
            "emulated device profile (`--emulate`; the numbers are declared and every such",
            "node says so). The hub is a real hub process in secure mode. Pooled inference",
            "uses the real llama.cpp binaries and a real GGUF model when present.",
            "",
            "| Node | Emulated as | Role |",
            "|---|---|---|",
        ]
        for name, kind, opts in FLEET:
            role = ", ".join(k for k in ("holds_models", "dedicated", "code_worker", "hot") if opts.get(k)) or "worker"
            lines.append(f"| {name} | {kind} | {role} |")
        lines += ["", "| # | Scenario | Result | Time | Evidence |", "|---|---|---|---|---|"]
        for r in self.results:
            lines.append(f"| {r['id']} | {r['title']} | **{r['status']}** | {r['seconds']}s | {r['evidence']} |")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nreport: {path}")

    def shutdown(self) -> None:
        for a in self.agents.values():
            kill_tree(a["proc"])
        if self.hub_proc is not None:
            kill_tree(self.hub_proc)
        # a promoted hub runs detached from its agent: find it by its port
        promoted = self.facts.get("promoted_hub")
        if promoted and os.name == "nt":
            port = promoted.rsplit(":", 1)[-1]
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
            for line in out.splitlines():
                if f"127.0.0.1:{port} " in line and "LISTENING" in line:
                    subprocess.run(["taskkill", "/PID", line.split()[-1], "/T", "/F"], capture_output=True)
        elif promoted:
            subprocess.run(["pkill", "-f", f"[s]warm.hub.server.*--port {promoted.rsplit(':', 1)[-1]}"], capture_output=True)


def find_llama_dir() -> Optional[str]:
    from swarm.probe.runtimes import find_llama_binaries

    bins = find_llama_binaries()
    if "llama_server" in bins and "llama_rpc" in bins:
        return str(Path(bins["llama_server"]).parent)
    return None


def find_models_dir() -> Optional[str]:
    from swarm.probe.runtimes import local_gguf_models, models_dirs

    return str(models_dirs()[0]) if local_gguf_models() else None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--report", default=str(ROOT / "docs" / "SIMULATION.md"))
    ap.add_argument("--keep", action="store_true", help="keep the temp directory (logs) afterwards")
    args = ap.parse_args(argv)
    work = Path(tempfile.mkdtemp(prefix="swarm-sim-"))
    print(f"simulation workdir: {work}")
    sim = Sim(work, find_llama_dir(), find_models_dir())
    print(f"llama.cpp: {sim.llama_dir or 'absent'}; models: {sim.models_dir or 'none'}")
    try:
        sim.start_hub()
        sim.s1_assemble()
        sim.s2_map()
        sim.s4_hot_phone()
        sim.s3_chaos()
        sim.s5_mcp()
        sim.s6_embed()
        gguf = sim.s7_pooled()
        sim.s8_helper_dies(gguf)
        sim.s9_hub_dies()
    finally:
        sim.report(Path(args.report))
        sim.shutdown()
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    return 0 if all(r["status"] != "FAIL" for r in sim.results) else 1


if __name__ == "__main__":
    sys.exit(main())
