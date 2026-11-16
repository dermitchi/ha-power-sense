"""Power Sense sensors: power, energy (total/day/month/year) and cost per device, plus an overview."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import DeviceTracker, Manager, PowerSenseConfigEntry
from .const import DOMAIN

DEVICE_KEYS = ("power", "energy", "energy_day", "energy_month", "energy_year", "cost_day", "cost_month", "cost_year")
OVERVIEW_KEYS = tuple(k for k in DEVICE_KEYS if k != "energy")


async def async_setup_entry(hass: HomeAssistant, entry: PowerSenseConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    manager = entry.runtime_data
    for subentry_id, tracker in manager.trackers.items():
        async_add_entities([DeviceSensor(tracker, key) for key in DEVICE_KEYS], config_subentry_id=subentry_id)
    async_add_entities([OverviewSensor(manager, key) for key in OVERVIEW_KEYS])


class _Sensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager: Manager, key: str, unique_id: str, signals: tuple[str, ...]) -> None:
        self.manager, self.key, self._signals = manager, key, signals
        self._attr_unique_id, self._attr_translation_key = unique_id, key
        kind, _, period = key.partition("_")
        self._period = period or None
        if key == "power":
            self._attr_device_class, self._attr_state_class = SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_suggested_display_precision = 1
        elif kind == "energy":
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING if key == "energy" else SensorStateClass.TOTAL
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_suggested_display_precision = 2
        else:
            self._attr_device_class, self._attr_state_class = SensorDeviceClass.MONETARY, SensorStateClass.TOTAL
            self._attr_suggested_display_precision = 2

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self._attr_device_class is SensorDeviceClass.MONETARY:
            return self.hass.config.currency
        return self._attr_native_unit_of_measurement

    @property
    def last_reset(self) -> datetime | None:
        if not self._period or self.key.startswith("power"):
            return None
        now = dt_util.now()
        start = {"day": now.replace(hour=0, minute=0, second=0, microsecond=0),
                 "month": now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
                 "year": now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)}[self._period]
        return start

    async def async_added_to_hass(self) -> None:
        for signal in self._signals:
            self.async_on_remove(async_dispatcher_connect(self.hass, signal, self.async_write_ha_state))


class DeviceSensor(_Sensor):
    def __init__(self, tracker: DeviceTracker, key: str) -> None:
        signals = (tracker.signal_tick,) + ((tracker.signal_power,) if key == "power" else ())
        super().__init__(tracker.manager, key, f"{tracker.subentry.subentry_id}_{key}", signals)
        self.tracker = tracker
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, tracker.subentry.subentry_id)}, name=tracker.subentry.title,
                                            manufacturer="Power Sense", model="Estimated" if tracker.entities else "Measured")

    @property
    def native_value(self) -> float:
        m = self.tracker.meter
        if self.key == "power":
            return round(m.power, 1)
        if self.key == "energy":
            return round(m.total, 3)
        energy = m.energy(self._period)
        return round(energy if self.key.startswith("energy") else energy * self.manager.price, 3 if self.key.startswith("energy") else 2)


class OverviewSensor(_Sensor):
    def __init__(self, manager: Manager, key: str) -> None:
        super().__init__(manager, key, f"{manager.entry.entry_id}_total_{key}",
                         (manager.signal_tick,) + ((manager.signal_power,) if key == "power" else ()))
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, manager.entry.entry_id)}, name="Power Sense", manufacturer="Power Sense")

    @property
    def native_value(self) -> float:
        meters = [t.meter for t in self.manager.trackers.values()]
        if self.key == "power":
            return round(sum(m.power for m in meters), 1)
        energy = sum(m.energy(self._period) for m in meters)
        return round(energy if self.key.startswith("energy") else energy * self.manager.price, 3 if self.key.startswith("energy") else 2)
