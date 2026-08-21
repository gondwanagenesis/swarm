from swarm.probe import power as power_mod
from swarm.core.models import PowerTrust


def test_rapl_wrap_modulo_realistic_range():
    before = (260.0, 262.144741, 1000.0)
    after = (2.0, 262.144741, 1010.0)
    watts = power_mod.rapl_watts(before, after)
    assert watts is not None
    assert abs(watts - 0.4144) < 0.01


def test_rapl_normal_delta():
    watts = power_mod.rapl_watts((100.0, 262.0, 0.0), (700.0, 262.0, 10.0))
    assert abs(watts - 60.0) < 1e-9


def test_rapl_nonpositive_dt_is_none():
    assert power_mod.rapl_watts((1.0, 262.0, 5.0), (2.0, 262.0, 5.0)) is None


def test_battery_only_counts_when_discharging(tmp_path, monkeypatch):
    bat = tmp_path / "BAT0"
    bat.mkdir()
    (bat / "status").write_text("Charging\n")
    monkeypatch.setattr(power_mod.glob, "glob", lambda pat: [str(bat)])
    assert power_mod._linux_battery() is None
    (bat / "status").write_text("Discharging\n")
    (bat / "power_now").write_text("15000000\n")
    reading = power_mod._linux_battery()
    assert reading is not None
    assert abs(reading.watts - 15.0) < 1e-9
    assert reading.trust is PowerTrust.BATTERY


def test_none_trust_is_valid_not_error(monkeypatch):
    monkeypatch.setattr(power_mod, "_linux_battery", lambda: None)
    monkeypatch.setattr(power_mod, "rapl_snapshot", lambda: None)
    reading, anomalies = power_mod.collect_power()
    assert reading.trust is PowerTrust.NONE
    assert reading.watts is None
    assert not any(a.severity == "error" for a in anomalies)
