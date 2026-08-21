import json
import time

from swarm.core.models import AgentCapability, DeviceInfo, NodeProfile
from swarm.core.serde import to_dict
from swarm.hub.coverage import coverage_report, device_class
from swarm.hub.registry import Registry
from swarm.probe import hotplug
from swarm.probe.hotplug import HotplugWatcher, diff_devices


def _gpu(name="RTX", pci="0000:01:00.0", uuid=None):
    return DeviceInfo(kind="gpu", name=name, vendor="NVIDIA", pci_address=pci, uuid=uuid)


def test_diff_added_removed_by_merge_key():
    before = [_gpu(), _gpu("iGPU", pci="0000:00:02.0")]
    after = [_gpu(), _gpu("NewGPU", pci="0000:02:00.0")]
    added, removed, degraded = diff_devices(before, after)
    assert len(added) == 1 and added[0].name == "NewGPU"
    assert len(removed) == 1 and removed[0].name == "iGPU"
    assert degraded == []


def test_diff_identity_degraded_when_no_keys():
    before = [DeviceInfo(kind="gpu", name="X", vendor="AMD")]
    after = [DeviceInfo(kind="gpu", name="Y", vendor="AMD")]
    added, removed, degraded = diff_devices(before, after)
    assert len(degraded) == 2


def test_watcher_fires_on_change(monkeypatch):
    state = {"devices": [_gpu()]}
    monkeypatch.setattr(hotplug, "collect_devices", lambda: (list(state["devices"]), []))
    events = []
    watcher = HotplugWatcher(interval=0.1)
    watcher.on_change(lambda added, removed: events.append((added, removed)))
    thread = watcher.start()
    state["devices"] = [_gpu(), _gpu("USB-NPU", pci="0000:03:00.0")]
    deadline = time.time() + 5.0
    while not events and time.time() < deadline:
        time.sleep(0.05)
    watcher.stop()
    thread.join(timeout=3)
    assert events and events[0][0][0].name == "USB-NPU"


def test_watcher_silent_when_stable(monkeypatch):
    monkeypatch.setattr(hotplug, "collect_devices", lambda: ([_gpu()], []))
    events = []
    watcher = HotplugWatcher(interval=0.05)
    watcher.on_change(lambda a, r: events.append(1))
    watcher.start()
    time.sleep(0.3)
    watcher.stop()
    assert events == []


def test_device_class_normalization():
    assert device_class("NVIDIA", "GeForce RTX 4090") == "nvidia:geforce_rtx_4090"
    assert device_class(None, None) == "unknown:unknown"


def _registry_with_device():
    reg = Registry(":memory:")
    profile = NodeProfile(
        node_id="n1",
        hostname="box",
        os="linux",
        arch="x86_64",
        devices=[
            DeviceInfo(kind="gpu", name="GeForce RTX 4090", vendor="NVIDIA", pci_address="0000:01:00.0")
        ],
    )
    reg.upsert_node(profile, AgentCapability(), json.dumps(to_dict(profile)), "{}")
    return reg


def test_coverage_uncovered_then_covered():
    reg = _registry_with_device()
    report = coverage_report(reg)
    assert report["total_devices"] == 1
    assert any(d["device_class"] == "nvidia:geforce_rtx_4090" for d in report["uncovered"])
    assert report["covered"] == []

    reg.record_adapter(
        adapter_id="hw-abc",
        device_class="nvidia:geforce_rtx_4090",
        authored_by="human",
        gate_run_id="gate-1",
    )
    report = coverage_report(reg)
    assert report["covered"] == ["nvidia:geforce_rtx_4090"]
    assert report["uncovered"] == []


def test_adapter_without_gate_pass_does_not_cover():
    reg = _registry_with_device()
    reg.record_adapter(
        adapter_id="ai-draft",
        device_class="nvidia:geforce_rtx_4090",
        authored_by="ai",
        gate_run_id="",
    )
    report = coverage_report(reg)
    assert report["covered"] == []
    assert len(report["uncovered"]) == 1
