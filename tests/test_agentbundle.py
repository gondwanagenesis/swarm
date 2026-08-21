import subprocess
import sys
import zipfile
from io import BytesIO

from swarm.hub.agentbundle import build_agent_pyz


def test_pyz_contains_only_stdlib_packages():
    payload = build_agent_pyz()
    with zipfile.ZipFile(BytesIO(payload)) as zf:
        names = zf.namelist()
    assert "__main__.py" in names
    packaged = {n.split("/")[0] for n in names if "/" in n}
    assert packaged == {"swarm"}
    subpkgs = {n.split("/")[1] for n in names if n.startswith("swarm/") and n.count("/") >= 2}
    assert "hub" not in subpkgs
    assert "integrator" not in subpkgs
    assert "core" in subpkgs and "agent" in subpkgs and "probe" in subpkgs


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
