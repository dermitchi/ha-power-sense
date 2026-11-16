"""Unit tests for the pure calculation module (no Home Assistant needed)."""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location("calc", Path(__file__).parent.parent / "custom_components/power_sense/calc.py")
calc = importlib.util.module_from_spec(spec)
sys.modules["calc"] = calc
spec.loader.exec_module(calc)

UTC = timezone.utc


def ts(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp()


def test_estimate_power_scales_with_brightness_and_min_level():
    states = {"light.a": (True, 255), "light.b": (True, 51), "light.c": (True, 0), "light.d": (False, 255), "switch.e": (True, None)}
    watts = {"light.a": 10, "light.b": 10, "light.c": 10, "light.d": 10}
    # a: 10, b: 2, c: min 10 % = 1, d off, e: default 5 W (no brightness)
    assert calc.estimate_power(states, watts, 5, True) == 10 + 2 + 1 + 5
    assert calc.estimate_power(states, watts, 5, False) == 10 + 10 + 10 + 5


def test_meter_integrates_and_ignores_long_gaps():
    m = calc.Meter()
    m.update(1000, ts(2026, 1, 1, 12), UTC)
    m.update(1000, ts(2026, 1, 1, 12, 6), UTC)  # 6 min at 1 kW = 0.1 kWh
    assert abs(m.total - 0.1) < 1e-9
    m.update(0, ts(2026, 1, 1, 18), UTC)  # 5.9 h gap (HA down): not integrated
    assert abs(m.total - 0.1) < 1e-9


def test_period_rollover_keeps_totals_and_resets_period_energy():
    m = calc.Meter()
    m.update(1000, ts(2026, 1, 31, 23, 50), UTC)
    m.update(1000, ts(2026, 1, 31, 23, 59), UTC)
    assert m.energy("day") > 0 and m.energy("month") > 0
    m.update(1000, ts(2026, 2, 1, 0, 10), UTC)
    assert m.energy("day") < 0.2 and m.energy("month") < 0.2  # new day and month
    assert m.energy("year") == m.total  # still the same year


def test_hourly_energy_splits_at_hour_boundaries():
    steps = [(ts(2026, 1, 1, 10, 30), 600.0), (ts(2026, 1, 1, 11, 30), 0.0)]
    h = calc.hourly_energy(steps, ts(2026, 1, 1, 0), ts(2026, 1, 2, 0))
    assert abs(h[ts(2026, 1, 1, 10)] - 0.3) < 1e-9 and abs(h[ts(2026, 1, 1, 11)] - 0.3) < 1e-9


def test_power_timeline_carries_brightness():
    power = lambda s: calc.estimate_power(s, {}, 10, True)  # noqa: E731
    ev = [(1, "light.a", True, 255), (2, "light.a", False, None), (3, "light.a", True, None)]
    assert calc.power_timeline(ev, power) == [(1, 10.0), (2, 0.0), (3, 10.0)]


def test_apply_history_sets_period_energy_correctly_and_is_replaceable():
    now = ts(2026, 3, 15, 12)
    m = calc.Meter()
    m.update(0, now, UTC)
    m.total = 0.5  # live so far (also today)
    m.base = {"day": ["2026-03-15", 0.0], "month": ["2026-03", 0.0], "year": ["2026", 0.0]}
    hourly = {ts(2026, 3, 15, 6): 1.0, ts(2026, 3, 10, 6): 2.0, ts(2026, 1, 5, 6): 4.0}
    m.apply_history(hourly, now, UTC)
    assert abs(m.total - 7.5) < 1e-9
    assert abs(m.energy("day") - 1.5) < 1e-9 and abs(m.energy("month") - 3.5) < 1e-9 and abs(m.energy("year") - 7.5) < 1e-9
    # importing again with other values replaces the first import instead of adding to it
    d = m.apply_history({ts(2026, 3, 15, 6): 2.0}, now, UTC)
    assert abs(d - (2.0 - 7.0)) < 1e-9
    assert abs(m.total - 2.5) < 1e-9 and abs(m.energy("day") - 2.5) < 1e-9 and abs(m.energy("year") - 2.5) < 1e-9


def test_serialisation_roundtrip():
    m = calc.Meter(total=1.5, power=20.0, ts=5.0)
    assert calc.Meter.from_dict(m.as_dict()) == m
    assert calc.Meter.from_dict(None) == calc.Meter()
