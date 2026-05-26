"""Derive all secondary values from a raw ``Inputs`` snapshot.

This module is a 1:1 port of the ``variables:`` blocks in the v1 YAML
automation ``comfortzone_settings_controller``. Every function is named after
the v1 variable it replaces so the two can be compared side-by-side during the
porting phase.

Design contract
---------------
- **Pure functions only** — no HA calls, no side effects, no I/O.
- ``compute()`` is the single entry point; the coordinator calls it once per
  cycle after reading all entity states into an ``Inputs`` snapshot.
- All arguments that could be ``None`` are checked before arithmetic. When a
  prerequisite is absent the function returns a safe falsy value so the caller
  (usually a cascade-branch precondition) can cleanly return ``False``.
- Ordering within ``compute()`` mirrors the dependency graph of v1's two
  ``variables:`` blocks. Do **not** reorder without verifying there are no
  hidden dependencies.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .const import (
    AM_BOUNDARY_HOUR,
    AUX_ALLOWED_DEFAULT_W,
    AUX_ALLOWED_HARD_WINTER_W,
    CHEAP_OR_AVG_PRICE_RATIO,
    CHEAP_PRICE_RATIO,
    CHEAPEST_MIDDAY_QUARTER_COUNT,
    EXPENSIVE_EVENING_START_HOUR,
    EXPENSIVE_QUARTER_COUNT,
    FULL_COMPRESSOR_AVG_W,
    FUTURE_TEMP_TOMORROW_THRESHOLD_HOUR,
    MAX_HEAT_CURVE,
    MAX_CLIMATE_TEMP,
    MIN_CLIMATE_TEMP,
    MIN_HEAT_CURVE,
    NOT_USING_ADDITION_GRACE_SECONDS,
    QUARTERS_PER_DAY,
    SUMMER_OUTDOOR_C,
    SUMMER_TODAY_HIGH_C,
    SUMMER_TOMORROW_HIGH_C,
    VERY_CHEAP_VS_RECENT_RATIO,
    WAIT_FOR_SUN_PV_KWH_THRESHOLD,
    WEATHER_OVERRIDE_FUTURE_HIGH_C,
    WEATHER_OVERRIDE_INDOOR_DELTA_C,
    WINTER_MONTHS_FALLBACK,
)
from .models import Computed, ForecastDay, Health, Inputs

_LOGGER = logging.getLogger(__name__)

_FALLBACK_TZ = ZoneInfo("Europe/Stockholm")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute(inputs: Inputs, health: Health, tz_name: str) -> Computed:
    """Derive every field in ``Computed`` from the raw ``Inputs`` snapshot.

    ``tz_name`` is ``hass.config.time_zone`` (e.g. ``'Europe/Stockholm'``).
    All time comparisons are done in local time to match v1 Jinja behaviour.
    """
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        _LOGGER.warning("Unknown timezone %r — falling back to Europe/Stockholm", tz_name)
        tz = _FALLBACK_TZ

    local = inputs.now.astimezone(tz)
    current_hour = local.hour
    current_minute = local.minute
    current_quarter = current_hour * 4 + current_minute // 15
    next_quarter = (current_quarter + 1) % QUARTERS_PER_DAY
    is_am = current_hour < AM_BOUNDARY_HOUR

    # ------------------------------------------------------------------
    # Phase 1 — Vacation / Legionella timestamps
    # ------------------------------------------------------------------
    now_ts = inputs.now

    is_vacation_active = (
        inputs.vacation_end is not None and now_ts < inputs.vacation_end
    )
    hours_to_vacation_end = (
        max(0.0, (inputs.vacation_end - now_ts).total_seconds() / 3600.0)
        if is_vacation_active and inputs.vacation_end is not None
        else 0.0
    )

    # An active boost requires BOTH the user-controlled switch and a valid
    # end-time in the future. Without the switch gate, turning the switch OFF
    # mid-boost would not cancel it — the HW cascade would keep returning a
    # 60°C setpoint until the original timer expired, contradicting the UI.
    is_legionella_boost_active = (
        inputs.legionella_boost_enabled
        and inputs.legionella_boost_end is not None
        and now_ts < inputs.legionella_boost_end
    )
    days_since_legionella = (
        int((now_ts - inputs.last_legionella_run).total_seconds() / 86400)
        if inputs.last_legionella_run is not None
        else 99
    )

    # ------------------------------------------------------------------
    # Phase 2 — Quarter-based price analysis
    # v1 equivalents: cheap_hours_list, cheap_midday_hours_list,
    #                 expensive_am_hours_list, expensive_pm_hours_list,
    #                 am_avg_price, pm_avg_price, am_more_expensive
    # ------------------------------------------------------------------
    today_am_avg: float | None = None
    today_pm_avg: float | None = None
    cheap_quarters: frozenset[int] = frozenset()
    cheap_midday_quarters: frozenset[int] = frozenset()
    expensive_am_quarters: frozenset[int] = frozenset()
    expensive_pm_quarters: frozenset[int] = frozenset()
    peak_expensive_am_quarter: int | None = None
    peak_expensive_pm_quarter: int | None = None

    q = inputs.price_quarters
    if len(q) == QUARTERS_PER_DAY and inputs.today_avg_price is not None:
        today_am_avg = sum(q[:48]) / 48.0
        today_pm_avg = sum(q[48:]) / 48.0

        # cheap_quarters — all quarters below CHEAP_PRICE_RATIO × today_avg
        threshold = CHEAP_PRICE_RATIO * inputs.today_avg_price
        cheap_quarters = frozenset(i for i in range(96) if q[i] < threshold)

        # cheap_midday_quarters — cheapest N in AM (0-47) + cheapest N in PM (48-95)
        # Replaces v1's "3 cheapest hours in each half" but at 15-min resolution.
        n = CHEAPEST_MIDDAY_QUARTER_COUNT
        am_by_price = sorted(range(48), key=lambda i: q[i])
        pm_by_price = sorted(range(48, 96), key=lambda i: q[i])
        cheap_midday_quarters = frozenset(am_by_price[:n] + pm_by_price[:n])

        # expensive quarters — most expensive N in each half
        am_by_price_desc = sorted(range(48), key=lambda i: q[i], reverse=True)
        pm_by_price_desc = sorted(range(48, 96), key=lambda i: q[i], reverse=True)
        expensive_am_quarters = frozenset(am_by_price_desc[:n])
        expensive_pm_quarters = frozenset(pm_by_price_desc[:n])

        # peak = single most expensive quarter per half (replaces v1's [-1])
        peak_expensive_am_quarter = am_by_price_desc[0]
        peak_expensive_pm_quarter = pm_by_price_desc[0]

    # ------------------------------------------------------------------
    # Phase 3 — Weather forecast
    # v1 equivalents: weather_today_index, future_highest_temp,
    #                 future_lowest_temp, is_winter, is_night_cold,
    #                 time_to_sunrise
    # ------------------------------------------------------------------
    forecast = inputs.weather_forecast
    today_idx = _find_today_index(forecast, local)
    today_forecast_condition: str | None = (
        forecast[today_idx].condition if today_idx < len(forecast) else None
    )

    # "future" switches from today→tomorrow at FUTURE_TEMP_TOMORROW_THRESHOLD_HOUR
    future_idx = (
        today_idx if current_hour < FUTURE_TEMP_TOMORROW_THRESHOLD_HOUR
        else today_idx + 1
    )
    tomorrow_idx = today_idx + 1

    future_highest_temp = _forecast_high(forecast, future_idx)
    future_lowest_temp = _forecast_low(forecast, future_idx)
    tomorrow_highest_temp = _forecast_high(forecast, tomorrow_idx)

    # is_winter: tomorrow AND day-after both < 3°C high, AND future_low < -1°C
    # Falls back to month if forecast is too short.
    if len(forecast) > today_idx + 2:
        day_after_highest = _forecast_high(forecast, today_idx + 2)
        is_winter = bool(
            tomorrow_highest_temp is not None and tomorrow_highest_temp < 3
            and day_after_highest is not None and day_after_highest < 3
            and future_lowest_temp is not None and future_lowest_temp < -1
        )
    else:
        is_winter = local.month in WINTER_MONTHS_FALLBACK

    is_night_cold = future_lowest_temp is not None and future_lowest_temp <= 6

    time_to_sunrise_hours = _time_to_sunrise_hours(inputs)

    # ------------------------------------------------------------------
    # Phase 4 — Solar / PV
    # ------------------------------------------------------------------
    pv_kwh = inputs.pv_forecast_today_kwh or 0.0
    wait_for_sun = inputs.survive_solar_enabled and pv_kwh > WAIT_FOR_SUN_PV_KWH_THRESHOLD

    # ------------------------------------------------------------------
    # Phase 5 — Power state
    # v1 equivalents: is_full_compressor, not_using_addition,
    #                 not_winter_addition, is_heating_alot
    # ------------------------------------------------------------------
    avg_comp = inputs.avg_compressor_power_last_hour_w or 0.0
    is_full_compressor = avg_comp > FULL_COMPRESSOR_AVG_W

    aux_w = inputs.aux_power_w or 0.0
    not_using_addition = _compute_not_using_addition(inputs)

    # not_winter_addition: in winter we tolerate some aux before disabling cheap-mode.
    is_hard_winter_cold = (
        future_highest_temp is not None and future_highest_temp < 0
        or future_lowest_temp is not None and future_lowest_temp < -4
    )
    aux_winter_limit = (
        AUX_ALLOWED_HARD_WINTER_W if is_hard_winter_cold else AUX_ALLOWED_DEFAULT_W
    )
    not_winter_addition = not is_winter or aux_w < aux_winter_limit

    heating_dur = inputs.heating_duration_last_hour or 0.0
    is_heating_alot = is_full_compressor and heating_dur > 0.8

    # ------------------------------------------------------------------
    # Phase 6 — Price cheapness
    # v1 equivalents: today_is_cheap, current_hour_is_cheap,
    #                 is_current_hour_cheap_or_avg,
    #                 is_current_hour_cheaper_than_avg
    # ------------------------------------------------------------------
    cp = inputs.current_price
    ta = inputs.today_avg_price
    ra = inputs.recent_avg_price

    today_is_cheap = _compute_today_is_cheap(
        has_pv_excess=inputs.has_pv_excess,
        is_am=is_am,
        recent_avg=ra,
        today_avg=ta,
        am_avg=today_am_avg,
        pm_avg=today_pm_avg,
    )

    current_hour_is_cheap = _compute_current_hour_is_cheap(
        current_price=cp,
        today_avg=ta,
        am_avg=today_am_avg,
        pm_avg=today_pm_avg,
        recent_avg=ra,
        is_am=is_am,
        is_winter=is_winter,
        has_pv_excess=inputs.has_pv_excess,
        is_on_battery=inputs.is_on_battery,
    )

    # is_current_hour_cheap_or_avg — weaker signal (≤ today_avg, not 90%)
    is_current_hour_cheap_or_avg = bool(
        (inputs.has_pv_excess and not is_am)
        or (cp is not None and cp < 0)
        or (
            cp is not None
            and ta is not None
            and not inputs.is_on_battery
            and cp <= CHEAP_OR_AVG_PRICE_RATIO * ta
            and cp <= inputs.price_threshold
        )
    )

    # is_current_hour_cheaper_than_avg — stronger signal (< 90% of today_avg)
    is_current_hour_cheaper_than_avg = bool(
        (inputs.has_pv_excess and not is_am)
        or (cp is not None and cp < 0)
        or (
            cp is not None
            and ta is not None
            and cp <= CHEAP_PRICE_RATIO * ta
            and cp <= inputs.price_threshold
        )
    )

    is_next_pm_quarter_expensive = next_quarter in expensive_pm_quarters

    # is_evening_expensive: any of the most-expensive PM quarters fall in
    # the evening window (EXPENSIVE_EVENING_START_HOUR onward).
    is_evening_expensive = any(
        q_idx >= EXPENSIVE_EVENING_START_HOUR * 4
        for q_idx in expensive_pm_quarters
    )

    # ------------------------------------------------------------------
    # Phase 7 — Vacation-adjusted defaults
    # v1 equivalents: default_indoor_temp_helper, default_hw_temp_helper,
    #                 default_heat_curve_helper (partial — curve uses
    #                 cheap_price_conditions_met computed in phase 9)
    # ------------------------------------------------------------------
    d_indoor = float(inputs.default_indoor_temp)
    d_curve = float(inputs.default_heat_curve)
    d_hw = float(inputs.default_hw_temp)

    if is_vacation_active:
        if hours_to_vacation_end < 5:
            default_indoor_helper = max(d_indoor - 1.0, MIN_CLIMATE_TEMP)
            default_curve_helper_base = round(max(d_curve - 0.5, MIN_HEAT_CURVE), 1)
        else:
            default_indoor_helper = max(d_indoor - 2.0, MIN_CLIMATE_TEMP)
            default_curve_helper_base = round(max(d_curve - 1.0, MIN_HEAT_CURVE), 1)
        default_hw_helper = 40.0
    else:
        default_indoor_helper = d_indoor
        default_hw_helper = d_hw
        default_curve_helper_base = d_curve

    # ------------------------------------------------------------------
    # Phase 8 — cheap_price_conditions_met (needed by curve helper)
    # Computed early so default_heat_curve_helper can use it.
    # v1 equivalent: is_cheap_price_conditions_met
    # ------------------------------------------------------------------
    cheap_price_cond = _compute_cheap_price_conditions_met(
        is_cheap_boost_enabled=inputs.cheap_price_enabled,
        current_hour_is_cheap=current_hour_is_cheap,
        not_using_addition=not_using_addition,
        not_winter_addition=not_winter_addition,
        future_lowest_temp=future_lowest_temp,
        hot_water_temp=inputs.hot_water_temp,
        hot_water_setpoint=inputs.hot_water_setpoint,
    )

    # Final default_heat_curve_helper — adds the "pre-boost before expensive PM"
    # tweak from v1 (that extra 0.5 curve boost when we're about to enter an
    # expensive evening and the current hour is not cheap but we are in
    # cheap_price_conditions_met mode).
    if not is_vacation_active and (
        cheap_price_cond
        and not current_hour_is_cheap
        and is_evening_expensive
        and is_night_cold
    ):
        default_curve_helper = round(min(default_curve_helper_base + 0.5, MAX_HEAT_CURVE), 1)
    else:
        default_curve_helper = default_curve_helper_base

    # ------------------------------------------------------------------
    # Phase 9 — Weather + price-peak branch preconditions
    # v1 equivalents: is_weather_active_conditions_met,
    #                 is_price_peak_mode_conditions_met
    # ------------------------------------------------------------------
    indoor_max = inputs.indoor_max_recent_temp

    weather_temp_logic = _compute_weather_temp_logic(
        future_highest_temp=future_highest_temp,
        today_forecast_condition=today_forecast_condition,
        is_sunny_day=inputs.is_sunny_day,
        outdoor_temp=inputs.outdoor_temp,
        time_to_sunrise_hours=time_to_sunrise_hours,
        is_sun_up=inputs.is_sun_up,
        indoor_temp=inputs.indoor_temp,
        default_indoor_temp=d_indoor,
        is_winter=is_winter,
        is_heating_alot=is_heating_alot,
        default_heat_curve_helper=default_curve_helper,
        min_heat_curve=MIN_HEAT_CURVE,
    )

    weather_active_cond = _compute_weather_active_conditions_met(
        has_pv_excess=inputs.has_pv_excess,
        is_bridge_active=inputs.is_bridge_active,
        wait_for_sun=wait_for_sun,
        is_weather_enabled=inputs.weather_anticipation_enabled,
        is_am=is_am,
        current_hour=current_hour,
        indoor_temp=inputs.indoor_temp,
        indoor_max_recent_temp=indoor_max,
        default_indoor_temp=d_indoor,
        weather_temp_logic=weather_temp_logic,
        is_night_cold=is_night_cold,
        is_winter=is_winter,
        outdoor_temp=inputs.outdoor_temp,
        future_highest_temp=future_highest_temp,
        tomorrow_highest_temp=tomorrow_highest_temp,
        current_price=cp,
        today_avg=ta,
    )

    price_peak_cond = _compute_price_peak_conditions_met(
        current_quarter=current_quarter,
        next_quarter=next_quarter,
        current_minute=current_minute,
        expensive_am_quarters=expensive_am_quarters,
        expensive_pm_quarters=expensive_pm_quarters,
        is_price_peak_enabled=inputs.price_peak_enabled,
        has_pv_excess=inputs.has_pv_excess,
        is_bridge_active=inputs.is_bridge_active,
        is_winter=is_winter,
        today_is_cheap=today_is_cheap,
        is_on_battery=inputs.is_on_battery,
        wait_for_sun=wait_for_sun,
    )

    # ------------------------------------------------------------------
    # Assemble and return
    # ------------------------------------------------------------------
    return Computed(
        current_hour=current_hour,
        current_minute=current_minute,
        current_quarter=current_quarter,
        next_quarter=next_quarter,
        is_am=is_am,
        is_vacation_active=is_vacation_active,
        hours_to_vacation_end=hours_to_vacation_end,
        is_legionella_boost_active=is_legionella_boost_active,
        days_since_legionella=days_since_legionella,
        today_am_avg_price=today_am_avg,
        today_pm_avg_price=today_pm_avg,
        cheap_quarters=cheap_quarters,
        cheap_midday_quarters=cheap_midday_quarters,
        expensive_am_quarters=expensive_am_quarters,
        expensive_pm_quarters=expensive_pm_quarters,
        peak_expensive_am_quarter=peak_expensive_am_quarter,
        peak_expensive_pm_quarter=peak_expensive_pm_quarter,
        today_forecast_index=today_idx,
        future_highest_temp=future_highest_temp,
        future_lowest_temp=future_lowest_temp,
        is_winter=is_winter,
        is_night_cold=is_night_cold,
        today_forecast_condition=today_forecast_condition,
        time_to_sunrise_hours=time_to_sunrise_hours,
        wait_for_sun=wait_for_sun,
        is_full_compressor=is_full_compressor,
        is_heating_alot=is_heating_alot,
        not_using_addition=not_using_addition,
        not_winter_addition=not_winter_addition,
        today_is_cheap=today_is_cheap,
        current_hour_is_cheap=current_hour_is_cheap,
        is_current_hour_cheap_or_avg=is_current_hour_cheap_or_avg,
        is_current_hour_cheaper_than_avg=is_current_hour_cheaper_than_avg,
        is_next_pm_quarter_expensive=is_next_pm_quarter_expensive,
        is_evening_expensive=is_evening_expensive,
        default_indoor_temp_helper=default_indoor_helper,
        default_heat_curve_helper=default_curve_helper,
        default_hw_temp_helper=default_hw_helper,
        weather_active_conditions_met=weather_active_cond,
        price_peak_conditions_met=price_peak_cond,
        cheap_price_conditions_met=cheap_price_cond,
    )


# ---------------------------------------------------------------------------
# Phase helpers — each maps to one v1 template variable
# ---------------------------------------------------------------------------

def _find_today_index(forecast: tuple[ForecastDay, ...], local: datetime) -> int:
    """Return the index in ``forecast`` whose ``day`` >= today (local).

    More robust than v1's ``datetime.startswith(now().strftime('%Y-%m-%d'))``
    because it works correctly across midnight and DST transitions.
    """
    today = local.date()
    for i, entry in enumerate(forecast):
        if entry.day >= today:
            return i
    return 0


def _forecast_high(forecast: tuple[ForecastDay, ...], idx: int) -> float | None:
    """Return the daily high for forecast[idx], or None if out of range."""
    if 0 <= idx < len(forecast):
        return forecast[idx].temperature
    return None


def _forecast_low(forecast: tuple[ForecastDay, ...], idx: int) -> float | None:
    """Return the daily low for forecast[idx], or None if out of range."""
    if 0 <= idx < len(forecast):
        return forecast[idx].templow
    return None


def _compute_not_using_addition(inputs: Inputs) -> bool:
    """True only when aux power is 0 AND has been 0 for the full grace period.

    v1 equivalent::

        not_using_addition: >
          {{ addition_power == 0 and last_changed is not none and
             (now() - last_changed).total_seconds() > 1800 }}
    """
    if inputs.aux_power_w is None or inputs.aux_power_w != 0:
        return False
    if inputs.aux_power_zero_since is None:
        return False
    elapsed = (inputs.now - inputs.aux_power_zero_since).total_seconds()
    return elapsed > NOT_USING_ADDITION_GRACE_SECONDS


def _time_to_sunrise_hours(inputs: Inputs) -> float:
    """Hours until next sunrise, clamped to ≥ 0.

    v1 equivalent::

        time_to_sunrise: >
          {{ [(as_timestamp(state_attr('sun.sun', 'next_rising'), now_ts) - now_ts) / 3600, 0] | max }}
    """
    if inputs.next_sunrise is None:
        return 0.0
    diff = (inputs.next_sunrise - inputs.now).total_seconds() / 3600.0
    return max(diff, 0.0)


def _compute_today_is_cheap(
    has_pv_excess: bool,
    is_am: bool,
    recent_avg: float | None,
    today_avg: float | None,
    am_avg: float | None,
    pm_avg: float | None,
) -> bool:
    """v1 equivalent: ``today_is_cheap``."""
    if has_pv_excess and not is_am:
        return True
    if recent_avg is None or today_avg is None:
        return False
    return (
        recent_avg > today_avg * 1.1
        and (am_avg is None or recent_avg > am_avg)
        and (pm_avg is None or recent_avg > pm_avg)
    )


def _compute_current_hour_is_cheap(
    current_price: float | None,
    today_avg: float | None,
    am_avg: float | None,
    pm_avg: float | None,
    recent_avg: float | None,
    is_am: bool,
    is_winter: bool,
    has_pv_excess: bool,
    is_on_battery: bool,
) -> bool:
    """Determine whether the current 15-min window qualifies as "cheap".

    v1 equivalent (3-branch cascade)::

        current_hour_is_cheap: >
          {% if has_pv_excess and not is_am or current_price < 0 or
               (not is_on_battery and current_price <= 0.9 * today_avg_price) %}
            {{ true }}
          {% elif is_winter and (current_price < 1.1 * today_avg_price or
               current_price < 0.9 * recent_avg_price) %}
            {{ true }}
          {% elif current_price <= 0.9 * today_avg_price and
               (is_winter or current_price < 0.5 * recent_avg_price) %}
            {{ current_price < am_avg_price if is_am else current_price < pm_avg_price }}
          {% else %}
            {{ false }}
          {% endif %}
    """
    if current_price is None or today_avg is None:
        return False
    # Branch 1 — unambiguously cheap
    if (has_pv_excess and not is_am) or current_price < 0:
        return True
    if not is_on_battery and current_price <= CHEAP_PRICE_RATIO * today_avg:
        return True
    # Branch 2 — winter leniency
    if is_winter and (
        current_price < 1.1 * today_avg
        or (recent_avg is not None and current_price < CHEAP_PRICE_RATIO * recent_avg)
    ):
        return True
    # Branch 3 — cheap relative to half-day average
    if current_price <= CHEAP_PRICE_RATIO * today_avg and (
        is_winter
        or (recent_avg is not None and current_price < VERY_CHEAP_VS_RECENT_RATIO * recent_avg)
    ):
        half_avg = am_avg if is_am else pm_avg
        if half_avg is not None:
            return current_price < half_avg
    return False


def _compute_weather_temp_logic(
    future_highest_temp: float | None,
    today_forecast_condition: str | None,
    is_sunny_day: bool,
    outdoor_temp: float | None,
    time_to_sunrise_hours: float,
    is_sun_up: bool,
    indoor_temp: float | None,
    default_indoor_temp: float,
    is_winter: bool,
    is_heating_alot: bool,
    default_heat_curve_helper: float,
    min_heat_curve: float,
) -> bool:
    """Combine multiple weather signals into a single flag for weather reduction.

    Ported 1:1 from v1, including the aggressive term 2 where
    ``future_highest_temp >= 3`` alone makes the whole expression True.

    v1 equivalent: ``weather_temp_logic``
    """
    if future_highest_temp is None:
        return False

    sun_soon = time_to_sunrise_hours <= 4 or is_sun_up
    sun_soon_3h = time_to_sunrise_hours <= 3 or is_sun_up
    sunny_condition = today_forecast_condition in ("sunny", "partlycloudy")

    # Term 1 — sunny/partly-cloudy + mild + sun coming
    term1 = bool(
        future_highest_temp >= 3
        and (sunny_condition or is_sunny_day)
        and (outdoor_temp is None or outdoor_temp >= -3)
        and sun_soon
    )

    # Term 2 — mild enough (>=3°C high triggers immediately per v1 logic)
    term2 = bool(
        future_highest_temp >= 3
        or (
            future_highest_temp >= 1
            and is_sunny_day
            and (outdoor_temp is None or outdoor_temp >= -2)
            and sun_soon_3h
        )
    )

    # Term 3 — house already warm and not winter
    term3 = bool(
        indoor_temp is not None
        and indoor_temp - 0.3 > default_indoor_temp
        and future_highest_temp >= 4
        and not is_winter
        and sun_soon
    )

    return term1 or term2 or term3


def _compute_weather_active_conditions_met(
    has_pv_excess: bool,
    is_bridge_active: bool,
    wait_for_sun: bool,
    is_weather_enabled: bool,
    is_am: bool,
    current_hour: int,
    indoor_temp: float | None,
    indoor_max_recent_temp: float | None,
    default_indoor_temp: float,
    weather_temp_logic: bool,
    is_night_cold: bool,
    is_winter: bool,
    outdoor_temp: float | None,
    future_highest_temp: float | None,
    tomorrow_highest_temp: float | None,
    current_price: float | None,
    today_avg: float | None,
) -> bool:
    """Gate for the Weather Anticipation Reduction branch.

    v1 equivalent: ``is_weather_active_conditions_met``::

        {{ (not has_pv_excess and (is_bridge_active or wait_for_sun))
           or
           ( is_weather_enabled and
             (is_am and indoor_max_recent_temp >= default_indoor_temp - 0.8 or current_hour >= 22) and
             (weather_temp_logic or (not is_night_cold or outdoor_temp >= 13)) and
             (current_price >= 0 and current_price >= today_avg_price)
           )
        }}

    Two v2-only overrides bypass the time, temp-condition, and price gates
    when conditions clearly say "no heating needed":

    FIX (v2) summer coast — today + tomorrow both forecast warm, outdoor
    already mild, indoor not unexpectedly low. Models Comfortzone's built-in
    summer mode but uses richer data: prevents climate boost on a warm sunny
    day regardless of how cheap electricity happens to be. *HW logic is
    untouched.*

    FIX (v2) anti-overheat — indoor measurably above default + today will
    warm further. Stops pre-heating an already-overheated house even when
    midday prices are below the daily average.
    """
    # Degraded path — bridge-to-solar or solar-survival forces reduction
    if not has_pv_excess and (is_bridge_active or wait_for_sun):
        return True

    if not is_weather_enabled:
        return False

    # FIX (v2): summer coast — warm now AND warm ahead → no climate heating
    if (
        not is_winter
        and future_highest_temp is not None
        and future_highest_temp >= SUMMER_TODAY_HIGH_C
        and tomorrow_highest_temp is not None
        and tomorrow_highest_temp >= SUMMER_TOMORROW_HIGH_C
        and outdoor_temp is not None
        and outdoor_temp >= SUMMER_OUTDOOR_C
        and (
            indoor_temp is None
            or indoor_temp >= default_indoor_temp - 0.5
        )
    ):
        return True

    # FIX (v2): anti-overheat — indoor already too warm + warm day → reduce
    if (
        not is_winter
        and indoor_max_recent_temp is not None
        and indoor_max_recent_temp
        >= default_indoor_temp + WEATHER_OVERRIDE_INDOOR_DELTA_C
        and future_highest_temp is not None
        and future_highest_temp >= WEATHER_OVERRIDE_FUTURE_HIGH_C
    ):
        return True

    # --- Standard v1 cascade below ---

    # Time gate — only reduce when we've been warm enough lately (AM) or late at night
    if is_am:
        time_gate = (
            indoor_max_recent_temp is not None
            and indoor_max_recent_temp >= default_indoor_temp - 0.8
        )
    else:
        time_gate = current_hour >= 22

    if not time_gate:
        return False

    # Temperature condition
    temp_ok = weather_temp_logic or (
        not is_night_cold or (outdoor_temp is not None and outdoor_temp >= 13)
    )
    if not temp_ok:
        return False

    # Price gate — only reduce when price is at or above today's average
    if current_price is None or today_avg is None:
        return False
    return current_price >= 0 and current_price >= today_avg


def _compute_price_peak_conditions_met(
    current_quarter: int,
    next_quarter: int,
    current_minute: int,
    expensive_am_quarters: frozenset[int],
    expensive_pm_quarters: frozenset[int],
    is_price_peak_enabled: bool,
    has_pv_excess: bool,
    is_bridge_active: bool,
    is_winter: bool,
    today_is_cheap: bool,
    is_on_battery: bool,
    wait_for_sun: bool,
) -> bool:
    """Gate for the Price Peak Reduction branch.

    Ported 1:1 from v1 with hour → quarter substitution.  The "look-ahead"
    check (v1: ``current_minute >= 50``) maps to the last 5 minutes of a
    quarter (``current_minute % 15 >= 10``).

    v1 equivalent: ``is_price_peak_mode_conditions_met``
    """
    if not is_price_peak_enabled or has_pv_excess:
        return False

    check_quarter_am = current_quarter in expensive_am_quarters
    check_quarter_pm = current_quarter in expensive_pm_quarters
    # Look ahead to next quarter in the final 5 min of the current quarter
    look_ahead = (current_minute % 15) >= 10
    check_next_am = look_ahead and next_quarter in expensive_am_quarters
    check_next_pm = look_ahead and next_quarter in expensive_pm_quarters

    period_active = check_quarter_am or check_next_am or check_quarter_pm or check_next_pm

    return bool(
        is_bridge_active
        or (
            (not is_winter or not today_is_cheap)
            and (not is_on_battery or wait_for_sun)
            and (wait_for_sun or period_active)
        )
    )


def _compute_cheap_price_conditions_met(
    is_cheap_boost_enabled: bool,
    current_hour_is_cheap: bool,
    not_using_addition: bool,
    not_winter_addition: bool,
    future_lowest_temp: float | None,
    hot_water_temp: float | None,
    hot_water_setpoint: float | None,
) -> bool:
    """Gate for the Cheap Price Intensify branch.

    v1 equivalent::

        is_cheap_price_conditions_met: >
          {{ is_cheap_boost_enabled and
             (current_hour_is_cheap and (not_using_addition or not_winter_addition) or
             (future_lowest_temp < -1 and rx95_hw_temp_now - rx95_hw_temp_set > -2)) }}
    """
    if not is_cheap_boost_enabled:
        return False
    if current_hour_is_cheap and (not_using_addition or not_winter_addition):
        return True
    # Also enter Cheap mode when it's going to be very cold and HW is near setpoint
    if (
        future_lowest_temp is not None
        and future_lowest_temp < -1
        and hot_water_temp is not None
        and hot_water_setpoint is not None
        and hot_water_temp - hot_water_setpoint > -2
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Helper: build a Health instance from a fresh Inputs snapshot
# ---------------------------------------------------------------------------

def assess_health(inputs: Inputs) -> Health:
    """Return a Health summary from an Inputs snapshot.

    Keeping this here (instead of coordinator.py) means tests can call it
    directly without standing up a coordinator.
    """
    return Health(
        has_price=inputs.current_price is not None,
        has_today_avg_price=inputs.today_avg_price is not None,
        has_price_quarters=len(inputs.price_quarters) == QUARTERS_PER_DAY,
        has_weather=len(inputs.weather_forecast) > 0,
        has_indoor_temp=inputs.indoor_temp is not None,
        has_outdoor_temp=inputs.outdoor_temp is not None,
        has_compressor_power=inputs.compressor_power_w is not None,
        has_aux_power=inputs.aux_power_w is not None,
        has_pump_activity=inputs.pump_activity is not None,
    )
