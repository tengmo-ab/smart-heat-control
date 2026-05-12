"""Config flow for the Smart Heat Control integration.

The flow walks the user through six steps, each binding entities in the
caller's Home Assistant to *roles* the controller understands. Optional roles
can be left empty — the controller skips branches that depend on absent roles.

    user        -> heating_system (required core)
    heating_system -> power
    power       -> pricing
    pricing     -> weather
    weather     -> solar
    solar       -> defaults
    defaults    -> create_entry

OptionsFlow re-uses the same step machinery so the user can re-bind any role
after install.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AUX_POWER_SENSOR,
    CONF_BATTERY_DISCHARGING_BINARY_SENSOR,
    CONF_BRIDGE_TO_SOLAR_BINARY_SENSOR,
    CONF_CLIMATE_ENTITY,
    CONF_COMPRESSOR_POWER_SENSOR,
    CONF_DEFAULT_HEAT_CURVE,
    CONF_DEFAULT_HW_TEMP,
    CONF_DEFAULT_INDOOR_TEMP,
    CONF_HEAT_CURVE_NUMBER,
    CONF_HOT_WATER_EXTRA_SWITCH,
    CONF_HOT_WATER_SETPOINT_NUMBER,
    CONF_HOT_WATER_TEMP_SENSOR,
    CONF_INDOOR_TEMP_SENSOR,
    CONF_IS_SUNNY_DAY_BINARY_SENSOR,
    CONF_LEGIONELLA_DURATION_HOURS,
    CONF_LEGIONELLA_MAX_DAYS,
    CONF_LEGIONELLA_MIN_DAYS,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_PRICE_THRESHOLD,
    CONF_PRICE_TODAY_SENSOR,
    CONF_PUMP_ACTIVITY_SENSOR,
    CONF_PV_EXCESS_BINARY_SENSOR,
    CONF_SOLAR_FORECAST_TODAY_SENSOR,
    CONF_WEATHER_FORECAST_ENTITY,
    DEFAULT_HEAT_CURVE,
    DEFAULT_HW_TEMP,
    DEFAULT_INDOOR_TEMP,
    DEFAULT_LEGIONELLA_DURATION_HOURS,
    DEFAULT_LEGIONELLA_MAX_DAYS,
    DEFAULT_LEGIONELLA_MIN_DAYS,
    DEFAULT_PRICE_THRESHOLD,
    DOMAIN,
    MAX_CLIMATE_TEMP,
    MAX_HEAT_CURVE,
    MAX_HW_TEMP,
    MIN_CLIMATE_TEMP,
    MIN_HEAT_CURVE,
    MIN_HW_TEMP,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Selector helpers
# ---------------------------------------------------------------------------
def _entity(domain: str | list[str], **extra: Any) -> selector.EntitySelector:
    config = selector.EntitySelectorConfig(domain=domain, **extra)
    return selector.EntitySelector(config)


def _temperature_sensor() -> selector.EntitySelector:
    return _entity("sensor", device_class="temperature")


def _power_sensor() -> selector.EntitySelector:
    return _entity("sensor", device_class="power")


def _monetary_sensor() -> selector.EntitySelector:
    return _entity("sensor", device_class="monetary")


def _number_box(
    *, mn: float, mx: float, step: float = 0.1, unit: str | None = None
) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=mn,
            max=mx,
            step=step,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement=unit,
        )
    )


# ---------------------------------------------------------------------------
# Per-step schemas. Optional roles default to vol.UNDEFINED so the picker
# starts empty; the controller treats missing keys as "feature disabled".
# ---------------------------------------------------------------------------
def _heating_system_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_CLIMATE_ENTITY,
                default=defaults.get(CONF_CLIMATE_ENTITY, vol.UNDEFINED),
            ): _entity("climate"),
            vol.Required(
                CONF_OUTDOOR_TEMP_SENSOR,
                default=defaults.get(CONF_OUTDOOR_TEMP_SENSOR, vol.UNDEFINED),
            ): _temperature_sensor(),
            vol.Optional(
                CONF_INDOOR_TEMP_SENSOR,
                default=defaults.get(CONF_INDOOR_TEMP_SENSOR, vol.UNDEFINED),
            ): _temperature_sensor(),
            vol.Optional(
                CONF_HEAT_CURVE_NUMBER,
                default=defaults.get(CONF_HEAT_CURVE_NUMBER, vol.UNDEFINED),
            ): _entity("number"),
            vol.Optional(
                CONF_HOT_WATER_SETPOINT_NUMBER,
                default=defaults.get(
                    CONF_HOT_WATER_SETPOINT_NUMBER, vol.UNDEFINED
                ),
            ): _entity("number"),
            vol.Optional(
                CONF_HOT_WATER_EXTRA_SWITCH,
                default=defaults.get(CONF_HOT_WATER_EXTRA_SWITCH, vol.UNDEFINED),
            ): _entity("switch"),
            vol.Optional(
                CONF_HOT_WATER_TEMP_SENSOR,
                default=defaults.get(CONF_HOT_WATER_TEMP_SENSOR, vol.UNDEFINED),
            ): _temperature_sensor(),
            vol.Optional(
                CONF_PUMP_ACTIVITY_SENSOR,
                default=defaults.get(CONF_PUMP_ACTIVITY_SENSOR, vol.UNDEFINED),
            ): _entity("sensor"),
        }
    )


def _power_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_COMPRESSOR_POWER_SENSOR,
                default=defaults.get(CONF_COMPRESSOR_POWER_SENSOR, vol.UNDEFINED),
            ): _power_sensor(),
            vol.Optional(
                CONF_AUX_POWER_SENSOR,
                default=defaults.get(CONF_AUX_POWER_SENSOR, vol.UNDEFINED),
            ): _power_sensor(),
        }
    )


def _pricing_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_PRICE_SENSOR,
                default=defaults.get(CONF_PRICE_SENSOR, vol.UNDEFINED),
            ): _monetary_sensor(),
            vol.Optional(
                CONF_PRICE_TODAY_SENSOR,
                default=defaults.get(CONF_PRICE_TODAY_SENSOR, vol.UNDEFINED),
            ): _entity("sensor"),
        }
    )


def _weather_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_WEATHER_FORECAST_ENTITY,
                default=defaults.get(CONF_WEATHER_FORECAST_ENTITY, vol.UNDEFINED),
            ): _entity(["weather", "sensor"]),
        }
    )


def _solar_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_PV_EXCESS_BINARY_SENSOR,
                default=defaults.get(CONF_PV_EXCESS_BINARY_SENSOR, vol.UNDEFINED),
            ): _entity("binary_sensor"),
            vol.Optional(
                CONF_SOLAR_FORECAST_TODAY_SENSOR,
                default=defaults.get(
                    CONF_SOLAR_FORECAST_TODAY_SENSOR, vol.UNDEFINED
                ),
            ): _entity("sensor"),
            vol.Optional(
                CONF_BATTERY_DISCHARGING_BINARY_SENSOR,
                default=defaults.get(
                    CONF_BATTERY_DISCHARGING_BINARY_SENSOR, vol.UNDEFINED
                ),
            ): _entity("binary_sensor"),
            vol.Optional(
                CONF_BRIDGE_TO_SOLAR_BINARY_SENSOR,
                default=defaults.get(
                    CONF_BRIDGE_TO_SOLAR_BINARY_SENSOR, vol.UNDEFINED
                ),
            ): _entity("binary_sensor"),
            vol.Optional(
                CONF_IS_SUNNY_DAY_BINARY_SENSOR,
                default=defaults.get(
                    CONF_IS_SUNNY_DAY_BINARY_SENSOR, vol.UNDEFINED
                ),
            ): _entity("binary_sensor"),
        }
    )


def _defaults_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_DEFAULT_INDOOR_TEMP,
                default=defaults.get(CONF_DEFAULT_INDOOR_TEMP, DEFAULT_INDOOR_TEMP),
            ): _number_box(mn=MIN_CLIMATE_TEMP, mx=MAX_CLIMATE_TEMP, unit="°C"),
            vol.Required(
                CONF_DEFAULT_HEAT_CURVE,
                default=defaults.get(CONF_DEFAULT_HEAT_CURVE, DEFAULT_HEAT_CURVE),
            ): _number_box(mn=MIN_HEAT_CURVE, mx=MAX_HEAT_CURVE),
            vol.Required(
                CONF_DEFAULT_HW_TEMP,
                default=defaults.get(CONF_DEFAULT_HW_TEMP, DEFAULT_HW_TEMP),
            ): _number_box(mn=MIN_HW_TEMP, mx=MAX_HW_TEMP, unit="°C"),
            vol.Required(
                CONF_PRICE_THRESHOLD,
                default=defaults.get(CONF_PRICE_THRESHOLD, DEFAULT_PRICE_THRESHOLD),
            ): _number_box(mn=0, mx=1000, step=1),
            vol.Required(
                CONF_LEGIONELLA_MIN_DAYS,
                default=defaults.get(
                    CONF_LEGIONELLA_MIN_DAYS, DEFAULT_LEGIONELLA_MIN_DAYS
                ),
            ): _number_box(mn=1, mx=30, step=1, unit="d"),
            vol.Required(
                CONF_LEGIONELLA_MAX_DAYS,
                default=defaults.get(
                    CONF_LEGIONELLA_MAX_DAYS, DEFAULT_LEGIONELLA_MAX_DAYS
                ),
            ): _number_box(mn=1, mx=30, step=1, unit="d"),
            vol.Required(
                CONF_LEGIONELLA_DURATION_HOURS,
                default=defaults.get(
                    CONF_LEGIONELLA_DURATION_HOURS,
                    DEFAULT_LEGIONELLA_DURATION_HOURS,
                ),
            ): _number_box(mn=1, mx=12, step=1, unit="h"),
        }
    )


# ---------------------------------------------------------------------------
# Initial install flow
# ---------------------------------------------------------------------------
class SmartHeatControlConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the install-time config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return SmartHeatControlOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_heating_system(user_input)

    async def async_step_heating_system(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_power()
        return self.async_show_form(
            step_id="heating_system",
            data_schema=_heating_system_schema(self._data),
        )

    async def async_step_power(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_pricing()
        return self.async_show_form(
            step_id="power", data_schema=_power_schema(self._data)
        )

    async def async_step_pricing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_weather()
        return self.async_show_form(
            step_id="pricing", data_schema=_pricing_schema(self._data)
        )

    async def async_step_weather(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_solar()
        return self.async_show_form(
            step_id="weather", data_schema=_weather_schema(self._data)
        )

    async def async_step_solar(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_defaults()
        return self.async_show_form(
            step_id="solar", data_schema=_solar_schema(self._data)
        )

    async def async_step_defaults(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="Smart Heat Control", data=self._data)
        return self.async_show_form(
            step_id="defaults", data_schema=_defaults_schema(self._data)
        )


# ---------------------------------------------------------------------------
# Options flow — re-uses the same step machinery so the user can rebind roles
# or tweak defaults after install.
# ---------------------------------------------------------------------------
class SmartHeatControlOptionsFlow(config_entries.OptionsFlow):
    """Handle re-configuration after install."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # Seed with the merged view (entry data + previously-saved options)
        # so the form pre-fills the user's last choices.
        self._data = {**self.config_entry.data, **self.config_entry.options}
        return await self.async_step_heating_system()

    async def async_step_heating_system(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_power()
        return self.async_show_form(
            step_id="heating_system",
            data_schema=_heating_system_schema(self._data),
        )

    async def async_step_power(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_pricing()
        return self.async_show_form(
            step_id="power", data_schema=_power_schema(self._data)
        )

    async def async_step_pricing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_weather()
        return self.async_show_form(
            step_id="pricing", data_schema=_pricing_schema(self._data)
        )

    async def async_step_weather(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_solar()
        return self.async_show_form(
            step_id="weather", data_schema=_weather_schema(self._data)
        )

    async def async_step_solar(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_defaults()
        return self.async_show_form(
            step_id="solar", data_schema=_solar_schema(self._data)
        )

    async def async_step_defaults(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="", data=self._data)
        return self.async_show_form(
            step_id="defaults", data_schema=_defaults_schema(self._data)
        )
