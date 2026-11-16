"""Config flow: one entry (electricity price) with one sub-entry per device."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    CONF_DEFAULT_WATTS,
    CONF_ENTITIES,
    CONF_NAME,
    CONF_POWER_SENSORS,
    CONF_PRICE,
    CONF_SCALE_BRIGHTNESS,
    CONF_WATTS,
    DEFAULT_PRICE,
    DEFAULT_WATTS,
    DOMAIN,
    ESTIMATE_DOMAINS,
    SUBENTRY_BULK,
    SUBENTRY_DEVICE,
)


def _price_schema(default: float) -> vol.Schema:
    return vol.Schema({vol.Required(CONF_PRICE, default=default): selector.NumberSelector(
        selector.NumberSelectorConfig(min=0, max=10, step=0.001, mode=selector.NumberSelectorMode.BOX))})


class PowerSenseConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Power Sense", data=user_input)
        return self.async_show_form(step_id="user", data_schema=_price_schema(DEFAULT_PRICE))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PriceOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_DEVICE: DeviceSubentryFlow, SUBENTRY_BULK: BulkSubentryFlow}


class PriceOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_PRICE, self.config_entry.data.get(CONF_PRICE, DEFAULT_PRICE))
        return self.async_show_form(step_id="init", data_schema=_price_schema(current))


class DeviceSubentryFlow(ConfigSubentryFlow):
    """Add or change a device: measured power sensors and/or estimated switchable entities."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def _current(self) -> dict[str, Any]:
        if self.source != SOURCE_RECONFIGURE:
            return {}
        sub = self._get_reconfigure_subentry()
        return {CONF_NAME: sub.title, **sub.data}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self._sources("user", user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self._sources("reconfigure", user_input)

    async def _sources(self, step_id: str, user_input: dict[str, Any] | None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_POWER_SENSORS) and not user_input.get(CONF_ENTITIES):
                errors["base"] = "no_source"
            else:
                self._data = dict(user_input)
                return await self.async_step_estimate() if user_input.get(CONF_ENTITIES) else self._finish()
        schema = vol.Schema({
            vol.Required(CONF_NAME): selector.TextSelector(),
            vol.Optional(CONF_POWER_SENSORS): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="power", multiple=True)),
            vol.Optional(CONF_ENTITIES): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=ESTIMATE_DOMAINS, multiple=True)),
        })
        return self.async_show_form(
            step_id=step_id, errors=errors,
            data_schema=self.add_suggested_values_to_schema(schema, user_input or self._current()))

    async def async_step_estimate(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            watts = user_input.get(CONF_WATTS) or {}
            if not isinstance(watts, dict) or any(k not in self._data[CONF_ENTITIES] or not isinstance(v, int | float) or v < 0
                                                  for k, v in watts.items()):
                errors[CONF_WATTS] = "invalid_watts"
            else:
                self._data.update(user_input)
                return self._finish()
        schema = vol.Schema({
            vol.Required(CONF_DEFAULT_WATTS, default=DEFAULT_WATTS): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=5000, step=0.1, unit_of_measurement="W", mode=selector.NumberSelectorMode.BOX)),
            vol.Required(CONF_SCALE_BRIGHTNESS, default=True): selector.BooleanSelector(),
            vol.Optional(CONF_WATTS): selector.ObjectSelector(),
        })
        return self.async_show_form(
            step_id="estimate", errors=errors,
            data_schema=self.add_suggested_values_to_schema(schema, user_input or self._current()))

    def _finish(self) -> SubentryFlowResult:
        data = {k: v for k, v in self._data.items() if k != CONF_NAME and v not in (None, [], {})}
        if not data.get(CONF_ENTITIES):  # entity-only settings make no sense without entities
            for key in (CONF_DEFAULT_WATTS, CONF_SCALE_BRIGHTNESS, CONF_WATTS):
                data.pop(key, None)
        title = self._data[CONF_NAME]
        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_and_abort(self._get_entry(), self._get_reconfigure_subentry(), title=title, data=data)
        return self.async_create_entry(title=title, data=data)


class BulkSubentryFlow(ConfigSubentryFlow):
    """Pick many power sensors at once: every sensor becomes its own device."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        entry = self._get_entry()
        if user_input is not None:
            used = {s for sub in entry.subentries.values() for s in sub.data.get(CONF_POWER_SENSORS, [])}
            added = 0
            for entity_id in user_input[CONF_POWER_SENSORS]:
                if entity_id in used:
                    continue
                self.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
                    data=MappingProxyType({CONF_POWER_SENSORS: [entity_id]}), subentry_type=SUBENTRY_DEVICE,
                    title=self._title(entity_id), unique_id=None))
                added += 1
            return self.async_abort(reason="bulk_added" if added else "nothing_new", description_placeholders={"count": str(added)})
        schema = vol.Schema({vol.Required(CONF_POWER_SENSORS): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="power", multiple=True))})
        return self.async_show_form(step_id="user", data_schema=schema)

    def _title(self, entity_id: str) -> str:
        """Name of the device the sensor belongs to, else the sensor's own name."""
        reg = er.async_get(self.hass).async_get(entity_id)
        device = dr.async_get(self.hass).async_get(reg.device_id) if reg and reg.device_id else None
        if device and (device.name_by_user or device.name):
            return device.name_by_user or device.name
        state = self.hass.states.get(entity_id)
        return state.name if state else entity_id
