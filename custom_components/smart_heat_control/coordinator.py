"""DataUpdateCoordinator for Smart Heat Control."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .computed import assess_health, compute
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
    CONTROL_INTERVAL_SECONDS,
    DEFAULT_HEAT_CURVE,
    DEFAULT_HW_TEMP,
    DEFAULT_INDOOR_TEMP,
    DEFAULT_LEGIONELLA_DURATION_HOURS,
    DEFAULT_LEGIONELLA_MAX_DAYS,
    DEFAULT_LEGIONELLA_MIN_DAYS,
    DEFAULT_PRICE_THRESHOLD,
    DOMAIN,
    HW_REDUCTION_HEATING_TIMEOUT_HOURS,
    HW_REDUCTION_NO_HEATING_TIMEOUT_MINUTES,
    HW_REDUCTION_TRIGGER_HOURS,
    QUARTERS_PER_DAY,
    WRITE_SETTLE_DELAY_SECONDS,
)
from .controller import decide
from .models import ForecastDay, FullDecision, Inputs, Mode, PumpActivity

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HA state-reading utilities
# ---------------------------------------------------------------------------

def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return None if f != f else f  # filter NaN
    except (ValueError, TypeError):
        return None


def _read_float(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown", ""):
        return None
    return _to_float(state.state)


def _read_bool(hass: HomeAssistant, entity_id: str | None) -> bool:
    if not entity_id:
        return False
    state = hass.states.get(entity_id)
    return state is not None and state.state == "on"


def _read_pump_activity(hass: HomeAssistant, entity_id: str | None) -> PumpActivity | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    try:
        return PumpActivity(state.state)
    except ValueError:
        return None


def _parse_price_quarters(hass: HomeAssistant, entity_id: str | None) -> tuple[float, ...]:
    """Parse the Nord Pool ``prices`` attribute into 96 floats (quarter resolution).

    Nord Pool exposes ``prices`` as a Python ``list[float]`` of length 96 —
    the comma-separated rendering you see in HA's developer-tools is the UI
    serialising the list, not the actual attribute value. Some other vendors
    expose it as a CSV string, so handle both.

    Also accepts the alternative ``data`` attribute (list of
    ``{start, end, price}`` dicts at 15-min resolution) when ``prices`` is
    missing.
    """
    if not entity_id:
        return ()
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown"):
        return ()

    raw = state.attributes.get("prices")
    parts: list[float] = []
    if isinstance(raw, (list, tuple)):
        for p in raw:
            if p is None:
                continue
            try:
                parts.append(float(p))
            except (ValueError, TypeError):
                return ()
    elif raw:
        try:
            parts = [float(p.strip()) for p in str(raw).split(",") if p.strip()]
        except (ValueError, TypeError):
            return ()

    # Fallback: parse from ``data`` attribute (list of dicts with ``price``)
    if not parts:
        data = state.attributes.get("data")
        if isinstance(data, list):
            try:
                parts = [float(entry["price"]) for entry in data if "price" in entry]
            except (ValueError, TypeError, KeyError):
                return ()

    return tuple(parts) if len(parts) == QUARTERS_PER_DAY else ()


def _parse_forecast(
    hass: HomeAssistant, entity_id: str | None, tz: ZoneInfo
) -> tuple[ForecastDay, ...]:
    """Parse the SMHI/generic forecast attribute into ForecastDay tuples.

    SMHI's daily forecast emits *two* entries per "today": a midnight-local
    snapshot of the current weather (datetime at previous-day 22:00 UTC for
    CEST) followed by the daily summary at local 14:00 (12:00 UTC). Both map
    to the same local date, so a naïve "first entry whose date >= today"
    lookup picks the snapshot — making future_highest_temp reflect the
    *current* outdoor temperature instead of the day's high.

    Fix: deduplicate by local date, preferring the entry whose local hour is
    closest to noon (the daily-summary anchor). Single-entry-per-day
    providers are unaffected since their one entry is also the closest-to-noon.
    """
    if not entity_id:
        return ()
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown"):
        return ()
    raw_list = state.attributes.get("forecast", [])
    if not isinstance(raw_list, list):
        return ()

    best: dict[date, tuple[int, ForecastDay]] = {}
    for entry in raw_list:
        try:
            dt_str = entry.get("datetime", "")
            local_dt = datetime.fromisoformat(str(dt_str)).astimezone(tz)
        except (ValueError, TypeError, AttributeError):
            continue
        local_date = local_dt.date()
        # Lower score = closer to local noon; daily summaries score 2 (hour=14),
        # midnight snapshots score 12 (hour=0).
        score = abs(local_dt.hour - 12)
        if local_date in best and score >= best[local_date][0]:
            continue
        best[local_date] = (
            score,
            ForecastDay(
                day=local_date,
                temperature=_to_float(entry.get("temperature")),
                templow=_to_float(entry.get("templow")),
                condition=entry.get("condition"),
            ),
        )

    return tuple(best[d][1] for d in sorted(best.keys()))


def _read_sun(hass: HomeAssistant) -> tuple[bool, datetime | None]:
    state = hass.states.get("sun.sun")
    if state is None:
        return False, None
    is_up = state.state == "above_horizon"
    next_rising: datetime | None = None
    rising_str = state.attributes.get("next_rising")
    if rising_str:
        try:
            dt = datetime.fromisoformat(str(rising_str))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=dt_util.UTC)
            next_rising = dt
        except (ValueError, TypeError):
            pass
    return is_up, next_rising


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class SmartHeatControlCoordinator(DataUpdateCoordinator[FullDecision]):
    """Runs the decision cascade on a 5-min cadence + state-change triggers."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=CONTROL_INTERVAL_SECONDS),
        )
        self.entry = entry
        cfg: dict[str, Any] = {**entry.data, **entry.options}

        # -------------------------------------------------------------------
        # Integration-owned settings — written by entity callbacks.
        # Initial values come from config-flow defaults; entities restore their
        # own state from HA on restart and push updates back here.
        # -------------------------------------------------------------------
        self.master_enabled: bool = False
        self.cheap_price_enabled: bool = True
        self.price_peak_enabled: bool = True
        self.weather_anticipation_enabled: bool = True
        self.legionella_boost_enabled: bool = False
        self.survive_solar_enabled: bool = False

        self.default_indoor_temp: float = float(
            cfg.get(CONF_DEFAULT_INDOOR_TEMP, DEFAULT_INDOOR_TEMP)
        )
        self.default_heat_curve: float = float(
            cfg.get(CONF_DEFAULT_HEAT_CURVE, DEFAULT_HEAT_CURVE)
        )
        self.default_hw_temp: float = float(
            cfg.get(CONF_DEFAULT_HW_TEMP, DEFAULT_HW_TEMP)
        )
        self.price_threshold: float = float(
            cfg.get(CONF_PRICE_THRESHOLD, DEFAULT_PRICE_THRESHOLD)
        )
        self.legionella_min_days: int = int(
            cfg.get(CONF_LEGIONELLA_MIN_DAYS, DEFAULT_LEGIONELLA_MIN_DAYS)
        )
        self.legionella_max_days: int = int(
            cfg.get(CONF_LEGIONELLA_MAX_DAYS, DEFAULT_LEGIONELLA_MAX_DAYS)
        )
        self.legionella_duration_hours: int = int(
            cfg.get(CONF_LEGIONELLA_DURATION_HOURS, DEFAULT_LEGIONELLA_DURATION_HOURS)
        )

        # Persisted datetimes (entities write back here; coordinator reads here)
        self.vacation_end: datetime | None = None
        self.last_legionella_run: datetime | None = None

        # -------------------------------------------------------------------
        # Internal runtime state — not user-settable, not persisted via entities
        # -------------------------------------------------------------------
        self._legionella_boost_end: datetime | None = None
        self._aux_power_zero_since: datetime | None = None
        self._prev_aux_power_w: float | None = None
        self._hw_reduction_active: bool = False
        self._hw_reduction_on_since: datetime | None = None
        self._hot_water_making_since: datetime | None = None
        self._prev_master_enabled: bool = False
        # Falling-edge tracker for the legionella switch: when the user turns
        # the switch off, we cancel any in-progress boost by clearing the
        # end-time. Otherwise toggling back on later would silently resume the
        # original boost window.
        self._prev_legionella_enabled: bool = False

        # Rolling sample buffers for 60-min averages
        self._compressor_samples: deque[tuple[datetime, float]] = deque()
        self._heating_samples: deque[tuple[datetime, bool]] = deque()
        self._indoor_temp_samples: deque[tuple[datetime, float]] = deque()

        self._unsub_state_listener = None
        # Stored for diagnostic sensors (not part of FullDecision to keep it pure)
        self._last_computed = None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> FullDecision:
        """Run one full cascade evaluation and write back to bound entities.

        Each phase is wrapped in its own try/except so the log line tells you
        exactly *which* phase failed (input reading vs. health-assessment vs.
        compute vs. controller vs. apply). Increase log level with::

            service: logger.set_level
            data:
              custom_components.smart_heat_control: debug
        """
        phase = "start"
        try:
            phase = "read_inputs"
            tz = ZoneInfo(self.hass.config.time_zone)
            inputs = self._read_inputs(tz)

            phase = "assess_health"
            health = assess_health(inputs)

            phase = "compute"
            computed_data = compute(inputs, health, self.hass.config.time_zone)
            self._last_computed = computed_data

            phase = "decide"
            decision = decide(inputs, computed_data, health)

            _LOGGER.debug(
                "SHC cascade: mode=%s hw_mode=%s degraded=%s "
                "indoor=%.1f outdoor=%.1f master=%s",
                decision.climate.mode,
                decision.hot_water.mode,
                decision.degraded_reason or "no",
                inputs.indoor_temp if inputs.indoor_temp is not None else -99,
                inputs.outdoor_temp if inputs.outdoor_temp is not None else -99,
                inputs.master_enabled,
            )

            # Reset when master goes OFF (only on the falling edge)
            if self._prev_master_enabled and not inputs.master_enabled:
                phase = "reset_to_defaults"
                await self._reset_to_defaults()
            self._prev_master_enabled = inputs.master_enabled

            # Cancel any active legionella boost on falling edge of the switch.
            # The computed.is_legionella_boost_active gate already makes the
            # HW cascade behave correctly while the switch is off, but clearing
            # the timer here ensures a subsequent "switch back on" starts a
            # fresh boost rather than silently resuming the cancelled one.
            if self._prev_legionella_enabled and not inputs.legionella_boost_enabled:
                if self._legionella_boost_end is not None:
                    _LOGGER.info(
                        "SHC: Legionella switch turned off mid-boost — cancelling boost timer"
                    )
                self._legionella_boost_end = None
            self._prev_legionella_enabled = inputs.legionella_boost_enabled

            # Apply decisions to bound external entities
            if inputs.master_enabled and health.has_room_temp_signal:
                phase = "apply"
                await self._apply(decision)

            # Handle legionella boost trigger
            if decision.start_legionella_boost:
                now = dt_util.utcnow()
                self.last_legionella_run = now
                self._legionella_boost_end = now + timedelta(
                    hours=self.legionella_duration_hours
                )
                _LOGGER.info(
                    "SHC: Legionella boost triggered (days since last: %d)",
                    computed_data.days_since_legionella,
                )

            return decision
        except Exception as exc:
            # Log the full traceback before wrapping — UpdateFailed alone hides
            # the stack and makes user-side debugging painful.
            _LOGGER.exception(
                "SHC cascade failed in phase '%s': %s. "
                "Check the entities bound in the config flow — most failures "
                "here come from an entity returning 'unavailable' or a value "
                "the parser can't read (e.g. a Nord Pool sensor with no "
                "'prices' attribute).",
                phase,
                exc,
            )
            raise UpdateFailed(f"Cascade evaluation failed in phase '{phase}': {exc}") from exc

    # ------------------------------------------------------------------
    # Input reading
    # ------------------------------------------------------------------

    def _read_inputs(self, tz: ZoneInfo) -> Inputs:
        hass = self.hass
        cfg: dict[str, Any] = {**self.entry.data, **self.entry.options}
        now = dt_util.utcnow()

        # Climate (required)
        climate_id: str = cfg.get(CONF_CLIMATE_ENTITY, "")
        climate_state = hass.states.get(climate_id) if climate_id else None

        indoor_temp_id = cfg.get(CONF_INDOOR_TEMP_SENSOR)
        if indoor_temp_id:
            indoor_temp = _read_float(hass, indoor_temp_id)
        else:
            indoor_temp = _to_float(
                climate_state.attributes.get("current_temperature")
                if climate_state else None
            )

        outdoor_temp = _read_float(hass, cfg.get(CONF_OUTDOOR_TEMP_SENSOR))
        climate_target = _to_float(
            climate_state.attributes.get("temperature") if climate_state else None
        )

        # Optional heating readbacks
        heat_curve = _read_float(hass, cfg.get(CONF_HEAT_CURVE_NUMBER))
        hw_setpoint = _read_float(hass, cfg.get(CONF_HOT_WATER_SETPOINT_NUMBER))
        hw_temp = _read_float(hass, cfg.get(CONF_HOT_WATER_TEMP_SENSOR))
        hw_extra_id = cfg.get(CONF_HOT_WATER_EXTRA_SWITCH)
        hw_extra_on: bool | None = _read_bool(hass, hw_extra_id) if hw_extra_id else None
        pump = _read_pump_activity(hass, cfg.get(CONF_PUMP_ACTIVITY_SENSOR))

        # Power monitoring
        comp_w = _read_float(hass, cfg.get(CONF_COMPRESSOR_POWER_SENSOR))
        aux_w = _read_float(hass, cfg.get(CONF_AUX_POWER_SENSOR))

        # Track when aux_power transitions to 0
        if aux_w is not None:
            if aux_w == 0.0 and (self._prev_aux_power_w is None or self._prev_aux_power_w != 0.0):
                self._aux_power_zero_since = now
            elif aux_w != 0.0:
                self._aux_power_zero_since = None
        self._prev_aux_power_w = aux_w

        # HW reduction state machine (v1 sub-automations)
        self._update_hw_reduction(pump, now)

        # Rolling buffers → avg_compressor, heating_fraction, indoor_max
        avg_comp, heat_frac, indoor_max = self._update_rolling_buffers(
            comp_w, pump, indoor_temp, now
        )

        # Pricing
        # Compute today_avg from the 96 quarters when available — Nord Pool's
        # _price_today sensor exposes the *current* quarter price as state,
        # NOT today's average. Reading state.state as the average gave a
        # ~100-öre value that drifted with the live price and made every
        # "is current price above/below today_avg" gate flip every quarter.
        price_today_id = cfg.get(CONF_PRICE_TODAY_SENSOR)
        current_price = _read_float(hass, cfg.get(CONF_PRICE_SENSOR))
        price_quarters = _parse_price_quarters(hass, price_today_id)
        if price_quarters:
            today_avg: float | None = sum(price_quarters) / len(price_quarters)
        else:
            # Fallback for vendors that DO expose the average as state
            today_avg = _read_float(hass, price_today_id)

        # Weather
        weather_forecast = _parse_forecast(
            hass, cfg.get(CONF_WEATHER_FORECAST_ENTITY), tz
        )

        # Sun
        is_sun_up, next_sunrise = _read_sun(hass)

        # PV / battery
        has_pv_excess = _read_bool(hass, cfg.get(CONF_PV_EXCESS_BINARY_SENSOR))
        is_on_battery = _read_bool(hass, cfg.get(CONF_BATTERY_DISCHARGING_BINARY_SENSOR))
        is_bridge_active = _read_bool(hass, cfg.get(CONF_BRIDGE_TO_SOLAR_BINARY_SENSOR))
        is_sunny_day = _read_bool(hass, cfg.get(CONF_IS_SUNNY_DAY_BINARY_SENSOR))
        pv_forecast = _read_float(hass, cfg.get(CONF_SOLAR_FORECAST_TODAY_SENSOR))

        return Inputs(
            now=now,
            indoor_temp=indoor_temp,
            outdoor_temp=outdoor_temp,
            climate_target_temp=climate_target,
            heat_curve=heat_curve,
            hot_water_setpoint=hw_setpoint,
            hot_water_temp=hw_temp,
            hot_water_extra_on=hw_extra_on,
            pump_activity=pump,
            compressor_power_w=comp_w,
            aux_power_w=aux_w,
            aux_power_zero_since=self._aux_power_zero_since,
            avg_compressor_power_last_hour_w=avg_comp,
            heating_duration_last_hour=heat_frac,
            current_price=current_price,
            today_avg_price=today_avg,
            price_quarters=price_quarters,
            weather_forecast=weather_forecast,
            indoor_max_recent_temp=indoor_max,
            has_pv_excess=has_pv_excess,
            is_on_battery=is_on_battery,
            is_bridge_active=is_bridge_active,
            is_sunny_day=is_sunny_day,
            pv_forecast_today_kwh=pv_forecast,
            is_sun_up=is_sun_up,
            next_sunrise=next_sunrise,
            master_enabled=self.master_enabled,
            cheap_price_enabled=self.cheap_price_enabled,
            price_peak_enabled=self.price_peak_enabled,
            weather_anticipation_enabled=self.weather_anticipation_enabled,
            legionella_boost_enabled=self.legionella_boost_enabled,
            survive_solar_enabled=self.survive_solar_enabled,
            default_indoor_temp=self.default_indoor_temp,
            default_heat_curve=self.default_heat_curve,
            default_hw_temp=self.default_hw_temp,
            price_threshold=self.price_threshold,
            legionella_min_days=self.legionella_min_days,
            legionella_max_days=self.legionella_max_days,
            legionella_duration_hours=self.legionella_duration_hours,
            vacation_end=self.vacation_end,
            last_legionella_run=self.last_legionella_run,
            legionella_boost_end=self._legionella_boost_end,
            hw_reduction_active=self._hw_reduction_active,
        )

    # ------------------------------------------------------------------
    # Internal state machines
    # ------------------------------------------------------------------

    def _update_hw_reduction(self, pump: PumpActivity | None, now: datetime) -> None:
        """State machine that mirrors the three v1 HW-reduction sub-automations."""
        # Sub-automation 1: activate after 1.5 h of continuous hot-water production
        if pump == PumpActivity.HOT_WATER:
            if self._hot_water_making_since is None:
                self._hot_water_making_since = now
        else:
            self._hot_water_making_since = None

        if (
            not self._hw_reduction_active
            and self._hot_water_making_since is not None
            and (now - self._hot_water_making_since).total_seconds()
            >= HW_REDUCTION_TRIGGER_HOURS * 3600
        ):
            self._hw_reduction_active = True
            self._hw_reduction_on_since = now
            _LOGGER.info("HW reduction activated after long hot-water run")

        if self._hw_reduction_active and self._hw_reduction_on_since is not None:
            on_secs = (now - self._hw_reduction_on_since).total_seconds()
            # Sub-automation 2: remove after 10 min if pump is idle/making-HW (no space heating)
            if on_secs >= HW_REDUCTION_NO_HEATING_TIMEOUT_MINUTES * 60 and pump in (
                PumpActivity.IDLE, PumpActivity.HOT_WATER, None
            ):
                self._hw_reduction_active = False
                self._hw_reduction_on_since = None
                _LOGGER.info("HW reduction removed: no heating demand")
            # Sub-automation 3: remove after 1 h unconditionally
            elif on_secs >= HW_REDUCTION_HEATING_TIMEOUT_HOURS * 3600:
                self._hw_reduction_active = False
                self._hw_reduction_on_since = None
                _LOGGER.info("HW reduction removed: timeout")

    def _update_rolling_buffers(
        self,
        comp_w: float | None,
        pump: PumpActivity | None,
        indoor_temp: float | None,
        now: datetime,
    ) -> tuple[float | None, float | None, float | None]:
        """Maintain 60-min rolling buffers; return (avg_comp, heat_frac, indoor_max)."""
        cutoff = now - timedelta(hours=1)

        if comp_w is not None:
            self._compressor_samples.append((now, comp_w))
        while self._compressor_samples and self._compressor_samples[0][0] < cutoff:
            self._compressor_samples.popleft()

        self._heating_samples.append((now, pump == PumpActivity.HEATING))
        while self._heating_samples and self._heating_samples[0][0] < cutoff:
            self._heating_samples.popleft()

        if indoor_temp is not None:
            self._indoor_temp_samples.append((now, indoor_temp))
        while self._indoor_temp_samples and self._indoor_temp_samples[0][0] < cutoff:
            self._indoor_temp_samples.popleft()

        avg_comp = (
            sum(v for _, v in self._compressor_samples) / len(self._compressor_samples)
            if self._compressor_samples else None
        )
        heat_frac = (
            sum(1 for _, v in self._heating_samples if v) / len(self._heating_samples)
            if self._heating_samples else None
        )
        indoor_max = (
            max(v for _, v in self._indoor_temp_samples)
            if self._indoor_temp_samples else None
        )
        return avg_comp, heat_frac, indoor_max

    # ------------------------------------------------------------------
    # Write-back
    # ------------------------------------------------------------------

    async def _apply(self, decision: FullDecision) -> None:
        """Write changed target values to bound external entities.

        Writes are skipped if the current value already matches (fixes the v1
        string-vs-float guard bug as a side effect). Writes are separated by
        WRITE_SETTLE_DELAY_SECONDS to avoid dropping commands.
        """
        cfg: dict[str, Any] = {**self.entry.data, **self.entry.options}
        hass = self.hass

        # --- Climate temperature ---
        climate_id = cfg.get(CONF_CLIMATE_ENTITY)
        if decision.climate.target_temp is not None and climate_id:
            c_state = hass.states.get(climate_id)
            current_temp = _to_float(
                c_state.attributes.get("temperature") if c_state else None
            )
            if current_temp is None or abs(current_temp - decision.climate.target_temp) > 0.05:
                await hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {"entity_id": climate_id, "temperature": decision.climate.target_temp},
                    blocking=True,
                )
                await asyncio.sleep(WRITE_SETTLE_DELAY_SECONDS)

        # --- Heat curve ---
        curve_id = cfg.get(CONF_HEAT_CURVE_NUMBER)
        if decision.climate.target_curve is not None and curve_id:
            current_curve = _read_float(hass, curve_id)
            if current_curve is None or abs(current_curve - decision.climate.target_curve) > 0.05:
                await hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": curve_id, "value": decision.climate.target_curve},
                    blocking=True,
                )
                await asyncio.sleep(WRITE_SETTLE_DELAY_SECONDS)

        # --- Hot water setpoint (only when hot_water_extra is OFF) ---
        hw_id = cfg.get(CONF_HOT_WATER_SETPOINT_NUMBER)
        hw_extra_id = cfg.get(CONF_HOT_WATER_EXTRA_SWITCH)
        hw_extra_off = not _read_bool(hass, hw_extra_id) if hw_extra_id else True
        if decision.hot_water.target_temp is not None and hw_id and hw_extra_off:
            current_hw = _read_float(hass, hw_id)
            if current_hw is None or abs(current_hw - decision.hot_water.target_temp) > 0.05:
                await hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": hw_id, "value": decision.hot_water.target_temp},
                    blocking=True,
                )
                await asyncio.sleep(WRITE_SETTLE_DELAY_SECONDS)

    async def _reset_to_defaults(self) -> None:
        """Reset bound entities to defaults when master is turned off.

        Mirrors v1's ``comfortzone_reset_to_default_when_disabled`` automation.
        """
        cfg: dict[str, Any] = {**self.entry.data, **self.entry.options}
        hass = self.hass

        climate_id = cfg.get(CONF_CLIMATE_ENTITY)
        curve_id = cfg.get(CONF_HEAT_CURVE_NUMBER)
        hw_id = cfg.get(CONF_HOT_WATER_SETPOINT_NUMBER)

        if climate_id:
            await hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": climate_id, "temperature": self.default_indoor_temp},
                blocking=True,
            )
        if curve_id:
            await asyncio.sleep(WRITE_SETTLE_DELAY_SECONDS)
            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": curve_id, "value": self.default_heat_curve},
                blocking=True,
            )
        if hw_id:
            await asyncio.sleep(WRITE_SETTLE_DELAY_SECONDS)
            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": hw_id, "value": self.default_hw_temp},
                blocking=True,
            )
        _LOGGER.info("Smart Heat Control: reset to defaults (master disabled)")

    async def async_shutdown(self) -> None:
        if self._unsub_state_listener is not None:
            self._unsub_state_listener()
            self._unsub_state_listener = None
        await super().async_shutdown()
