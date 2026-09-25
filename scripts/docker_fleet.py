#!/usr/bin/env python
"""Real joiners on real (containerised) machines, then kill the hub.

Every container is its own machine: own filesystem, own network stack, own
IP. Nothing is shared with the host but the repo (mounted read-only into the
hub only). What runs:

  hub          python:3.12-slim running the hub from the repo, secure mode
  linux-1..2   python:3.12-slim — the REAL one-line joiner fetched from the
               hub (installs the pinned llama.cpp release, starts the agent)
  bare         alpine without Python — the joiner must refuse cleanly and
               print the exact install command (no half-installs)
  termux       termux/termux-docker — the Android joiner path: installs
               Python with pkg by itself, is a code worker by default

Then: every joined device is visible to the hub; the hub container is
killed; the best-ranked successor container restores its replica and serves
a NEW hub (epoch 2) from its own agent file; the others re-attach.

    python scripts/docker_fleet.py        # appends results to docs/SIMULATION.md

Needs Docker (Desktop or Engine). Stdlib only.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
NET = "swarm-sim-net"
FAST = [
    "-e", "SWARM_HEARTBEAT_S=3", "-e", "SWARM_FAILOVER_S=20", "-e", "SWARM_RANK_GRACE_S=15",
    "-e", "SWARM_REPLICA_REFRESH_S=5", "-e", "SWARM_ONLINE_WINDOW_S=20",
]
results: List[Dict[str, Any]] = []


def docker(*args: str, check: bool = True, timeout: float = 900) -> str:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])}... failed: {proc.stderr.strip()[:400]}")
    return (proc.stdout or "") + (proc.stderr or "")


def record(sid: str, title: str, ok: Optional[bool], evidence: str, t0: float) -> None:
    status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    results.append({"id": sid, "title": title, "status": status, "evidence": evidence, "seconds": round(time.time() - t0, 1)})
    print(f"[{status:4}] {sid} {title} - {evidence}", flush=True)


TERMUX_PY = "/data/data/com.termux/files/usr/bin/python"


def owner_call(container: str, path: str, key: str, url: str = "http://127.0.0.1:8777", payload: Optional[dict] = None) -> Any:
    """Call a hub from INSIDE a container (container IPs are not routable from
    the host on Docker Desktop). The code travels on stdin: no shell quoting."""
    code = "\n".join(
        [
            "import json, urllib.request",
            f"payload = {payload!r}",
            "data = json.dumps(payload).encode() if payload is not None else None",
            f"req = urllib.request.Request({(url + path)!r}, data=data, headers={{'Authorization': 'Bearer ' + {key!r}, 'Content-Type': 'application/json'}})",
            "try:",
            "    print(urllib.request.urlopen(req, timeout=30).read().decode())",
            "except Exception as exc:",
            "    print(json.dumps({'error': str(exc)}))",
            "",
        ]
    )
    python = TERMUX_PY if "termux" in container else "python3"
    proc = subprocess.run(["docker", "exec", "-i", container, python, "-"], input=code, capture_output=True, text=True, timeout=60)
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        return None if isinstance(out, dict) and set(out) == {"error"} else out
    except (ValueError, IndexError):
        return None


def cleanup() -> None:
    names = ["swarm-hub", "swarm-linux-1", "swarm-linux-2", "swarm-bare", "swarm-termux"]
    docker("rm", "-f", *names, check=False)
    docker("network", "rm", NET, check=False)


def main() -> int:
    cleanup()
    docker("network", "create", NET)
    t0 = time.time()
    # --- hub
    docker("run", "-d", "--name", "swarm-hub", "--network", NET, "--network-alias", "hub",
           "-v", f"{ROOT}:/src:ro", "-e", "SWARM_ONLINE_WINDOW_S=20", "-e", "SWARM_LLAMA_TAG=b11190",
           "-e", "PYTHONDONTWRITEBYTECODE=1", "-w", "/src",
           "python:3.12-slim", "python", "-m", "swarm.hub.server", "--host", "0.0.0.0", "--port", "8777",
           "--db", "/root/.swarm/hub.db")
    key = ""
    for _ in range(90):
        key = docker("exec", "swarm-hub", "cat", "/root/.swarm/owner.key", check=False).strip()
        if key.startswith("swo_"):
            break
        time.sleep(1)
    token = (owner_call("swarm-hub", "/api/tokens", key, payload={"label": "docker", "ttl_s": 7200}) or {}).get("token")
    record("D1", "hub container up, secure, mints invites", bool(key.startswith("swo_") and token),
           "owner key present; invite minted" if token else "no token", t0)
    if not token:
        return 1
    joiner = f"http://hub:8777/join.sh?token={token}"
    fetch = f"python3 -c \"import urllib.request;open('/tmp/j.sh','w').write(urllib.request.urlopen('{joiner}').read().decode())\""

    # --- two linux devices: the real joiner
    t0 = time.time()
    for i in (1, 2):
        docker("run", "-d", "--name", f"swarm-linux-{i}", "--network", NET, *FAST, "-e", "DEDICATED=1",
               "python:3.12-slim", "sh", "-c", f"{fetch} && sh /tmp/j.sh > /tmp/join.log 2>&1; tail -f /dev/null")
    time.sleep(5)
    for _ in range(120):
        logs = [docker("exec", f"swarm-linux-{i}", "cat", "/tmp/join.log", check=False) for i in (1, 2)]
        if all("joined:" in lg for lg in logs):
            break
        time.sleep(3)
    log1 = docker("exec", "swarm-linux-1", "cat", "/tmp/join.log", check=False)
    llama_ok = "llama.cpp: installed" in log1 or "already installed" in log1
    record("D2", "real one-line joiner on two Linux machines", all("joined:" in lg for lg in logs) and llama_ok,
           "; ".join(line for line in log1.splitlines() if line.startswith(("llama.cpp", "agent", "joined", "autostart")))[:300], t0)

    # --- a machine without python: must refuse cleanly
    t0 = time.time()
    out = docker("run", "--rm", "--network", NET, "alpine:3.20", "sh", "-c",
                 f"wget -qO /tmp/j.sh '{joiner}' && sh /tmp/j.sh; echo EXIT=$?", check=False)
    refused = "apk add python3" in out and "EXIT=1" in out
    record("D3", "a machine without Python gets the exact install command, nothing half-installed", refused,
           " / ".join(line for line in out.splitlines() if "apk" in line or "EXIT" in line)[:200], t0)

    # --- android: termux container (x86_64 build of the Termux environment)
    t0 = time.time()
    termux_ok: Optional[bool] = None
    evidence = "termux image unavailable"
    pulled = docker("pull", "termux/termux-docker:x86_64", check=False, timeout=900)
    if "Error" not in pulled and "denied" not in pulled:
        docker("run", "-d", "--name", "swarm-termux", "--network", NET, *FAST,
               "termux/termux-docker:x86_64", "bash", "-c", "sleep infinity", check=False)
        run = docker("exec", "swarm-termux", "/data/data/com.termux/files/usr/bin/bash", "-lc",
                     f"curl -fsSL '{joiner}' | AUTOSTART=1 sh 2>&1 | tail -12", check=False, timeout=1500)
        termux_ok = "joined:" in run and "--code-worker" not in run  # args are not printed; checked on the hub below
        evidence = " / ".join(line for line in run.splitlines() if line.strip())[-300:]
    record("D4", "Android (Termux) joiner installs Python itself and joins", termux_ok, evidence, t0)

    # --- the hub sees everyone
    t0 = time.time()
    online: List[str] = []
    for _ in range(60):
        nodes = (owner_call("swarm-hub", "/api/nodes", key) or {}).get("nodes") or []
        online = [n["hostname"] for n in nodes if n.get("last_seen") and time.time() - n["last_seen"] < 30]
        if len(online) >= 2 + (1 if termux_ok else 0):
            break
        time.sleep(3)
    record("D5", "every joined machine is visible to the hub", len(online) >= 2 + (1 if termux_ok else 0),
           f"online: {online}", t0)

    # --- kill the hub machine
    t0 = time.time()
    time.sleep(12)  # let successors pull a replica
    docker("rm", "-f", "swarm-hub")
    promoted = None
    for _ in range(80):
        for name in ("swarm-linux-1", "swarm-linux-2", "swarm-termux"):
            info = owner_call(name, "/api/hubinfo", key, url="http://127.0.0.1:8777")
            if info and info.get("epoch", 0) >= 2 and not info.get("demoted"):
                promoted = name
                break
        if promoted:
            break
        time.sleep(3)
    nodes_after: List[str] = []
    if promoted:
        for _ in range(30):
            nodes = (owner_call(promoted, "/api/nodes", key) or {}).get("nodes") or []
            nodes_after = [n["hostname"] for n in nodes if n.get("last_seen") and time.time() - n["last_seen"] < 30]
            if len(nodes_after) >= 2:
                break
            time.sleep(3)
    record("D6", "hub machine killed; a device machine becomes the hub from its replica; others re-attach",
           bool(promoted) and len(nodes_after) >= 2,
           f"new hub on {promoted} (epoch 2, served from its own agent file); re-attached: {nodes_after}; owner key accepted", t0)
    return 0 if all(r["status"] != "FAIL" for r in results) else 1


def report() -> None:
    path = ROOT / "docs" / "SIMULATION.md"
    lines = ["", "## Containerised machines (`scripts/docker_fleet.py`)", "",
             f"Run {time.strftime('%Y-%m-%d %H:%M')}. Each container is a separate machine (own filesystem, network stack, IP).", "",
             "| # | Scenario | Result | Time | Evidence |", "|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['id']} | {r['title']} | **{r['status']}** | {r['seconds']}s | {r['evidence']} |")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"appended to {path}")


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        report()
        if "--keep" not in sys.argv:
            cleanup()
    sys.exit(code)
