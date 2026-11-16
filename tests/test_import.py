"""History import: statistics for measured devices, state history for estimated ones."""

from datetime import timedelta

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.power_sense.const import DOMAIN

from .test_integration import entity, setup_entry, sub


def hour_floor():
    return dt_util.utcnow().replace(minute=0, second=0, microsecond=0)


async def test_import_measured_history_and_replace(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    base = hour_floor() - timedelta(hours=6)
    async_import_statistics(
        hass,
        {"mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False, "name": None, "source": "recorder", "statistic_id": "sensor.p",
         "unit_class": "power", "unit_of_measurement": "W"},
        [{"start": base + timedelta(hours=i), "mean": 1000.0, "min": 1000.0, "max": 1000.0} for i in range(3)],
    )
    await async_wait_recording_done(hass)
    hass.states.async_set("sensor.p", "0", {"unit_of_measurement": "W"})
    await setup_entry(hass, [sub("P", {"power_sensors": ["sensor.p"]})])

    await hass.services.async_call(DOMAIN, "import_history", {"device": "P", "days": 2}, blocking=True)
    await async_wait_recording_done(hass)
    total = entity(hass, "s1_energy")
    assert float(hass.states.get(total).state) == 3.0
    assert float(hass.states.get(entity(hass, "s1_energy_year")).state) >= 0  # periods stay consistent

    stats = await hass.async_add_executor_job(
        lambda: statistics_during_period(hass, base - timedelta(hours=1), None, {total}, "hour", None, {"sum", "state"}))
    assert [round(r["sum"], 3) for r in stats[total]] == [1.0, 2.0, 3.0]

    # running it again replaces the first import instead of doubling it
    await hass.services.async_call(DOMAIN, "import_history", {"days": 2}, blocking=True)
    assert float(hass.states.get(total).state) == 3.0


async def test_import_estimated_history_from_states(recorder_mock, enable_custom_integrations, hass: HomeAssistant, freezer) -> None:
    start = hour_floor() - timedelta(hours=5)
    freezer.move_to(start)
    hass.states.async_set("light.a", "on", {"brightness": 255})
    await async_wait_recording_done(hass)
    freezer.move_to(start + timedelta(hours=2))
    hass.states.async_set("light.a", "off")
    await async_wait_recording_done(hass)
    freezer.move_to(hour_floor() + timedelta(minutes=5))
    data = {"entities": ["light.a"], "default_watts": 10, "scale_brightness": True}
    await setup_entry(hass, [sub("Lamp", data)])

    await hass.services.async_call(DOMAIN, "import_history", {"days": 1}, blocking=True)
    await async_wait_recording_done(hass)
    assert round(float(hass.states.get(entity(hass, "s1_energy")).state), 3) == 0.02  # 10 W for 2 h
