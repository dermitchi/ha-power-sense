"""Power Sense – energy use and cost per device, measured or estimated."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .calc import Meter, estimate_power, hourly_energy, merge_hourly, power_timeline
from .const import (
    CONF_DEFAULT_WATTS,
    CONF_ENTITIES,
    CONF_POWER_SENSORS,
    CONF_PRICE,
    CONF_SCALE_BRIGHTNESS,
    CONF_WATTS,
    DEFAULT_PRICE,
    DEFAULT_WATTS,
    DOMAIN,
    MIN_LEVEL,
    SAVE_INTERVAL_S,
    STORAGE_VERSION,
    TICK_INTERVAL_S,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]
SERVICE_IMPORT = "import_history"

type PowerSenseConfigEntry = ConfigEntry[Manager]


class DeviceTracker:
    """One configured device: reads its sources, keeps a Meter."""

    def __init__(self, manager: Manager, subentry: ConfigSubentry, meter: Meter) -> None:
        self.manager, self.subentry, self.meter = manager, subentry, meter
        d = subentry.data
        self.power_sensors: list[str] = list(d.get(CONF_POWER_SENSORS, []))
        self.entities: list[str] = list(d.get(CONF_ENTITIES, []))
        self.default_watts = float(d.get(CONF_DEFAULT_WATTS, DEFAULT_WATTS))
        self.scale = bool(d.get(CONF_SCALE_BRIGHTNESS, True))
        self.watts: dict[str, float] = {k: float(v) for k, v in (d.get(CONF_WATTS) or {}).items()}
        self.signal_power = f"{DOMAIN}_power_{subentry.subentry_id}"
        self.signal_tick = f"{DOMAIN}_tick_{subentry.subentry_id}"

    @property
    def sources(self) -> list[str]:
        return [*self.power_sensors, *self.entities]

    def read_power(self) -> float:
        hass = self.manager.hass
        total = 0.0
        for entity_id in self.power_sensors:
            state = hass.states.get(entity_id)
            try:
                value = float(state.state) if state else 0.0
            except ValueError:
                value = 0.0
            if state and state.attributes.get("unit_of_measurement") == "kW":
                value *= 1000
            total += value
        if self.entities:
            states = {}
            for entity_id in self.entities:
                state = hass.states.get(entity_id)
                states[entity_id] = (state is not None and state.state == "on", state.attributes.get("brightness") if state else None)
            total += estimate_power(states, self.watts, self.default_watts, self.scale, MIN_LEVEL)
        return total

    def refresh(self) -> None:
        self.meter.update(self.read_power(), time.time(), dt_util.get_default_time_zone())


class Manager:
    """Owns all trackers of the config entry, persists them and drives the updates."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass, self.entry = hass, entry
        self.store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.trackers: dict[str, DeviceTracker] = {}
        self.signal_power = f"{DOMAIN}_power_{entry.entry_id}"
        self.signal_tick = f"{DOMAIN}_tick_{entry.entry_id}"

    @property
    def price(self) -> float:
        return float(self.entry.options.get(CONF_PRICE, self.entry.data.get(CONF_PRICE, DEFAULT_PRICE)))

    async def async_setup(self) -> None:
        saved = await self.store.async_load() or {}
        for subentry_id, subentry in self.entry.subentries.items():
            self.trackers[subentry_id] = DeviceTracker(self, subentry, Meter.from_dict(saved.get(subentry_id)))
        for tracker in self.trackers.values():
            tracker.refresh()

        sources = sorted({s for t in self.trackers.values() for s in t.sources})
        if sources:
            self.entry.async_on_unload(async_track_state_change_event(self.hass, sources, self._source_changed))
        self.entry.async_on_unload(async_track_time_interval(self.hass, self._tick, timedelta(seconds=TICK_INTERVAL_S)))
        self.entry.async_on_unload(async_track_time_interval(self.hass, self._save, timedelta(seconds=SAVE_INTERVAL_S)))
        self.entry.async_on_unload(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._save))
        self.entry.async_on_unload(self.async_save)

    @callback
    def _source_changed(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        for tracker in self.trackers.values():
            if entity_id in tracker.sources:
                tracker.refresh()
                async_dispatcher_send(self.hass, tracker.signal_power)
        async_dispatcher_send(self.hass, self.signal_power)

    @callback
    def _tick(self, _now: datetime | None = None) -> None:
        for tracker in self.trackers.values():
            tracker.refresh()
            async_dispatcher_send(self.hass, tracker.signal_tick)
        async_dispatcher_send(self.hass, self.signal_tick)

    async def _save(self, _arg=None) -> None:
        await self.async_save()

    async def async_save(self) -> None:
        for tracker in self.trackers.values():
            tracker.meter.advance(time.time())
        await self.store.async_save({sid: t.meter.as_dict() for sid, t in self.trackers.items()})

    # ---- history import -------------------------------------------------

    async def async_import_history(self, name: str | None, days: int) -> dict[str, float]:
        """Rebuild the history of one or all devices from the recorder. Returns kWh imported per device."""
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import get_significant_states
        from homeassistant.components.recorder.models import StatisticMeanType
        from homeassistant.components.recorder.statistics import (
            async_import_statistics,
            statistics_during_period,
        )

        recorder = get_instance(self.hass)
        tz = dt_util.get_default_time_zone()
        now = time.time()
        registry = er.async_get(self.hass)
        done: dict[str, float] = {}

        for tracker in self.trackers.values():
            if name and tracker.subentry.title != name:
                continue
            tracker.refresh()
            live_since = tracker.meter.live_since or now
            end = live_since - live_since % 3600  # history ends where the live measurement begins
            start = end - days * 86400
            start_dt, end_dt = dt_util.utc_from_timestamp(start), dt_util.utc_from_timestamp(end)
            series = []

            if tracker.power_sensors:
                stats = await recorder.async_add_executor_job(
                    statistics_during_period, self.hass, start_dt, end_dt, set(tracker.power_sensors), "hour", None, {"mean"})
                hourly: dict[float, float] = {}
                for entity_id, rows in stats.items():
                    state = self.hass.states.get(entity_id)
                    factor = 1.0 if state and state.attributes.get("unit_of_measurement") == "kW" else 0.001
                    for row in rows:
                        if row.get("mean") is not None:
                            hourly[row["start"]] = hourly.get(row["start"], 0.0) + row["mean"] * factor
                series.append(hourly)

            if tracker.entities:
                history = await recorder.async_add_executor_job(
                    lambda: get_significant_states(self.hass, start_dt, end_dt, tracker.entities, None, True, False, False, False))
                events = []
                for entity_id, states in history.items():
                    for s in states:
                        events.append((max(s.last_updated.timestamp(), start), entity_id, s.state == "on", s.attributes.get("brightness")))
                power_of = lambda current: estimate_power(current, tracker.watts, tracker.default_watts, tracker.scale, MIN_LEVEL)  # noqa: E731
                series.append(hourly_energy(power_timeline(events, power_of), start, end))

            hourly_total = {t: v for t, v in merge_hourly(*series).items() if start <= t < end}
            delta = tracker.meter.apply_history(hourly_total, now, tz)

            entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{tracker.subentry.subentry_id}_energy")
            if entity_id:
                existing = (await recorder.async_add_executor_job(
                    statistics_during_period, self.hass, end_dt, None, {entity_id}, "hour", None, {"state", "sum"})).get(entity_id, [])
                rows, cum = [], 0.0
                for t in sorted(hourly_total):
                    cum += hourly_total[t]
                    rows.append({"start": dt_util.utc_from_timestamp(t), "state": cum, "sum": cum})
                rows += [{"start": dt_util.utc_from_timestamp(r["start"]), "state": (r["state"] or 0) + delta, "sum": (r["sum"] or 0) + delta}
                         for r in existing]
                async_import_statistics(
                    self.hass,
                    {"mean_type": StatisticMeanType.NONE, "has_sum": True, "name": None, "source": "recorder", "statistic_id": entity_id,
                     "unit_class": "energy", "unit_of_measurement": "kWh"},
                    rows)
            done[tracker.subentry.title] = round(tracker.meter.hist_total, 3)
            async_dispatcher_send(self.hass, tracker.signal_tick)

        await self.async_save()
        self._tick()
        return done


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def import_history(call: ServiceCall) -> None:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            return
        await entries[0].runtime_data.async_import_history(call.data.get("device"), call.data["days"])

    hass.services.async_register(
        DOMAIN, SERVICE_IMPORT, import_history,
        schema=vol.Schema({vol.Optional("device"): cv.string, vol.Optional("days", default=30): vol.All(vol.Coerce(int), vol.Range(min=1, max=730))}))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PowerSenseConfigEntry) -> bool:
    manager = Manager(hass, entry)
    await manager.async_setup()
    entry.runtime_data = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: PowerSenseConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PowerSenseConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
