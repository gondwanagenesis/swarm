import platform

from swarm.probe import cpu as cpu_mod

FAKE_CPUINFO = """processor   : 0
model name  : TestChip 3000
cpu cores   : 4
cpu MHz     : 3000.000
processor   : 1
model name  : TestChip 3000
cpu cores   : 4
cpu MHz     : 1800.000
"""

FAKE_MEMINFO = """MemTotal:       16384000 kB
MemAvailable:    8192000 kB
"""


class FakePaths:
    def __init__(self):
        self.files = {}

    def read(self, path):
        return self.files.get(path)


def test_linux_biglittle_frequency_spread(monkeypatch):
    fake = FakePaths()
    fake.files["/proc/cpuinfo"] = FAKE_CPUINFO
    fake.files["/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq"] = "3000000"
    fake.files["/sys/devices/system/cpu/cpu1/cpufreq/cpuinfo_max_freq"] = "1800000"
    monkeypatch.setattr(cpu_mod, "_read_text", fake.read)
    monkeypatch.setattr(
        cpu_mod, "_read_int", lambda p: int(fake.read(p)) if fake.read(p) else None
    )
    listed = ["cpu0", "cpu1"]
    monkeypatch.setattr(cpu_mod.os, "listdir", lambda p: listed)
    monkeypatch.setattr(cpu_mod.os.path, "join", lambda *a: "/".join(a))
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(cpu_mod.os, "cpu_count", lambda: 2)

    info, anomalies = cpu_mod.collect_cpu()
    assert info.model == "TestChip 3000"
    assert info.physical_cores == 4
    assert info.max_freq_hz == 3.0e9
    assert info.heterogeneous is True
    assert info.freq_spread_ratio and abs(info.freq_spread_ratio - 0.4) < 1e-6


def test_linux_cgroup_v2_quota(monkeypatch):
    fake = FakePaths()
    fake.files["/proc/cpuinfo"] = FAKE_CPUINFO
    fake.files["/sys/fs/cgroup/cpu.max"] = "150000 100000"
    monkeypatch.setattr(cpu_mod, "_read_text", fake.read)
    monkeypatch.setattr(cpu_mod, "_read_int", lambda p: None)
    monkeypatch.setattr(
        cpu_mod.os, "listdir", lambda p: (_ for _ in ()).throw(OSError())
    )
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(cpu_mod.os, "cpu_count", lambda: 16)

    info, _ = cpu_mod.collect_cpu()
    assert info.cgroup_quota_cores == 1.5


def test_linux_meminfo(monkeypatch):
    fake = FakePaths()
    fake.files["/proc/meminfo"] = FAKE_MEMINFO
    monkeypatch.setattr(cpu_mod, "_read_text", fake.read)
    monkeypatch.setattr(cpu_mod, "_read_int", lambda p: None)
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    info, anomalies = cpu_mod.collect_memory()
    assert info.total_bytes == 16384000 * 1024
    assert info.free_bytes == 8192000 * 1024
    assert not [a for a in anomalies if a.severity == "error"]


def test_windows_cim_cpu(monkeypatch):
    payload = (
        '[{"Name":"TestWin CPU","NumberOfCores":8,'
        '"NumberOfLogicalProcessors":16,"MaxClockSpeed":4300}]'
    )
    monkeypatch.setattr(cpu_mod, "run_bounded", lambda cmd, timeout=15.0: payload)
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(cpu_mod.os, "cpu_count", lambda: 16)

    info, anomalies = cpu_mod.collect_cpu()
    assert info.model == "TestWin CPU"
    assert info.physical_cores == 8
    assert info.logical_cores == 16
    assert info.max_freq_hz == 4.3e9


def test_never_raises_when_everything_is_missing(monkeypatch):
    monkeypatch.setattr(cpu_mod, "_read_text", lambda p: None)
    monkeypatch.setattr(cpu_mod, "_read_int", lambda p: None)
    monkeypatch.setattr(cpu_mod, "run_bounded", lambda cmd, timeout=15.0: None)
    monkeypatch.setattr(cpu_mod.os, "listdir", lambda p: [])
    monkeypatch.setattr(platform, "system", lambda: "Plan9")

    info, anomalies = cpu_mod.collect_cpu()
    assert info.model is None
    mem, mem_anomalies = cpu_mod.collect_memory()
    assert mem.total_bytes is None
    assert any(a.severity in ("warning", "info") for a in mem_anomalies)
