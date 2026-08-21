import json

from swarm.core.models import (
    AdapterOrigin,
    AdapterRecord,
    AgentCapability,
    Anomaly,
    CpuInfo,
    DeviceInfo,
    MemoryInfo,
    NodeProfile,
    PowerReading,
    PowerTrust,
    TaskSpec,
)
from swarm.core.serde import canonical_json, from_dict, to_dict


def _full_profile() -> NodeProfile:
    return NodeProfile(
        node_id="node-1",
        hostname="rig",
        os="linux",
        arch="x86_64",
        cpu=CpuInfo(
            model="Test CPU", logical_cores=8, physical_cores=4, heterogeneous=False
        ),
        memory=MemoryInfo(total_bytes=16 * 1024**3, free_bytes=8 * 1024**3),
        devices=[
            DeviceInfo(
                kind="gpu",
                name="Test GPU",
                vendor="NVIDIA",
                pci_address="0000:01:00.0",
                vram_bytes=24 * 1024**3,
                runtimes=["cuda", "opencl"],
                evidence={"vendor": "nvidia-smi"},
            )
        ],
        power=PowerReading(watts=120.5, trust=PowerTrust.RAPL_PACKAGE),
        anomalies=[Anomaly("gpu", "one source missing")],
        probed_at=1234.5,
    )


def test_round_trip_full_profile():
    original = _full_profile()
    restored = from_dict(NodeProfile, to_dict(original))
    assert restored == original
    assert restored.power.trust is PowerTrust.RAPL_PACKAGE


def test_unknown_keys_dropped_forward_compat():
    data = to_dict(_full_profile())
    data["future_field"] = {"nested": True}
    restored = from_dict(NodeProfile, data)
    assert not hasattr(restored, "future_field")


def test_missing_keys_use_defaults():
    restored = from_dict(NodeProfile, {"node_id": "n9"})
    assert restored.node_id == "n9"
    assert restored.devices == []
    assert restored.cpu is None


def test_enums_serialize_as_strings():
    record = AdapterRecord(adapter_id="ai-1", authored_by=AdapterOrigin.AI)
    data = to_dict(record)
    assert data["authored_by"] == "ai"
    assert json.dumps(data)


def test_canonical_json_deterministic():
    a = TaskSpec(op="x", input_refs=["1"], params={"b": "2", "a": "1"})
    b = TaskSpec(op="x", input_refs=["1"], params={"a": "1", "b": "2"})
    assert canonical_json(a) == canonical_json(b)
    assert canonical_json(a) == canonical_json(a)


def test_nested_none_survives_round_trip():
    cap = AgentCapability(
        max_floor=1, tools={"clinfo": None}, instrument={"json_roundtrip_us": None}
    )
    restored = from_dict(AgentCapability, to_dict(cap))
    assert restored.tools["clinfo"] is None
    assert restored.instrument["json_roundtrip_us"] is None
    assert restored.max_floor == 1
