import http.client
import json
import urllib.request

from swarm.core.models import (
    AgentCapability,
    Anomaly,
    BenchResult,
    LinkMeasurement,
    MeasurementTrust,
    MemoryInfo,
    NodeProfile,
)
from swarm.core.serde import to_dict
from swarm.hub.registry import Registry
from swarm.hub.server import Hub


def test_registry_round_trip():
    reg = Registry(":memory:")
    profile = NodeProfile(
        node_id="n-test",
        hostname="box",
        os="linux",
        arch="x86_64",
        memory=MemoryInfo(free_bytes=1024),
        anomalies=[Anomaly("cpu", "test anomaly")],
    )
    cap = AgentCapability(max_floor=1, tools={"clinfo": "present"})
    reg.upsert_node(
        profile, cap, json.dumps(to_dict(profile)), json.dumps(to_dict(cap))
    )
    assert reg.heartbeat("n-test")
    reg.record_bench(
        "n-test",
        BenchResult(
            name="cpu_fp32_gflops",
            value=0.05,
            unit="GFLOPS",
            trust=MeasurementTrust.FALLBACK,
            benchmark_run_id="run-x",
        ),
    )
    reg.record_link(
        LinkMeasurement(
            src_node="n-test",
            dst_node="hub",
            rtt_p50_ms=0.5,
            rtt_p95_ms=1.2,
            bandwidth_bps=1e10,
            direct=True,
            trust=MeasurementTrust.STANDARD,
        )
    )
    nodes = reg.list_nodes()
    assert len(nodes) == 1 and nodes[0]["hostname"] == "box"
    benches = reg.latest_benches("n-test")
    assert len(benches) == 1 and benches[0]["trust"] == "fallback"
    links = reg.list_links()
    assert len(links) == 1 and links[0]["direct"] == 1
    anomalies = reg.recent_anomalies()
    assert len(anomalies) == 1 and anomalies[0]["source"] == "cpu"
    reg.close()


def test_hub_http_endpoints():
    hub = Hub(port=0)
    host, port = hub.start_background()
    try:
        payload = {
            "profile": to_dict(
                NodeProfile(node_id="n-http", hostname="h", os="linux", arch="x86_64")
            ),
            "capability": to_dict(AgentCapability(max_floor=2)),
            "benchmarks": [
                to_dict(
                    BenchResult(
                        name="mem_bandwidth_gbps",
                        value=3.3,
                        unit="GB/s",
                        trust=MeasurementTrust.FALLBACK,
                        benchmark_run_id="run-y",
                    )
                )
            ],
        }
        req = urllib.request.Request(
            f"http://{host}:{port}/api/register",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        assert resp["ok"] and resp["node_id"] == "n-http"

        nodes = json.loads(
            urllib.request.urlopen(f"http://{host}:{port}/api/nodes", timeout=10).read()
        )
        assert any(n["node_id"] == "n-http" for n in nodes["nodes"])

        detail = json.loads(
            urllib.request.urlopen(
                f"http://{host}:{port}/api/nodes/n-http", timeout=10
            ).read()
        )
        assert detail["benches"][0]["value"] == 3.3

        ping = json.loads(
            urllib.request.urlopen(f"http://{host}:{port}/api/ping", timeout=10).read()
        )
        assert ping["ok"]

        body = b"\x00" * 65536
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.request("POST", "/api/echo", body=body)
        echoed = conn.getresponse().read()
        conn.close()
        assert echoed == body

        dash = (
            urllib.request.urlopen(f"http://{host}:{port}/", timeout=10).read().decode()
        )
        assert "n-http" in dash and "F2" in dash
    finally:
        hub.stop()
