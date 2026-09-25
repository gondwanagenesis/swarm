"""Add-a-device surface: the owner page mints invites; joiners and the seed
kit are token-gated; the scripts are syntactically valid for their shells
and carry the hub address the owner actually used."""

import io
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

import pytest

from swarm.agent.join_scripts import render_posix, render_powershell
from swarm.hub.server import Hub

OWNER = "swo_join-owner"


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture()
def hub(monkeypatch):
    monkeypatch.setenv("SWARM_LLAMA_TAG", "b11190")  # never hit GitHub from tests
    h = Hub(host="127.0.0.1", port=0, secure=True, owner_key=OWNER)
    _, port = h.start_background()
    yield h, port
    h.stop()


def test_join_page_is_owner_only_and_mints_invites(hub):
    h, port = hub
    assert _get(port, "/join")[0] == 401
    code, page = _get(port, "/join", {"Authorization": f"Bearer {OWNER}"})
    assert code == 200
    text = page.decode()
    assert f"http://127.0.0.1:{port}/join.sh?token=swk_" in text
    assert "join.ps1?token=swk_" in text and "seed-kit.zip" in text
    labels = {t["label"] for t in h.enrollment.list_tokens()}
    assert {"join-page", "seed-kit"} <= labels


def test_joiners_refuse_bad_tokens_and_bake_the_hub(hub):
    h, port = hub
    assert _get(port, "/join.sh?token=swk_nope")[0] == 403
    token = h.enrollment.create()["token"]
    code, script = _get(port, f"/join.sh?token={token}&dedicated=1")
    assert code == 200
    text = script.decode()
    assert f'HUB="http://127.0.0.1:{port}"' in text and f'TOKEN="{token}"' in text
    assert 'DEDICATED="${DEDICATED:-1}"' in text and "--self-update" in text
    assert 'LLAMA_TAG="b11190"' in text, "every joiner installs the fleet's pinned llama.cpp build"
    code, ps = _get(port, f"/join.ps1?token={token}")
    assert code == 200 and f"$Token = '{token}'" in ps.decode()


def test_seed_kit_contains_a_working_joiner_set(hub):
    h, port = hub
    token = h.enrollment.create()["token"]
    code, blob = _get(port, f"/join/seed-kit.zip?token={token}")
    assert code == 200
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = set(zf.namelist())
    assert {
        "swarm-seed/swarm-agent.pyz", "swarm-seed/join-unix.sh", "swarm-seed/join-windows.ps1",
        "swarm-seed/JOIN-WINDOWS.cmd", "swarm-seed/JOIN-MAC.command", "swarm-seed/README.txt",
    } <= names
    agent = zipfile.ZipFile(io.BytesIO(zf.read("swarm-seed/swarm-agent.pyz")))
    cfg = json.loads(agent.read("swarm_config.json"))
    assert cfg["hub"] == f"http://127.0.0.1:{port}" and cfg["token"] == token
    assert "swarm_build.json" in agent.namelist()
    mode = zf.getinfo("swarm-seed/join-unix.sh").external_attr >> 16
    assert mode & 0o111, "shell joiner must be executable when unzipped"


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh here")
def test_posix_joiner_parses(tmp_path):
    script = tmp_path / "j.sh"
    script.write_bytes(render_posix("http://hub:8777", "swk_t", True, "http://seed:8788").encode())
    assert subprocess.run(["sh", "-n", str(script)]).returncode == 0


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell parser check runs on Windows")
def test_powershell_joiner_parses(tmp_path):
    script = tmp_path / "j.ps1"
    script.write_text(render_powershell("http://hub:8777", "swk_t"), encoding="utf-8")
    check = (
        "$e=$null;$t=$null;[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}',[ref]$t,[ref]$e)|Out-Null; exit $e.Count"
    )
    assert subprocess.run(["powershell", "-NoProfile", "-Command", check]).returncode == 0
