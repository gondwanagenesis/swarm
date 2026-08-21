import json
import urllib.request

from swarm.agent.daemon import Agent
from swarm.bench.pilot import pilot_cpu_fp32
from swarm.core.models import AgentCapability, DeviceInfo
from swarm.hub.server import Hub
from swarm.hub.verdicts import ADOPT, PARK, SYNTHESIZE, decide_verdict
from swarm.probe.discovery import discover_runtimes


def test_verdict_adopt_when_runtime_binds():
    verdict, reason = decide_verdict(
        "nvidia:rtx_4090",
        [{"device_class": "nvidia:rtx_4090", "runtime": "opencl", "confidence": 0.7, "evidence": {}}],
        None,
        None,
        None,
        set(),
    )
    assert verdict == ADOPT and "runtime" in reason


def test_verdict_parks_battery_weak_pilot():
    verdict, reason = decide_verdict(
        "phone:chip",
        [{"device_class": "phone:chip", "runtime": None, "confidence": 0.0, "evidence": {}}],
        0.01,
        "battery",
        5.0,
        set(),
    )
    assert verdict == PARK and "battery" in reason.lower()


def test_verdict_synthesize_when_pilot_strong_no_binding():
    verdict, reason = decide_verdict(
        "novel:npu9000",
        [{"device_class": "novel:npu9000", "runtime": None, "confidence": 0.0, "evidence": {}}],
        0.5,
        None,
        None,
        set(),
    )
    assert verdict == SYNTHESIZE


def test_verdict_covered_class_short_circuits():
    verdict, reason = decide_verdict(
        "nvidia:rtx_4090",
        [],
        None,
        None,
        None,
        {"nvidia:rtx_4090"},
    )
    assert verdict == ADOPT


def test_pilot_runs_and_scores():
    out = pilot_cpu_fp32()
    assert out["score_gflops"] is not None and out["score_gflops"] > 0
    assert out["trust"] == "fallback"


def test_discovery_marks_cuda_devices():
    devs = [DeviceInfo(kind="gpu", name="RTX 4090", vendor="NVIDIA", uuid="GPU-x", runtimes=["cuda"])]
    cap = AgentCapability(max_floor=0, tools={}, packages={})
    bindings, _ = discover_runtimes(devs, cap)
    assert bindings[0]["runtime"] == "cuda"
    assert bindings[0]["confidence"] == 0.95


def test_discovery_unknown_device_gets_null_binding():
    devs = [DeviceInfo(kind="gpu", name="Mystery NPU", vendor="AcmeCo")]
    cap = AgentCapability(max_floor=0, tools={})
    bindings, _ = discover_runtimes(devs, cap)
    assert bindings[0]["runtime"] is None


def test_registration_records_verdicts_live():
    hub = Hub(port=0)
    host, port = hub.start_background()
    try:
        agent = Agent(hub_url=f"http://{host}:{port}", bench=False, node_id="verdict-node")
        agent.run_once()
        verdicts = json.loads(urllib.request.urlopen(f"http://{host}:{port}/api/verdicts", timeout=5).read())[
            "verdicts"
        ]
        assert isinstance(verdicts, list)
        assert all("verdict" in v and "reason" in v for v in verdicts)
    finally:
        hub.stop()
