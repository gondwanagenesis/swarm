import subprocess
import sys
import zipfile
from io import BytesIO

import pytest

from swarm.hub.agentbundle import build_agent_pyz


def test_pyz_contains_only_stdlib_packages():
    payload = build_agent_pyz()
    with zipfile.ZipFile(BytesIO(payload)) as zf:
        names = zf.namelist()
    assert "__main__.py" in names
    packaged = {n.split("/")[0] for n in names if "/" in n}
    assert packaged == {"swarm"}
    subpkgs = {n.split("/")[1] for n in names if n.startswith("swarm/") and n.count("/") >= 2}
    # Holographic: every agent file carries the whole swarm, hub included, so
    # any node can become the hub. The price is that ALL of it stays stdlib
    # (the CI import gate now covers the whole package).
    assert {"core", "agent", "probe", "hub", "integrator"} <= subpkgs
    assert "swarm/cli.py" in names and "swarm/mcp.py" in names
    assert "swarm_build.json" in names


def test_pyz_runs_help(tmp_path):
    out = tmp_path / "swarm-agent.pyz"
    build_agent_pyz(output=out)
    proc = subprocess.run(
        [sys.executable, str(out), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0
    assert "--hub" in proc.stdout


@pytest.mark.slow
def test_the_agent_file_can_run_a_hub(tmp_path):
    """Any node can become the hub: the agent file itself serves one."""
    import json
    import socket
    import time
    import urllib.request

    out = tmp_path / "swarm-agent.pyz"
    build_agent_pyz(output=out)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    proc = subprocess.Popen(
        [sys.executable, str(out), "--run-hub", "--host", "127.0.0.1", "--port", str(port),
         "--db", str(tmp_path / "hub.db")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(tmp_path),
    )
    try:
        info = None
        deadline = time.time() + 60
        while time.time() < deadline and info is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/hubinfo", timeout=2) as r:
                    info = json.loads(r.read())
            except OSError:
                time.sleep(0.5)
        assert info and info["ok"] and info["swarm_id"].startswith("swm_")
        # and it can hand out agent files even though it has no source tree
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/agent.pyz", timeout=10) as r:
            assert r.read()[:2] == b"PK"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
