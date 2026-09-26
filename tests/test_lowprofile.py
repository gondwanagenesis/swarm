"""Low profile: a secure hub is invisible to strangers, and a device opens
only with the swarm's access code."""

import json
import subprocess
import sys
import urllib.error
import urllib.request

from swarm.hub.lowprofile import check_code, derive_access_code, make_verifier, peer_header, peer_ok
from swarm.hub.server import Hub

OWNER = "swo_lowprofile-owner-key"


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_a_secure_hub_is_dark_to_strangers():
    hub = Hub(host="127.0.0.1", port=0, secure=True, owner_key=OWNER)
    _, port = hub.start_background()
    try:
        for path in ("/", "/join", "/api/hubinfo", "/agent.pyz", "/worker", "/api/me", "/api/nodes",
                     "/invite", "/bundle.pyz?token=nope", "/v2/anything", "/.env"):
            code, headers, body = _get(port, path)
            assert code == 404 and body == b"Not Found", (path, code, body[:80])
            blob = (json.dumps(headers) + body.decode("utf-8", "replace")).lower()
            assert "swarm" not in blob and "swm_" not in blob, path
        code, headers, _ = _get(port, "/api/ping")
        assert code == 200 and "swarm" not in headers.get("Server", "").lower()
    finally:
        hub.stop()


def test_credentialed_callers_still_get_through():
    hub = Hub(host="127.0.0.1", port=0, secure=True, owner_key=OWNER)
    _, port = hub.start_background()
    try:
        assert _get(port, "/api/hubinfo", {"Authorization": f"Bearer {OWNER}"})[0] == 200
        peer = {"X-Swarm-Peer": peer_header(hub.auth.owner_key_hash)}
        code, _, body = _get(port, "/api/hubinfo", peer)
        assert code == 200 and json.loads(body)["swarm_id"] == hub.holo.swarm_id
        stale = {"X-Swarm-Peer": peer_header(hub.auth.owner_key_hash, now=0)}
        assert _get(port, "/api/hubinfo", stale)[0] == 404, "an old signature is replay, not proof"
        token = hub.enrollment.create()["token"]
        assert _get(port, f"/agent.pyz?token={token}")[0] == 200
        node_key = hub.auth.issue_node_key("n1")
        code, _, body = _get(port, "/api/me", {"X-Swarm-Node": "n1", "X-Swarm-Node-Key": node_key})
        assert code == 200 and json.loads(body)["node_id"] == "n1"
    finally:
        hub.stop()


def test_access_code_is_derived_from_the_owner_key_and_stored_only_as_a_verifier():
    code = derive_access_code(OWNER)
    assert len(code) == 9 and code[4] == "-" and code == derive_access_code(OWNER)
    assert code != derive_access_code("swo_someone-else")
    verifier = make_verifier(code, iterations=1000)
    assert code not in json.dumps(verifier)
    assert check_code(code, verifier) and check_code(code.lower().replace("-", " "), verifier)
    assert not check_code("AAAA-AAAA", verifier)
    assert peer_ok({"X-Swarm-Peer": peer_header("h")}, "h") and not peer_ok({"X-Swarm-Peer": peer_header("h")}, "x")


def test_bundles_carry_the_verifier_and_the_join_page_shows_the_code():
    hub = Hub(host="127.0.0.1", port=0, secure=True, owner_key=OWNER)
    _, port = hub.start_background()
    try:
        code, _, page = _get(port, "/join", {"Authorization": f"Bearer {OWNER}"})
        assert code == 200 and derive_access_code(OWNER) in page.decode()
        cfg = hub.bundle_config("http://h", "swk_t")
        assert check_code(derive_access_code(OWNER), cfg["access"])
    finally:
        hub.stop()


def test_device_status_opens_only_with_the_code(tmp_path):
    """Build a real agent file carrying a verifier, then ask it for status."""
    from swarm.hub.agentbundle import build_agent_pyz

    code = derive_access_code(OWNER)
    pyz = tmp_path / "swarm-agent.pyz"
    build_agent_pyz(output=pyz, config={"hub": "http://127.0.0.1:9", "token": "t", "access": make_verifier(code, iterations=1000)})
    env = {"USERPROFILE": str(tmp_path), "HOME": str(tmp_path), "PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}

    def run(args, secret):
        env2 = dict(env, SWARM_ACCESS_CODE=secret)
        return subprocess.run([sys.executable, str(pyz), *args], capture_output=True, text=True, env=env2, timeout=60)

    wrong = run(["status"], "AAAA-AAAA")
    assert wrong.returncode == 1 and wrong.stdout.strip() == "no", "the wrong code reveals nothing"
    right = run(["status"], code)
    assert right.returncode == 0 and "hub       http://127.0.0.1:9" in right.stdout
    assert run(["pause"], code).returncode == 0 and (tmp_path / ".swarm" / "paused").exists()
    assert "PAUSED" in run(["status"], code).stdout
    assert run(["resume"], code).returncode == 0 and not (tmp_path / ".swarm" / "paused").exists()


def test_a_valid_invite_also_proves_membership_for_hubinfo():
    """A node that joined seconds before a failover may be missing from the
    successor's replica; its invite (which is in the replica) lets it find
    and re-join the new hub."""
    hub = Hub(host="127.0.0.1", port=0, secure=True, owner_key=OWNER)
    _, port = hub.start_background()
    try:
        token = hub.enrollment.create()["token"]
        assert _get(port, "/api/hubinfo", {"X-Swarm-Token": token})[0] == 200
        assert _get(port, "/api/hubinfo", {"X-Swarm-Token": "swk_expired"})[0] == 404
    finally:
        hub.stop()
