"""The service manager runs only what this node discovered, with a command
line it built itself from a validated spec — a hub can ask, never dictate."""

import sys
import time
from pathlib import Path

import pytest

from swarm.agent import services
from swarm.agent.services import ServiceManager, SpecError, build_command, validate_spec

FAKE = str(Path(__file__).parent / "fixtures" / "fake_llama.py")


def _resolver(tmp_path):
    (tmp_path / "tiny.gguf").write_bytes(b"GGUF")

    def resolve(name):
        p = tmp_path / (name + ".gguf")
        return p if p.exists() else None

    return resolve


def test_rejects_what_a_hostile_hub_might_send(tmp_path):
    resolve = _resolver(tmp_path)
    base = {"service_id": "svc-1", "kind": "llama_rpc", "port": 50052, "bind": "127.0.0.1"}
    validate_spec(base, resolve)
    bad = [
        dict(base, kind="shell"),
        dict(base, port=22),
        dict(base, bind="0.0.0.0"),
        dict(base, bind="example.com"),
        dict(base, service_id="../../x"),
        dict(base, device="Vulkan0; rm -rf /"),
        dict(base, kind="llama_server", model="../../etc/passwd"),
        dict(base, kind="llama_server", model="C:\\Windows\\x"),
        dict(base, kind="llama_server", model="not-on-this-node"),
        dict(base, kind="llama_server", model="tiny", rpc=["evil host:1"]),
        dict(base, kind="llama_server", model="tiny", tensor_split=[-1, 2]),
    ]
    for spec in bad:
        with pytest.raises(SpecError):
            validate_spec(spec, resolve)


def test_wildcard_bind_needs_explicit_owner_override(tmp_path, monkeypatch):
    spec = {"service_id": "s", "kind": "llama_rpc", "port": 50052, "bind": "0.0.0.0"}
    with pytest.raises(SpecError):
        validate_spec(spec, _resolver(tmp_path))
    monkeypatch.setenv("SWARM_ALLOW_WILDCARD_BIND", "1")
    assert validate_spec(spec, _resolver(tmp_path))["bind"] == "0.0.0.0"


def test_command_line_is_built_here(tmp_path):
    resolve = _resolver(tmp_path)
    spec = validate_spec(
        {
            "service_id": "svc-2", "kind": "llama_server", "port": 8090, "bind": "100.64.0.5",
            "model": "tiny", "rpc": ["100.64.0.9:50052"], "tensor_split": [20, 12], "ctx": 4096,
        },
        resolve,
    )
    cmd = build_command(spec, {"llama_server": "/opt/llama-server"})
    assert cmd[0] == "/opt/llama-server"
    assert cmd[cmd.index("--model") + 1] == str(tmp_path / "tiny.gguf")
    assert cmd[cmd.index("--rpc") + 1] == "100.64.0.9:50052"
    assert cmd[cmd.index("--tensor-split") + 1] == "20,12"
    with pytest.raises(SpecError):
        build_command(spec, {})  # no binary discovered -> nothing runs


def _fake_build(spec, binaries):
    real = build_command(spec, {"llama_rpc": "x", "llama_server": "x"})
    mode = "rpc" if spec["kind"] == "llama_rpc" else "server"
    return [sys.executable, FAKE, mode, *real[1:]]


def _free_port():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_reconcile_starts_reports_and_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(services, "build_command", _fake_build)
    mgr = ServiceManager({"llama_rpc": "x"}, _resolver(tmp_path), log_dir=tmp_path / "logs")
    port = _free_port()
    spec = {"service_id": "svc-rpc", "kind": "llama_rpc", "port": port, "bind": "127.0.0.1"}
    try:
        status = mgr.reconcile([spec])
        assert status[0]["state"] in ("starting", "running")
        deadline = time.time() + 15
        while time.time() < deadline and mgr.status()[0]["state"] != "running":
            time.sleep(0.2)
        assert mgr.status()[0]["state"] == "running"
        assert mgr.reconcile([]) == []  # no longer desired -> stopped
    finally:
        mgr.stop_all()


def test_parked_while_host_is_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(services, "build_command", _fake_build)
    mgr = ServiceManager(
        {"llama_rpc": "x"}, _resolver(tmp_path),
        welfare=lambda: {"allowed": False, "reason": "user active 3s ago"}, log_dir=tmp_path,
    )
    st = mgr.reconcile([{"service_id": "s", "kind": "llama_rpc", "port": _free_port(), "bind": "127.0.0.1"}])
    assert st[0]["state"] == "parked" and "user active" in st[0]["error"]


def test_crashed_service_is_reported_with_its_log(tmp_path, monkeypatch):
    def crashing(spec, binaries):
        return [sys.executable, "-c", "import sys; print('out of memory: tensor too big'); sys.exit(3)"]

    monkeypatch.setattr(services, "build_command", crashing)
    mgr = ServiceManager({"llama_rpc": "x"}, _resolver(tmp_path), log_dir=tmp_path)
    mgr.reconcile([{"service_id": "s", "kind": "llama_rpc", "port": _free_port(), "bind": "127.0.0.1"}])
    deadline = time.time() + 10
    while time.time() < deadline and mgr.status()[0]["state"] != "failed":
        time.sleep(0.2)
    st = mgr.status()[0]
    assert st["state"] == "failed" and "code 3" in st["error"]
    assert "out of memory" in st["log_tail"]


def test_head_that_dropped_its_rpc_helper_is_a_failure(tmp_path, monkeypatch):
    """llama-server keeps running when a helper speaks another RPC version;
    the manager must not let that pass as a healthy pooled deployment."""
    script = (
        "import sys, time; print('E RPC server version mismatch: 7.0.0'); "
        "print('E Failed to connect to 10.0.0.9:50052'); sys.stdout.flush(); time.sleep(30)"
    )
    monkeypatch.setattr(services, "build_command", lambda spec, b: [sys.executable, "-c", script])
    mgr = ServiceManager({"llama_server": "x"}, _resolver(tmp_path), log_dir=tmp_path)
    spec = {"service_id": "head", "kind": "llama_server", "port": _free_port(), "bind": "127.0.0.1",
            "model": "tiny", "rpc": ["10.0.0.9:50052"]}
    try:
        mgr.reconcile([spec])
        deadline = time.time() + 10
        while time.time() < deadline and mgr.status()[0]["state"] != "failed":
            time.sleep(0.2)
        st = mgr.status()[0]
        assert st["state"] == "failed" and "version mismatch" in st["error"]
    finally:
        mgr.stop_all()
