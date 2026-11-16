"""Pure calculation helpers for Power Sense (no Home Assistant imports, fully unit-testable)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

PERIODS = ("day", "month", "year")
MAX_GAP_S = 15 * 60  # longer gaps (HA was down) are not integrated
HOUR = 3600


def period_ids(ts: float, tz) -> dict[str, str]:
    """Identify the day/month/year a timestamp belongs to (local time)."""
    d = datetime.fromtimestamp(ts, tz)
    return {"day": d.strftime("%Y-%m-%d"), "month": d.strftime("%Y-%m"), "year": d.strftime("%Y")}


def estimate_power(
    states: Mapping[str, tuple[bool, float | None]],
    watts: Mapping[str, float],
    default_watts: float,
    scale_brightness: bool,
    min_level: float = 0.1,
) -> float:
    """Estimated power of switched entities.

    states: entity_id -> (is_on, brightness 0..255 or None). Entities that report a
    brightness are scaled by it (never below *min_level*) when *scale_brightness* is set.
    """
    total = 0.0
    for entity_id, (is_on, brightness) in states.items():
        if not is_on:
            continue
        w = watts.get(entity_id, default_watts)
        if scale_brightness and brightness is not None:
            w *= max(brightness / 255.0, min_level)
        total += w
    return total


@dataclass
class Meter:
    """Integrates power into energy and keeps day/month/year baselines."""

    total: float = 0.0  # kWh since the meter was created (plus imported history)
    power: float = 0.0  # W
    ts: float | None = None
    base: dict[str, list] = field(default_factory=dict)  # period -> [period id, baseline kWh]
    hist_total: float = 0.0  # kWh that came from import_history
    hist_period: dict[str, list] = field(default_factory=dict)  # period -> [period id, history kWh in it]
    live_since: float | None = None  # first timestamp measured live (history ends before it)

    def advance(self, now: float) -> None:
        if self.ts is not None and 0 < now - self.ts <= MAX_GAP_S:
            self.total += self.power * (now - self.ts) / 3.6e6
        self.ts = now
        if self.live_since is None:
            self.live_since = now

    def update(self, power: float, now: float, tz) -> None:
        """Integrate up to *now*, take over the new power and roll periods over."""
        self.advance(now)
        self.power = max(power, 0.0)
        ids = period_ids(now, tz)
        for p in PERIODS:
            if self.base.get(p, [None])[0] != ids[p]:
                self.base[p] = [ids[p], self.total]
            if self.hist_period.get(p, [None])[0] != ids[p]:
                self.hist_period[p] = [ids[p], 0.0]

    def energy(self, period: str) -> float:
        return max(self.total - self.base.get(period, [None, self.total])[1], 0.0)

    def apply_history(self, hourly: Mapping[float, float], now: float, tz) -> float:
        """Replace previously imported history by *hourly* (hour start -> kWh).

        Returns the change of the history total (new - old) so the statistics can be shifted.
        """
        ids = period_ids(now, tz)
        new_total = sum(hourly.values())
        new_period = {p: sum(v for t, v in hourly.items() if period_ids(t, tz)[p] == ids[p]) for p in PERIODS}
        for p in PERIODS:
            old_in = self.hist_period.get(p, [ids[p], 0.0])
            old_in = old_in[1] if old_in[0] == ids[p] else 0.0
            if p not in self.base or self.base[p][0] != ids[p]:
                self.base[p] = [ids[p], self.total]
            # period energy = total - base: history inside the period must raise it, history before must not
            self.base[p][1] += (new_total - self.hist_total) - (new_period[p] - old_in)
            self.hist_period[p] = [ids[p], new_period[p]]
        delta = new_total - self.hist_total
        self.total += delta
        self.hist_total = new_total
        return delta

    def as_dict(self) -> dict:
        return {"total": self.total, "power": self.power, "ts": self.ts, "base": self.base, "hist_total": self.hist_total,
                "hist_period": self.hist_period, "live_since": self.live_since}

    @classmethod
    def from_dict(cls, data: Mapping | None) -> Meter:
        if not data:
            return cls()
        return cls(total=data.get("total", 0.0), power=data.get("power", 0.0), ts=data.get("ts"), base=data.get("base", {}),
                   hist_total=data.get("hist_total", 0.0), hist_period=data.get("hist_period", {}), live_since=data.get("live_since"))


def power_timeline(
    events: Iterable[tuple[float, str, bool, float | None]],
    power_of: Callable[[Mapping[str, tuple[bool, float | None]]], float],
    initial: Mapping[str, tuple[bool, float | None]] | None = None,
) -> list[tuple[float, float]]:
    """Turn entity state events into (timestamp, estimated power) steps.

    events: (ts, entity_id, is_on, brightness or None), brightness None keeps the last known one.
    """
    current: dict[str, tuple[bool, float | None]] = dict(initial or {})
    steps: list[tuple[float, float]] = []
    for ts, entity_id, is_on, brightness in sorted(events, key=lambda e: e[0]):
        if brightness is None:
            brightness = current.get(entity_id, (False, None))[1]
        current[entity_id] = (is_on, brightness)
        steps.append((ts, power_of(current)))
    return steps


def hourly_energy(steps: list[tuple[float, float]], start: float, end: float) -> dict[float, float]:
    """Integrate a piecewise-constant power curve (W) into kWh per full hour between start and end.

    *steps* are (timestamp, power) changes; the value before the first step is 0.
    """
    out: dict[float, float] = {}
    if not steps:
        return out
    steps = sorted(steps)
    bounds = [t for t, _ in steps] + [end]
    for i, (t, p) in enumerate(steps):
        a, b = max(t, start), min(bounds[i + 1], end)
        while a < b:
            hour = a - a % HOUR
            seg_end = min(b, hour + HOUR)
            out[hour] = out.get(hour, 0.0) + p * (seg_end - a) / 3.6e6
            a = seg_end
    return out


def merge_hourly(*series: Mapping[float, float]) -> dict[float, float]:
    out: dict[float, float] = {}
    for s in series:
        for t, v in s.items():
            out[t] = out.get(t, 0.0) + v
    return out
