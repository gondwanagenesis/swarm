import importlib

from swarm.probe import self_probe
from swarm.core.models import AgentCapability


def test_floor_zero_when_nothing_exists(monkeypatch):
    monkeypatch.setattr(self_probe.shutil, "which", lambda tool: None)

    def no_import(name):
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", no_import)
    cap, anomalies = self_probe.climb_tower(bench_instrument=True)
    assert cap.max_floor == 0
    assert cap.tools == {}
    assert cap.packages == {}
    assert "json_roundtrip_us" in cap.instrument


def test_floor_one_with_system_tools(monkeypatch):
    def fake_which(tool):
        return "/usr/bin/clinfo" if tool == "clinfo" else None

    monkeypatch.setattr(self_probe.shutil, "which", fake_which)
    monkeypatch.setattr(self_probe, "_tool_version", lambda t: "present")

    def no_import(name):
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", no_import)
    cap, _ = self_probe.climb_tower(bench_instrument=False)
    assert cap.max_floor == 1
    assert "clinfo" in cap.tools
    assert "opencl" in cap.runtimes


def test_floor_two_with_packages(monkeypatch):
    monkeypatch.setattr(self_probe.shutil, "which", lambda tool: None)

    class FakeMod:
        __version__ = "1.26.4"

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: (
            FakeMod() if name == "numpy" else (_ for _ in ()).throw(ImportError(name))
        ),
    )
    cap, _ = self_probe.climb_tower(bench_instrument=False)
    assert cap.max_floor == 2
    assert cap.packages["numpy"] == "1.26.4"


def test_floor_three_with_bench_suite(monkeypatch):
    monkeypatch.setattr(
        self_probe.shutil,
        "which",
        lambda tool: "/usr/local/bin/clpeak" if tool == "clpeak" else None,
    )
    monkeypatch.setattr(self_probe, "_tool_version", lambda t: "present")

    def no_import(name):
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", no_import)
    cap, _ = self_probe.climb_tower(bench_instrument=False)
    assert cap.max_floor == 3


def test_never_raises_even_if_which_explodes(monkeypatch):
    monkeypatch.setattr(
        self_probe.shutil,
        "which",
        lambda tool: (_ for _ in ()).throw(RuntimeError("PATH corrupted")),
    )
    cap, anomalies = self_probe.climb_tower(bench_instrument=False)
    assert isinstance(cap, AgentCapability)
    assert any("tools" in a.source for a in anomalies)
