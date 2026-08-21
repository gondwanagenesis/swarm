"""Recon pass: adaptive hotplug, debounced spore, raw mDNS, backup, events."""

import hashlib
import json
import sqlite3
import tempfile
import time
import urllib.request

from swarm.agent.spore import AttachmentWatcher
from swarm.hub.server import Hub
from swarm.probe import hotplug
from swarm.probe.hotplug import HotplugWatcher


def _gpu(name="A"):
    from swarm.core.models import DeviceInfo

    return DeviceInfo(kind="gpu", name=name, pci_address=f"0000:0{name}:00.0")


def test_hotplug_adaptive_interval_relaxes(monkeypatch):
    monkeypatch.setattr(hotplug, "collect_devices", lambda: ([_gpu("A")], []))
    watcher = HotplugWatcher(interval=0.05)
    watcher.start()
    time.sleep(0.3)
    watcher.stop()
    assert watcher._known  # baseline captured


def test_spore_debounce_ghost_leases_die(monkeypatch):
    import swarm.agent.spore as spore_mod

    state = {"ifaces": ["lo"]}
    monkeypatch.setattr(spore_mod, "_iface_names", lambda: list(state["ifaces"]))
    monkeypatch.setattr(spore_mod, "_adb_devices", lambda: [])
    monkeypatch.setattr(spore_mod, "_tailscale_peers", lambda: [])
    fired = []
    watcher = AttachmentWatcher(poll_s=0.05, debounce=3)
    watcher.on_attach(lambda ch, hint: fired.append(ch))
    thread = watcher.start()
    state["ifaces"].append("rndis0")  # appears
    time.sleep(0.06)
    state["ifaces"].remove("rndis0")  # vanishes before 3rd tick — ghost
    time.sleep(0.3)
    watcher.stop()
    thread.join(timeout=2)
    assert fired == [], "ghost attachment leaked through debounce"


def test_spore_debounce_confirming_attach(monkeypatch):
    import swarm.agent.spore as spore_mod

    state = {"ifaces": ["lo"]}
    monkeypatch.setattr(spore_mod, "_iface_names", lambda: list(state["ifaces"]))
    monkeypatch.setattr(spore_mod, "_adb_devices", lambda: [])
    monkeypatch.setattr(spore_mod, "_tailscale_peers", lambda: [])
    fired = []
    watcher = AttachmentWatcher(poll_s=0.05, debounce=2)
    watcher.on_attach(lambda ch, hint: fired.append((ch, hint)))
    watcher.start()
    time.sleep(0.2)  # let the baseline pass complete
    state["ifaces"].append("usb-net0")
    deadline = time.time() + 4
    while not fired and time.time() < deadline:
        time.sleep(0.05)
    watcher.stop()
    assert fired and fired[0][0] == "interface"


def test_mdns_packet_roundtrip():
    from swarm.agent import mdns

    q = mdns.build_query("_swarm._tcp.local")
    assert q[4:6] == b"\x00\x01"
    resp = mdns.build_response_ptr("seed-deadbeefcafe._swarm._tcp.local")
    pairs = mdns.parse_response(resp)
    assert pairs == [("_swarm._tcp.local", "seed-deadbeefcafe._swarm._tcp.local")]


def test_backup_roundtrip():
    reg_hub = Hub(port=0)
    host, port = reg_hub.start_background()
    try:
        from swarm.core.models import AgentCapability, NodeProfile
        from swarm.core.serde import to_dict

        reg_hub.registry.upsert_node(
            NodeProfile(node_id="b-node", hostname="b", os="linux", arch="x86_64"),
            AgentCapability(),
            json.dumps(to_dict(NodeProfile(node_id="b-node"))),
            "{}",
        )
        req = urllib.request.Request(f"http://{host}:{port}/api/backup")
        resp = urllib.request.urlopen(req, timeout=15)
        body = resp.read()
        sha = resp.headers.get("X-Backup-SHA256")
        assert sha == hashlib.sha256(body).hexdigest()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as fh:
            fh.write(body)
            path = fh.name
        conn = sqlite3.connect(path)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"nodes", "tasks", "bags", "results", "adapters"} <= names
        rows = conn.execute("SELECT node_id FROM nodes").fetchall()
        assert rows and rows[0][0] == "b-node"
        conn.close()
    finally:
        reg_hub.stop()
