"""Integration tests: config flow, devices as sub-entries and the resulting sensors."""

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.power_sense.const import DOMAIN

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def setup_entry(hass: HomeAssistant, subentries: list[ConfigSubentryData], price=0.30) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={"price": price}, subentries_data=subentries)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def sub(title, data, sid="s1"):
    return ConfigSubentryData(data=data, subentry_type="device", title=title, unique_id=None, subentry_id=sid)


def entity(hass, entry_unique):
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, entry_unique)


async def test_user_flow_creates_entry_with_price(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"price": 0.32})
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["data"] == {"price": 0.32}
    # only one instance
    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert again["type"] is FlowResultType.ABORT


async def test_add_measured_device_via_subentry_flow(hass: HomeAssistant) -> None:
    entry = await setup_entry(hass, [])
    r = await hass.config_entries.subentries.async_init((entry.entry_id, "device"), context={"source": "user"})
    assert r["type"] is FlowResultType.FORM
    r = await hass.config_entries.subentries.async_configure(r["flow_id"], {"name": "Dryer", "power_sensors": ["sensor.dryer_power"]})
    assert r["type"] is FlowResultType.CREATE_ENTRY and r["title"] == "Dryer"
    assert r["data"] == {"power_sensors": ["sensor.dryer_power"]}


async def test_add_estimated_device_asks_for_watts_and_validates(hass: HomeAssistant) -> None:
    entry = await setup_entry(hass, [])
    r = await hass.config_entries.subentries.async_init((entry.entry_id, "device"), context={"source": "user"})
    r = await hass.config_entries.subentries.async_configure(r["flow_id"], {"name": "Lamps", "entities": ["light.a", "light.b"]})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "estimate"
    bad = await hass.config_entries.subentries.async_configure(
        r["flow_id"], {"default_watts": 9, "scale_brightness": True, "watts": {"light.not_selected": 5}})
    assert bad["errors"] == {"watts": "invalid_watts"}
    ok = await hass.config_entries.subentries.async_configure(
        r["flow_id"], {"default_watts": 9, "scale_brightness": True, "watts": {"light.a": 5}})
    assert ok["type"] is FlowResultType.CREATE_ENTRY
    assert ok["data"]["entities"] == ["light.a", "light.b"] and ok["data"]["watts"] == {"light.a": 5}


async def test_no_source_is_rejected(hass: HomeAssistant) -> None:
    entry = await setup_entry(hass, [])
    r = await hass.config_entries.subentries.async_init((entry.entry_id, "device"), context={"source": "user"})
    r = await hass.config_entries.subentries.async_configure(r["flow_id"], {"name": "Nothing"})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "no_source"}


async def test_measured_device_sensors_follow_the_power_sensor(hass: HomeAssistant, freezer) -> None:
    hass.states.async_set("sensor.dryer_power", "1000", {"unit_of_measurement": "W"})
    entry = await setup_entry(hass, [sub("Dryer", {"power_sensors": ["sensor.dryer_power"]})], price=0.30)
    power = entity(hass, "s1_power")
    assert hass.states.get(power).state == "1000.0"
    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entity(hass, "s1_energy")).state == "0.1"  # 1 kW for 6 min
    assert hass.states.get(entity(hass, "s1_energy_day")).state == "0.1"
    assert hass.states.get(entity(hass, "s1_cost_day")).state == "0.03"
    assert hass.states.get("sensor.power_sense_power").state == "1000.0"  # overview
    # kW sensors are converted
    hass.states.async_set("sensor.dryer_power", "0.5", {"unit_of_measurement": "kW"})
    await hass.async_block_till_done()
    assert hass.states.get(power).state == "500.0"
    assert entry.runtime_data.price == 0.30


async def test_estimated_device_scales_with_brightness(hass: HomeAssistant) -> None:
    hass.states.async_set("light.a", "on", {"brightness": 255})
    hass.states.async_set("light.b", "on", {"brightness": 51})
    hass.states.async_set("switch.c", "off")
    data = {"entities": ["light.a", "light.b", "switch.c"], "default_watts": 10, "scale_brightness": True, "watts": {"light.a": 20}}
    await setup_entry(hass, [sub("Lamps", data)])
    assert hass.states.get(entity(hass, "s1_power")).state == "22.0"  # a: 20, b: 10 * 0.2, c off
    hass.states.async_set("switch.c", "on")
    await hass.async_block_till_done()
    assert hass.states.get(entity(hass, "s1_power")).state == "32.0"


async def test_mixed_device_adds_measured_and_estimated(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.plug", "4", {"unit_of_measurement": "W"})
    hass.states.async_set("light.a", "on", {"brightness": 255})
    data = {"power_sensors": ["sensor.plug"], "entities": ["light.a"], "default_watts": 9, "scale_brightness": True}
    await setup_entry(hass, [sub("Lamps", data)])
    assert hass.states.get(entity(hass, "s1_power")).state == "13.0"


async def test_totals_survive_a_restart(hass: HomeAssistant, freezer) -> None:
    hass.states.async_set("sensor.p", "600", {"unit_of_measurement": "W"})
    entry = await setup_entry(hass, [sub("P", {"power_sensors": ["sensor.p"]})])
    freezer.tick(timedelta(minutes=10))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entity(hass, "s1_energy")).state == "0.1"
    assert await hass.config_entries.async_unload(entry.entry_id)  # saves
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(entity(hass, "s1_energy")).state == "0.1"


async def test_bulk_add_creates_one_device_per_sensor_and_skips_used(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.a_power", "5", {"unit_of_measurement": "W", "friendly_name": "Plug A"})
    hass.states.async_set("sensor.b_power", "7", {"unit_of_measurement": "W", "friendly_name": "Plug B"})
    entry = await setup_entry(hass, [sub("Existing", {"power_sensors": ["sensor.a_power"]})])
    r = await hass.config_entries.subentries.async_init((entry.entry_id, "bulk"), context={"source": "user"})
    r = await hass.config_entries.subentries.async_configure(r["flow_id"], {"power_sensors": ["sensor.a_power", "sensor.b_power"]})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "bulk_added" and r["description_placeholders"] == {"count": "1"}
    await hass.async_block_till_done()
    assert sorted(s.title for s in entry.subentries.values()) == ["Existing", "Plug B"]
    again = await hass.config_entries.subentries.async_init((entry.entry_id, "bulk"), context={"source": "user"})
    again = await hass.config_entries.subentries.async_configure(again["flow_id"], {"power_sensors": ["sensor.a_power", "sensor.b_power"]})
    assert again["reason"] == "nothing_new"


async def test_media_players_use_on_and_standby_watts(hass: HomeAssistant) -> None:
    hass.states.async_set("media_player.tv", "playing")
    hass.states.async_set("media_player.box", "off")
    hass.states.async_set("media_player.gone", "unavailable")
    hass.states.async_set("camera.garden", "idle")
    data = {"entities": ["media_player.tv", "media_player.box", "media_player.gone", "camera.garden"], "default_watts": 10,
            "standby_watts": 2, "scale_brightness": True,
            "watts": {"media_player.tv": {"on": 80, "standby": 0.5}, "camera.garden": {"on": 9, "standby": 9}}}
    await setup_entry(hass, [sub("TV", data)])
    assert hass.states.get(entity(hass, "s1_power")).state == "91.0"  # tv 80 + box standby 2 + unavailable 0 + camera 9 (idle uses its standby value 9)
