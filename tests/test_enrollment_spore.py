import json
import time
import urllib.error
import urllib.request

from swarm.agent.spore import AttachmentWatcher, snapshot
from swarm.agent.welfare import welfare_gate
from swarm.hub.enrollment import Enrollment
from swarm.hub.server import Hub


def test_token_lifecycle():
    from swarm.hub.registry import Registry

    reg = Registry(":memory:")
    enr = Enrollment(reg._conn, lock=reg._lock)
    tok = enr.create(role="node", label="test")
    assert tok["token"].startswith("swk_")
    valid = enr.validate(tok["token"])
    assert valid is not None and valid["used"] == 0
    consumed = enr.validate(tok["token"], consume=True)
    assert consumed["used"] == 1
    assert enr.revoke(tok["token"])
    assert enr.validate(tok["token"]) is None


def test_token_expiry():
    from swarm.hub.registry import Registry

    reg = Registry(":memory:")
    enr = Enrollment(reg._conn, lock=reg._lock)
    tok = enr.create(ttl_s=-1.0)
    assert enr.validate(tok["token"]) is None


def test_registration_rejected_without_token_when_required():
    hub = Hub(port=0, require_token=True)
    host, port = hub.start_background()
    try:
        payload = {
            "profile": {"node_id": "no-token", "hostname": "h", "os": "linux", "arch": "x86_64"},
            "capability": {},
            "benchmarks": [],
        }
        req = urllib.request.Request(
            f"http://{host}:{port}/api/register",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("registration should have been rejected")
        except urllib.error.HTTPError as e:
            assert e.code == 400

        token = hub.enrollment.create()["token"]
        payload["token"] = token
        req = urllib.request.Request(
            f"http://{host}:{port}/api/register",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
        assert resp["ok"]
    finally:
        hub.stop()


def test_invite_page_and_bundle_with_token():
    hub = Hub(port=0)
    host, port = hub.start_background()
    try:
        page = urllib.request.urlopen(f"http://{host}:{port}/invite", timeout=5).read().decode()
        assert "swk_" in page and "swarm-agent.pyz" in page
        token = next(iter(hub.enrollment.list_tokens()))["token"]
        good = urllib.request.urlopen(f"http://{host}:{port}/bundle.pyz?token={token}", timeout=30).read()
        assert good[:2] == b"PK" and len(good) > 10000
        try:
            urllib.request.urlopen(f"http://{host}:{port}/bundle.pyz?token=swk_bogus", timeout=5)
            raise AssertionError("bogus token must be rejected")
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        hub.stop()


def test_spore_event_logged():
    hub = Hub(port=0)
    host, port = hub.start_background()
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/api/spore/event",
            data=json.dumps(
                {"seed_node_id": "seed-1", "channel": "interface", "peer_hint": "rndis0"}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert json.loads(urllib.request.urlopen(req, timeout=5).read())["ok"]
        events = json.loads(
            urllib.request.urlopen(f"http://{host}:{port}/api/spore/events", timeout=5).read()
        )["events"]
        assert events and events[0]["peer_hint"] == "rndis0"
    finally:
        hub.stop()


def test_watcher_fires_on_interface_change(monkeypatch):
    import swarm.agent.spore as spore_mod

    state = {"ifaces": ["lo", "eth0"]}
    monkeypatch.setattr(spore_mod, "_iface_names", lambda: list(state["ifaces"]))
    monkeypatch.setattr(spore_mod, "_adb_devices", lambda: [])
    fired = []
    watcher = AttachmentWatcher(poll_s=0.05)
    watcher.on_attach(lambda channel, hint: fired.append((channel, hint)))
    thread = watcher.start()
    settle_by = time.time() + 2.0
    while watcher._ifaces == [] and time.time() < settle_by:
        time.sleep(0.02)
    state["ifaces"].append("rndis0")
    deadline = time.time() + 4.0
    while not fired and time.time() < deadline:
        time.sleep(0.05)
    watcher.stop()
    thread.join(timeout=3)
    assert fired and fired[0][0] == "interface" and "rndis0" in fired[0][1]


def test_welfare_gate_returns_info_or_allowance():
    out = welfare_gate()
    assert "allowed" in out and "reason" in out and "details" in out


def test_snapshot_never_raises():
    ifaces, adb = snapshot()
    assert isinstance(ifaces, list) and isinstance(adb, list)
