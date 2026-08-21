import platform

from swarm.probe import gpu as gpu_mod
from swarm.core.models import DeviceInfo


def _nv(primary=True):
    return DeviceInfo(
        kind="gpu",
        name="RTX 4090",
        vendor="NVIDIA",
        pci_address="0000:01:00.0",
        uuid="GPU-abc",
        vram_bytes=24 * 1024**3,
        runtimes=["cuda"],
        evidence={"vendor": "nvidia-smi"},
    )


def test_merge_dedups_on_pci_vendor_wins():
    generic = DeviceInfo(
        kind="gpu",
        name="Wrong Name From WMI",
        vendor="Intel",
        pci_address="0000:01:00.0",
        vram_bytes=1234,
        runtimes=["opencl"],
        evidence={"generic": "wmi"},
    )
    merged = gpu_mod.merge_devices([_nv()], [generic])
    assert len(merged) == 1
    dev = merged[0]
    assert dev.name == "RTX 4090"
    assert dev.vram_bytes == 24 * 1024**3
    assert "opencl" in dev.runtimes and "cuda" in dev.runtimes
    assert dev.evidence.get("generic") == "wmi"


def test_merge_without_keys_keeps_both():
    a = DeviceInfo(kind="gpu", name="A")
    b = DeviceInfo(kind="gpu", name="B")
    merged = gpu_mod.merge_devices([a], [b])
    assert len(merged) == 2


def test_windows_adapterram_clamp_becomes_none(monkeypatch):
    payload = (
        '[{"Name":"Future GPU 32GB","AdapterRAM":4293918720,'
        '"DriverVersion":"99.0","PNPDeviceID":"PCI\\\\VEN_10DE&DEV_2704",'
        '"AdapterCompatibility":"NVIDIA"}]'
    )
    monkeypatch.setattr(gpu_mod, "run_bounded", lambda cmd, timeout=15.0: payload)
    devices, anomalies = gpu_mod._windows_cim_gpu()
    assert len(devices) == 1
    dev = devices[0]
    assert dev.vendor == "NVIDIA"
    assert dev.vram_bytes is None
    assert any("clamp" in a.message.lower() for a in anomalies)


def test_windows_vendor_mapping(monkeypatch):
    payload = (
        '[{"Name":"iGPU","AdapterRAM":1073741824,"DriverVersion":"1.0",'
        '"PNPDeviceID":"PCI\\\\VEN_8086&DEV_A780","AdapterCompatibility":"Intel"}]'
    )
    monkeypatch.setattr(gpu_mod, "run_bounded", lambda cmd, timeout=15.0: payload)
    devices, _ = gpu_mod._windows_cim_gpu()
    assert devices[0].vendor == "Intel"
    assert devices[0].unified_memory is True
    assert devices[0].vram_bytes == 1073741824


def test_collect_never_raises_with_no_tools(monkeypatch):
    monkeypatch.setattr(gpu_mod, "run_bounded", lambda cmd, timeout=15.0: None)
    monkeypatch.setattr(platform, "system", lambda: "Haiku")
    devices, anomalies = gpu_mod.collect_devices()
    assert devices == []
    assert any(a.source == "gpu" for a in anomalies)
