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

Debug notes
-----------
Every step handler is wrapped in `_run_step()`, which logs the full traceback
of *any* unexpected exception via `_LOGGER.exception()` — so when a user sees
"Unknown error occurred" in the UI, the matching traceback is sitting in
the HA log under the logger name `custom_components.smart_heat_control.config_flow`.

To raise the log level without restarting HA::

    service: logger.set_level
    data:
      custom_components.smart_heat_control: debug
"""
from __future__ import annotations

import logging
from typing import Any, Callable

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


def _price_sensor() -> selector.EntitySelector:
    # Do NOT filter by device_class — Nord Pool, Tibber and template sensors
    # rarely set device_class=monetary; a plain sensor selector works for all.
    return _entity("sensor")


def _number_box(
    *, mn: float, mx: float, step: float = 0.1, unit: str | None = None
) -> selector.NumberSelector:
    """Build a NumberSelector in BOX mode.

    NOTE: `unit_of_measurement` must be **omitted** (not set to None) when no
    unit is desired — HA's selector config schema validates that key as `str`
    and rejects None with `vol.Invalid`. Passing it as None used to bubble up
    as "Unknown error occurred" on the *previous* config-flow step (because
    the schema for the next step couldn't be built).
    """
    kwargs: dict[str, Any] = {
        "min": float(mn),
        "max": float(mx),
        "step": float(step),
        "mode": selector.NumberSelectorMode.BOX,
    }
    if unit:
        kwargs["unit_of_measurement"] = unit
    return selector.NumberSelector(selector.NumberSelectorConfig(**kwargs))


# ---------------------------------------------------------------------------
# Per-step schemas. Optional roles use default=vol.UNDEFINED so the picker
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
            vol.Optional(
                CONF_PRICE_SENSOR,
                default=defaults.get(CONF_PRICE_SENSOR, vol.UNDEFINED),
            ): _price_sensor(),
            vol.Optional(
                CONF_PRICE_TODAY_SENSOR,
                default=defaults.get(CONF_PRICE_TODAY_SENSOR, vol.UNDEFINED),
            ): _price_sensor(),
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
            ): _number_box(mn=0, mx=1000, step=1, unit="öre/kWh"),
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
# Step ordering + shared run logic
# ---------------------------------------------------------------------------
# (step_id, schema_builder, next_step_id_or_None). Both ConfigFlow and
# OptionsFlow walk this list, so the order — and any error messaging — is
# defined in exactly one place.
_STEPS: tuple[tuple[str, Callable[[dict[str, Any]], vol.Schema], str | None], ...] = (
    ("heating_system", _heating_system_schema, "power"),
    ("power", _power_schema, "pricing"),
    ("pricing", _pricing_schema, "weather"),
    ("weather", _weather_schema, "solar"),
    ("solar", _solar_schema, "defaults"),
    ("defaults", _defaults_schema, None),
)
_STEP_INDEX: dict[str, tuple[Callable[[dict[str, Any]], vol.Schema], str | None]] = {
    step_id: (schema_fn, next_id) for step_id, schema_fn, next_id in _STEPS
}


def _clean_user_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Remove None/empty values from optional fields.

    HA's frontend sends `null` for unfilled optional EntitySelector fields.
    Storing those in `self._data` would later contaminate `defaults.get()`
    lookups in subsequent steps, so strip them here.
    """
    return {k: v for k, v in user_input.items() if v not in (None, "")}


async def _run_step(
    flow: "_FlowBase",
    step_id: str,
    user_input: dict[str, Any] | None,
) -> ConfigFlowResult:
    """Run one config-flow step with comprehensive error logging.

    Any unhandled exception is logged with full traceback under this module's
    logger, then surfaced to the user via the standard 'unknown' error key —
    so the HA log line is the canonical source of truth for what went wrong.
    """
    schema_fn, next_id = _STEP_INDEX[step_id]

    if user_input is not None:
        try:
            cleaned = _clean_user_input(user_input)
            flow._data.update(cleaned)
            _LOGGER.debug(
                "Smart Heat Control: step '%s' accepted %d key(s); merged data now has %d key(s)",
                step_id,
                len(cleaned),
                len(flow._data),
            )
        except Exception:  # noqa: BLE001 — we want truly any exception logged
            _LOGGER.exception(
                "Smart Heat Control: error while merging user input on step '%s' "
                "(input keys=%s)",
                step_id,
                sorted(user_input.keys()),
            )
            return _safe_show_form(flow, step_id, schema_fn, error_key="unknown")

        # Advance to next step (or finish).
        if next_id is None:
            return flow._finish_flow()
        try:
            return await getattr(flow, f"async_step_{next_id}")()
        except Exception:
            _LOGGER.exception(
                "Smart Heat Control: failed to transition from '%s' to '%s' "
                "— likely a schema-build failure in the next step",
                step_id,
                next_id,
            )
            # Re-show current step so the user isn't trapped on a blank screen.
            return _safe_show_form(flow, step_id, schema_fn, error_key="unknown")

    return _safe_show_form(flow, step_id, schema_fn)


def _safe_show_form(
    flow: "_FlowBase",
    step_id: str,
    schema_fn: Callable[[dict[str, Any]], vol.Schema],
    *,
    error_key: str | None = None,
) -> ConfigFlowResult:
    """Build the schema and show the form, aborting cleanly if the schema fails.

    A failure here means the integration code itself is broken (not user input),
    so we abort with a specific reason rather than silently re-rendering.
    """
    try:
        schema = schema_fn(flow._data)
    except Exception:
        _LOGGER.exception(
            "Smart Heat Control: schema builder for step '%s' raised — "
            "the integration cannot present this step. This is a bug in "
            "custom_components/smart_heat_control/config_flow.py; please "
            "include the traceback above when reporting the issue.",
            step_id,
        )
        return flow.async_abort(reason="schema_build_failed")

    errors = {"base": error_key} if error_key else None
    return flow.async_show_form(
        step_id=step_id,
        data_schema=schema,
        errors=errors,
    )


class _FlowBase:
    """Tiny protocol describing the surface both flow classes expose to helpers."""

    _data: dict[str, Any]

    def async_show_form(self, **kwargs: Any) -> ConfigFlowResult:  # pragma: no cover
        raise NotImplementedError

    def async_abort(self, *, reason: str) -> ConfigFlowResult:  # pragma: no cover
        raise NotImplementedError

    def _finish_flow(self) -> ConfigFlowResult:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Initial install flow
# ---------------------------------------------------------------------------
class SmartHeatControlConfigFlow(config_entries.ConfigFlow, _FlowBase, domain=DOMAIN):
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

    def _finish_flow(self) -> ConfigFlowResult:
        _LOGGER.info(
            "Smart Heat Control: completing install with %d configured role(s)",
            len(self._data),
        )
        return self.async_create_entry(title="Smart Heat Control", data=self._data)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("Smart Heat Control: starting install config flow")
        return await self.async_step_heating_system(user_input)

    async def async_step_heating_system(self, user_input=None):
        return await _run_step(self, "heating_system", user_input)

    async def async_step_power(self, user_input=None):
        return await _run_step(self, "power", user_input)

    async def async_step_pricing(self, user_input=None):
        return await _run_step(self, "pricing", user_input)

    async def async_step_weather(self, user_input=None):
        return await _run_step(self, "weather", user_input)

    async def async_step_solar(self, user_input=None):
        return await _run_step(self, "solar", user_input)

    async def async_step_defaults(self, user_input=None):
        return await _run_step(self, "defaults", user_input)


# ---------------------------------------------------------------------------
# Options flow — re-uses the same step machinery so the user can rebind roles
# or tweak defaults after install.
# ---------------------------------------------------------------------------
class SmartHeatControlOptionsFlow(config_entries.OptionsFlow, _FlowBase):
    """Handle re-configuration after install."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def _finish_flow(self) -> ConfigFlowResult:
        _LOGGER.info(
            "Smart Heat Control: saving options with %d configured role(s)",
            len(self._data),
        )
        return self.async_create_entry(title="", data=self._data)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # Seed with merged view (entry data + previously-saved options) so the
        # form pre-fills the user's last choices.
        self._data = {**self.config_entry.data, **self.config_entry.options}
        _LOGGER.debug(
            "Smart Heat Control: opening options flow with %d existing key(s)",
            len(self._data),
        )
        return await self.async_step_heating_system()

    async def async_step_heating_system(self, user_input=None):
        return await _run_step(self, "heating_system", user_input)

    async def async_step_power(self, user_input=None):
        return await _run_step(self, "power", user_input)

    async def async_step_pricing(self, user_input=None):
        return await _run_step(self, "pricing", user_input)

    async def async_step_weather(self, user_input=None):
        return await _run_step(self, "weather", user_input)

    async def async_step_solar(self, user_input=None):
        return await _run_step(self, "solar", user_input)

    async def async_step_defaults(self, user_input=None):
        return await _run_step(self, "defaults", user_input)
