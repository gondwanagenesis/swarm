"""Device-class routing, wired end to end through a real hub.

`set_node_device_classes` existed with no caller: routing was built but off,
so every `device_class` bag reported unservable regardless of the hardware
present. These tests pin the wire that turns it on — registration indexes the
classes a node's MEASURED profile actually contains, and work follows.

The unservable case matters as much as the servable one. A bag no node can
serve must be visibly blocked with a reason, never silently queued forever.
"""

from __future__ import annotations

import pytest

from swarm.core.models import DeviceInfo, NodeProfile
from swarm.core.serde import to_dict
from swarm.hub.server import Hub


def _profile(node_id: str, devices=None) -> NodeProfile:
    return NodeProfile(node_id=node_id, hostname=node_id, devices=devices or [])


def _gpu(vendor="NVIDIA", name="GeForce RTX 4090") -> DeviceInfo:
    return DeviceInfo(kind="gpu", name=name, vendor=vendor)


@pytest.fixture
def hub():
    h = Hub(port=0)
    yield h


def _register(hub: Hub, profile: NodeProfile) -> str:
    return hub.handle_register(
        {"profile": to_dict(profile), "capability": {}, "benchmarks": []}
    )


def test_cpu_only_node_serves_cpu_not_gpu(hub):
    node = _register(hub, _profile("cpu-box"))
    served = {
        r["device_class"]
        for r in hub.queue.conn.execute(
            "SELECT device_class FROM node_device_classes WHERE node_id=?", (node,)
        )
    }
    assert "cpu:generic" in served
    assert "gpu:generic" not in served, "a node with no GPU must not advertise one"


def test_gpu_node_serves_both_granularities(hub):
    """Adapters are gated per specific class; contracts target the kind."""
    node = _register(hub, _profile("gpu-box", [_gpu()]))
    served = {
        r["device_class"]
        for r in hub.queue.conn.execute(
            "SELECT device_class FROM node_device_classes WHERE node_id=?", (node,)
        )
    }
    assert "gpu:generic" in served
    assert "nvidia:geforce_rtx_4090" in served
    assert "cpu:generic" in served


def test_gpu_work_reaches_only_the_gpu_node(hub):
    cpu_node = _register(hub, _profile("cpu-box"))
    gpu_node = _register(hub, _profile("gpu-box", [_gpu()]))

    bag = hub.queue.submit_bag(
        "matmul",
        [{"m": 8, "k": 8, "n": 8, "seed": i} for i in range(4)],
        [f"idem-{i}" for i in range(4)],
        device_class="gpu:generic",
    )

    cpu_pull = hub.queue.pull(cpu_node, 4, 60.0, 10.0)
    assert not cpu_pull, "gpu-classed work leaked onto a node with no GPU"

    gpu_pull = hub.queue.pull(gpu_node, 4, 60.0, 10.0)
    assert gpu_pull, "gpu node was not offered gpu-classed work"
    assert all(t["bag_id"] == bag for t in gpu_pull)


def test_unclassed_work_still_reaches_every_node(hub):
    """Backward compatibility: no device_class means no restriction."""
    cpu_node = _register(hub, _profile("cpu-box"))
    hub.queue.submit_bag("primesum", [{"n": 100}], ["idem-plain"])
    assert hub.queue.pull(cpu_node, 1, 60.0, 10.0), "unclassed work must reach any node"


def test_unservable_bag_is_loud_not_hung(hub):
    """Fail closed AND visible — a silently queued bag reads as progress."""
    _register(hub, _profile("cpu-box"))
    bag = hub.queue.submit_bag(
        "matmul", [{"m": 4, "k": 4, "n": 4}], ["idem-tpu"], device_class="tpu:generic"
    )

    status = hub.queue.bag_status(bag)
    assert status["servable"] is False
    assert status["blocked_reason"], "an unservable bag must say why"
    assert bag in {b["bag_id"] for b in hub.queue.unservable_bags()}


def test_new_capable_node_unblocks_a_stalled_bag(hub):
    """The organism regrows around absence: capacity arriving must unstick it."""
    _register(hub, _profile("cpu-box"))
    bag = hub.queue.submit_bag(
        "matmul", [{"m": 4, "k": 4, "n": 4}], ["idem-gpu"], device_class="gpu:generic"
    )
    assert hub.queue.bag_status(bag)["servable"] is False

    gpu_node = _register(hub, _profile("gpu-box", [_gpu()]))
    assert hub.queue.bag_status(bag)["servable"] is True
    assert hub.queue.pull(gpu_node, 1, 60.0, 10.0), "capable node did not pick up the freed bag"


def test_http_submit_preserves_device_class(hub):
    """Regression: handle_submit dropped device_class, so every classed bag
    submitted over HTTP silently became unrestricted work. The HTTP path is
    the only one real agents use, so the unit-level tests all passed while
    routing was off in practice."""
    _register(hub, _profile("cpu-box"))
    bag = hub.handle_submit(
        {
            "op": "matmul",
            "device_class": "tpu:generic",
            "params_list": [{"m": 4, "k": 4, "n": 4, "seed": 1}],
        }
    )
    status = hub.queue.bag_status(bag)
    assert status["device_class"] == "tpu:generic", "device_class lost in the HTTP path"
    assert status["servable"] is False


def test_http_submit_without_device_class_is_unrestricted(hub):
    node = _register(hub, _profile("cpu-box"))
    bag = hub.handle_submit({"op": "primesum", "params_list": [{"n": 50}]})
    assert hub.queue.bag_status(bag)["device_class"] is None
    assert hub.queue.pull(node, 1, 60.0, 10.0), "unclassed HTTP work must still route"


def test_reregistration_replaces_classes_not_appends(hub):
    """A hotplug removal must retract the class, or routing keeps a ghost."""
    node = _register(hub, _profile("box", [_gpu()]))
    _register(hub, _profile("box"))  # same node, GPU now gone

    served = {
        r["device_class"]
        for r in hub.queue.conn.execute(
            "SELECT device_class FROM node_device_classes WHERE node_id=?", (node,)
        )
    }
    assert "gpu:generic" not in served, "removed device still advertised"
    assert "cpu:generic" in served
