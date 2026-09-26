"""The consent membrane, enforced: a hub reachable off-box is secure by
default. Owner key for owner routes, node keys for the work loop, enrollment
tokens for joining — and a stranger with none of them gets nothing that runs
code anywhere."""

import json
import urllib.error
import urllib.request

import pytest

from swarm.hub.server import Hub

OWNER = "swo_test-owner-key"


def _call(port, path, payload=None, headers=None, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method or ("POST" if data is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body[:1] in (b"{", b"[") else body)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, body


def _profile(node_id):
    return {
        "profile": {"node_id": node_id, "hostname": node_id, "os": "linux", "arch": "x86_64"},
        "capability": {},
        "benchmarks": [],
    }


@pytest.fixture()
def secure_hub():
    hub = Hub(host="127.0.0.1", port=0, secure=True, owner_key=OWNER)
    _, port = hub.start_background()
    yield hub, port
    hub.stop()


def test_loopback_hub_is_open_and_off_box_hub_is_secure():
    open_hub = Hub(host="127.0.0.1", port=0)
    assert not open_hub.secure and not open_hub.require_token
    lan = Hub(host="0.0.0.0", port=0, owner_key=OWNER)
    assert lan.secure and lan.require_token
    open_hub.registry.close()
    lan.registry.close()


def test_owner_routes_need_the_owner_key(secure_hub):
    hub, port = secure_hub
    bag = {"op": "primesum", "params_list": [{"n": 10}]}
    code, _ = _call(port, "/api/bag/submit", bag)
    assert code == 404, "dark: a stranger cannot even tell the route exists"
    code, _ = _call(port, "/api/bag/submit", bag, {"Authorization": "Bearer wrong"})
    assert code == 404
    code, body = _call(port, "/api/bag/submit", bag, {"Authorization": f"Bearer {OWNER}"})
    assert code == 200 and body["ok"]
    # reading anything about the fleet is owner-only too
    assert _call(port, "/api/nodes")[0] == 404
    assert _call(port, "/api/tokens")[0] == 404
    assert _call(port, "/api/backup")[0] == 404
    assert _call(port, "/api/nodes", headers={"X-Swarm-Key": OWNER})[0] == 200
    # liveness stays public so agents can measure the link before joining
    assert _call(port, "/api/ping")[0] == 200


def test_code_carrying_bags_and_self_edits_are_locked_to_strangers(secure_hub):
    _, port = secure_hub
    evil = {"op": "x", "params_list": [{"adapter_source": "import os\ndef run(p): os.system('boom')"}]}
    assert _call(port, "/api/bag/submit", evil)[0] == 404
    patch = {"title": "t", "reason": "r", "files": {"swarm/__init__.py": "pwned"}}
    assert _call(port, "/api/workshop/propose", patch)[0] == 404
    assert _call(port, "/api/brain/admin", {"action": "enable"})[0] == 404


def test_join_needs_a_token_then_the_node_key_takes_over(secure_hub):
    hub, port = secure_hub
    code, body = _call(port, "/api/register", _profile("n1"))
    assert code == 404, "dark: without an invite the join endpoint looks like nothing"

    token = hub.enrollment.create()["token"]
    payload = dict(_profile("n1"), token=token)
    code, body = _call(port, "/api/register", payload)
    assert code == 200 and body["node_key"].startswith("swn_")
    key = body["node_key"]

    # work loop without the key: refused; with it: fine
    assert _call(port, "/api/tasks/pull", {"node_id": "n1"})[0] == 401
    hdr = {"X-Swarm-Node": "n1", "X-Swarm-Node-Key": key}
    code, body = _call(port, "/api/tasks/pull", {"node_id": "n1"}, hdr)
    assert code == 200 and body["ok"]

    # re-registration with the key needs no token (tokens expire; keys stand)
    code, body = _call(port, "/api/register", _profile("n1"), hdr)
    assert code == 200 and "node_key" not in body


def test_a_node_key_never_speaks_for_another_node(secure_hub):
    hub, port = secure_hub
    token = hub.enrollment.create()["token"]
    _, a = _call(port, "/api/register", dict(_profile("a"), token=token))
    _, b = _call(port, "/api/register", dict(_profile("b"), token=token))
    hdr_a = {"X-Swarm-Node": "a", "X-Swarm-Node-Key": a["node_key"]}
    assert _call(port, "/api/tasks/pull", {"node_id": "b"}, hdr_a)[0] == 401
    assert _call(port, "/api/heartbeat", {"node_id": "b"}, hdr_a)[0] == 401
    hdr_wrong = {"X-Swarm-Node": "a", "X-Swarm-Node-Key": b["node_key"]}
    assert _call(port, "/api/heartbeat", {"node_id": "a"}, hdr_wrong)[0] == 401


def test_revoked_node_is_out(secure_hub):
    hub, port = secure_hub
    token = hub.enrollment.create()["token"]
    _, body = _call(port, "/api/register", dict(_profile("r"), token=token))
    hdr = {"X-Swarm-Node": "r", "X-Swarm-Node-Key": body["node_key"]}
    assert _call(port, "/api/heartbeat", {"node_id": "r"}, hdr)[0] == 200
    assert _call(port, "/api/nodes/revoke", {"node_id": "r"}, {"Authorization": f"Bearer {OWNER}"})[0] == 200
    assert _call(port, "/api/heartbeat", {"node_id": "r"}, hdr)[0] == 401


def test_invite_page_never_mints_tokens_for_strangers(secure_hub):
    hub, port = secure_hub
    code, _ = _call(port, "/invite")
    assert code == 404
    assert hub.enrollment.list_tokens() == []
    token = hub.enrollment.create()["token"]
    code, page = _call(port, f"/invite/{token}")
    assert code == 200 and b"swarm-agent.pyz" in page


def test_dashboard_key_visit_sets_cookie(secure_hub):
    _, port = secure_hub
    assert _call(port, "/")[0] == 404
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", f"/?key={OWNER}")
    resp = conn.getresponse()
    assert resp.status == 303
    cookie = resp.getheader("Set-Cookie")
    assert cookie and "HttpOnly" in cookie
    conn.close()
    code, page = _call(port, "/", headers={"Cookie": f"swarm_key={OWNER}"})
    assert code == 200 and b"Fleet" in page


def test_bundle_bakes_the_address_the_client_used(secure_hub):
    hub, port = secure_hub
    token = hub.enrollment.create()["token"]
    import io
    import zipfile

    code, blob = _call(port, f"/bundle.pyz?token={token}")
    assert code == 200
    cfg = json.loads(zipfile.ZipFile(io.BytesIO(blob)).read("swarm_config.json"))
    assert cfg["hub"] == f"http://127.0.0.1:{port}" and cfg["token"] == token
