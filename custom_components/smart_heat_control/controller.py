"""Decision cascade — ports the v1 ``comfortzone_settings_controller`` 1:1.

Cascade order is frozen; see v1_porting_notes.md.  Any deliberate fixes vs v1
are marked with ``# FIX (v2):``.
"""
from __future__ import annotations

from .const import (
    AUX_POWER_CHEAP_HW_WINTER_LIMIT_W,
    COMPRESSOR_LOW_POWER_CHEAP_W,
    MAX_CLIMATE_TEMP,
    MAX_HEAT_CURVE,
    MAX_HW_TEMP,
    MIN_CLIMATE_TEMP,
    MIN_HEAT_CURVE,
    MIN_HW_TEMP,
)
from .models import (
    ClimateDecision,
    Computed,
    FullDecision,
    Health,
    HwDecision,
    HwMode,
    Inputs,
    Mode,
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def decide(inputs: Inputs, computed: Computed, health: Health) -> FullDecision:
    """Run the full cascade and return what the coordinator should write.

    ``None`` in any target field means "leave that entity unchanged".
    """
    if not inputs.master_enabled:
        return FullDecision(
            climate=ClimateDecision(mode=Mode.DEFAULT, target_temp=None, target_curve=None),
            hot_water=HwDecision(mode=HwMode.DEFAULT, target_temp=None),
            health=health,
            degraded_reason="master_disabled",
        )

    if not health.has_room_temp_signal:
        return FullDecision(
            climate=ClimateDecision(mode=Mode.DEFAULT, target_temp=None, target_curve=None),
            hot_water=HwDecision(mode=HwMode.DEFAULT, target_temp=None),
            health=health,
            degraded_reason="no_room_temp_signal",
        )

    trace: list[str] = []
    climate = _decide_climate(inputs, computed, trace)
    hot_water = _decide_hot_water(inputs, computed, climate.mode)

    start_legionella = _should_start_legionella(inputs, computed, climate)
    if start_legionella:
        hot_water = HwDecision(mode=HwMode.LEGIONELLA_BOOST, target_temp=min(60.0, MAX_HW_TEMP))

    return FullDecision(
        climate=climate,
        hot_water=hot_water,
        health=health,
        start_legionella_boost=start_legionella,
        trace=tuple(trace),
    )


# ---------------------------------------------------------------------------
# Climate cascade
# ---------------------------------------------------------------------------

def _decide_climate(
    inputs: Inputs,
    computed: Computed,
    trace: list[str],
) -> ClimateDecision:
    d = computed.default_indoor_temp_helper
    dc = computed.default_heat_curve_helper

    # 1. Weather Anticipation Reduction
    if computed.weather_active_conditions_met:
        trace.append("climate:weather_anticipation")
        temp_adj = _weather_temp_adjust(inputs, computed)
        target_temp = round(max(d - temp_adj, MIN_CLIMATE_TEMP), 1)
        target_curve = round(_weather_anticipation_curve(inputs, computed), 1)
        return ClimateDecision(
            mode=Mode.WEATHER_ANTICIPATION,
            target_temp=target_temp,
            target_curve=target_curve,
        )

    # 2. Price Peak Reduction
    if computed.price_peak_conditions_met:
        trace.append("climate:price_peak")
        temp_adj = _price_peak_temp_adjustment(inputs, computed)
        curve_adj = _price_peak_curve_adjustment(inputs, computed)
        target_temp = round(max(d - temp_adj, MIN_CLIMATE_TEMP), 1)
        target_curve = round(max(dc - curve_adj, MIN_HEAT_CURVE), 1)
        return ClimateDecision(
            mode=Mode.PRICE_PEAK_REDUCTION,
            target_temp=target_temp,
            target_curve=target_curve,
        )

    # 3. Cheap Price Intensify
    if computed.cheap_price_conditions_met:
        trace.append("climate:cheap_price")
        target_temp = round(_cheap_price_temp(inputs, computed), 1)
        target_curve = round(
            min(_cheap_price_curve(inputs, computed), MAX_HEAT_CURVE), 1
        )
        return ClimateDecision(
            mode=Mode.CHEAP_PRICE_INTENSIFY,
            target_temp=target_temp,
            target_curve=target_curve,
        )

    # 4. Default
    trace.append("climate:default")
    target_temp = round(_default_target_temp(inputs, computed), 1)
    target_curve = round(_default_curve(inputs, computed), 1)
    return ClimateDecision(
        mode=Mode.DEFAULT,
        target_temp=target_temp,
        target_curve=target_curve,
    )


# ---------------------------------------------------------------------------
# Climate branch helpers
# ---------------------------------------------------------------------------

def _weather_temp_adjust(inputs: Inputs, computed: Computed) -> float:
    """v1 equivalent: ``temp_adjust`` inside the Weather Anticipation branch.

    v1::
        {{ 4 if indoor_max_recent_temp > default_indoor_temp or
               future_highest_temp >= 9 and is_sunny_day
           else (3 if indoor_max_recent_temp >= default_indoor_temp - 0.3
           else (2 if indoor_max_recent_temp >= default_indoor_temp - 0.8
           else 1)) }}
    """
    d = inputs.default_indoor_temp
    indoor_max = inputs.indoor_max_recent_temp
    fh = computed.future_highest_temp

    if indoor_max is not None and indoor_max > d:
        return 4.0
    if fh is not None and fh >= 9 and inputs.is_sunny_day:
        return 4.0
    if indoor_max is not None and indoor_max >= d - 0.3:
        return 3.0
    if indoor_max is not None and indoor_max >= d - 0.8:
        return 2.0
    return 1.0


def _weather_anticipation_curve(inputs: Inputs, computed: Computed) -> float:
    """v1 equivalent: ``weather_anticipation_curve``."""
    d = computed.default_heat_curve_helper
    fh = computed.future_highest_temp
    outdoor = inputs.outdoor_temp
    sunny = computed.today_forecast_condition in ("sunny", "partlycloudy") or inputs.is_sunny_day

    if (
        not computed.is_winter
        and fh is not None and fh >= 9
        and outdoor is not None and outdoor >= -3
        and sunny
    ):
        return max(1.0, MIN_HEAT_CURVE)
    if not computed.is_winter and computed.is_heating_alot:
        return max(d - 2.0, MIN_HEAT_CURVE)
    if inputs.indoor_max_recent_temp is not None and inputs.indoor_max_recent_temp >= inputs.default_indoor_temp - 0.8:
        return max(d - 1.0, MIN_HEAT_CURVE)
    return d


def _price_peak_temp_adjustment(inputs: Inputs, computed: Computed) -> float:
    """v1 equivalent: ``price_peak_temp_adjustment``.

    The v1 ``current_hour != peak_expensive_am/pm`` check maps to
    ``current_quarter != peak_expensive_am/pm_quarter`` in v2 (finer resolution,
    same intent: less aggressive on the exact peak quarter).
    """
    outdoor = inputs.outdoor_temp
    # True when we are NOT in the single most expensive quarter of either half.
    not_at_peak = (
        computed.current_quarter != computed.peak_expensive_am_quarter
        and computed.current_quarter != computed.peak_expensive_pm_quarter
    )

    if not computed.not_using_addition:
        return 1.5
    if computed.is_winter and not computed.not_winter_addition:
        return 1.0
    if computed.is_winter and computed.is_night_cold and not_at_peak:
        return 0.5
    if (outdoor is not None and outdoor < 6 or computed.is_night_cold) and not_at_peak:
        return 1.3
    if outdoor is not None and outdoor > 12:
        return 2.5
    return 2.0


def _price_peak_curve_adjustment(inputs: Inputs, computed: Computed) -> float:
    """v1 equivalent: ``price_peak_curve_adjustment``."""
    outdoor = inputs.outdoor_temp
    not_at_peak = (
        computed.current_quarter != computed.peak_expensive_am_quarter
        and computed.current_quarter != computed.peak_expensive_pm_quarter
    )

    if computed.is_winter and computed.is_night_cold and outdoor is not None and outdoor > -1 and not_at_peak:
        return 0.5
    if (outdoor is not None and outdoor < 6 or computed.is_night_cold) and not_at_peak:
        return 0.7
    if outdoor is not None and outdoor > 12:
        return 2.0
    if computed.is_winter and outdoor is not None and outdoor < -1:
        return 0.0
    return 1.1


def _cheap_price_temp(inputs: Inputs, computed: Computed) -> float:
    """v1 equivalent: ``cheap_price_temp``.

    v1::
        {% if not_using_addition and (not is_winter or d_indoor - indoor_temp < 0.5) %}
          {{ [indoor_temp + delta, max_climate_temp] | min }}
          # where delta = [[max_climate_temp - indoor_temp - 0.1, 1.0] | min, 0.5] | max
          #   unless (is_winter and is_full_compressor and not not_winter_addition) → delta = 0.5
        {% elif not_using_addition %}
          {{ [indoor_temp + 0.5, max_climate_temp] | min }}
        {% else %}
          {{ [indoor_temp, max_climate_temp] | min }}
        {% endif %}
    """
    indoor = inputs.indoor_temp if inputs.indoor_temp is not None else computed.default_indoor_temp_helper
    d = computed.default_indoor_temp_helper

    if computed.not_using_addition and (not computed.is_winter or d - indoor < 0.5):
        if not computed.is_winter or not computed.is_full_compressor or computed.not_winter_addition:
            headroom = MAX_CLIMATE_TEMP - indoor - 0.1
            delta = max(min(headroom, 1.0), 0.5)
        else:
            delta = 0.5
        return min(indoor + delta, MAX_CLIMATE_TEMP)
    if computed.not_using_addition:
        return min(indoor + 0.5, MAX_CLIMATE_TEMP)
    return min(indoor, MAX_CLIMATE_TEMP)


def _cheap_price_curve(inputs: Inputs, computed: Computed) -> float:
    """v1 equivalent: ``cheap_price_curve_adjustment`` applied to d.

    Uses ``COMPRESSOR_LOW_POWER_CHEAP_W`` (3400 W) threshold from v1.
    """
    d = computed.default_heat_curve_helper
    comp_w = inputs.compressor_power_w if inputs.compressor_power_w is not None else 0.0

    if computed.not_using_addition and comp_w < COMPRESSOR_LOW_POWER_CHEAP_W and not computed.is_full_compressor:
        return MAX_HEAT_CURVE
    if computed.not_using_addition and not computed.is_full_compressor:
        return min(d + 1.5, MAX_HEAT_CURVE)
    if computed.not_using_addition:
        return min(d + 1.0, MAX_HEAT_CURVE)
    return d


def _default_target_temp(inputs: Inputs, computed: Computed) -> float:
    """v1 equivalent: Default branch ``target_temp``.

    v1::
        {% if not_using_addition and is_winter and (today_is_cheap or is_night_cold) %}
          {{ [d, indoor_temp + 0.4, max_climate_temp] | min }}   {# min of three #}
        {% elif not_using_addition and (cheap_or_avg or next_pm_expensive or (on_battery and not_winter_addition)) %}
          {{ d }}
        {% elif not_using_addition or (has_pv_excess and not_winter_addition) %}
          {{ max(d - 0.5, MIN) }}
        {% elif not_winter_addition %}
          {{ max(d - 1.0, MIN) }}
        {% else %}
          {{ max(d - 1.5, MIN) }}
        {% endif %}
    """
    d = computed.default_indoor_temp_helper
    indoor = inputs.indoor_temp if inputs.indoor_temp is not None else d

    if computed.not_using_addition and computed.is_winter and (computed.today_is_cheap or computed.is_night_cold):
        return min(d, indoor + 0.4, MAX_CLIMATE_TEMP)
    if computed.not_using_addition and (
        computed.is_current_hour_cheap_or_avg
        or computed.is_next_pm_quarter_expensive
        or (inputs.is_on_battery and computed.not_winter_addition)
    ):
        return d
    if computed.not_using_addition or (inputs.has_pv_excess and computed.not_winter_addition):
        return max(d - 0.5, MIN_CLIMATE_TEMP)
    if computed.not_winter_addition:
        return max(d - 1.0, MIN_CLIMATE_TEMP)
    return max(d - 1.5, MIN_CLIMATE_TEMP)


def _default_curve(inputs: Inputs, computed: Computed) -> float:
    """v1 equivalent: Default branch ``curve_to_set``.

    v1::
        {% if (cheaper_than_avg and not_using_addition) or next_pm_quarter_expensive %}
          {{ min(d + 0.5, MAX) }}
        {% elif (not_using_addition or is_winter) and (pv_excess or on_battery or today_cheap
                or night_cold or cheap_or_avg) %}
          {{ d }}
        {% else %}
          {{ max(d - 0.5, MIN) }}
        {% endif %}
    """
    d = computed.default_heat_curve_helper

    if (computed.is_current_hour_cheaper_than_avg and computed.not_using_addition) or computed.is_next_pm_quarter_expensive:
        return min(d + 0.5, MAX_HEAT_CURVE)
    if (computed.not_using_addition or computed.is_winter) and (
        inputs.has_pv_excess
        or inputs.is_on_battery
        or computed.today_is_cheap
        or computed.is_night_cold
        or computed.is_current_hour_cheap_or_avg
    ):
        return d
    return max(d - 0.5, MIN_HEAT_CURVE)


# ---------------------------------------------------------------------------
# Hot-water cascade
# ---------------------------------------------------------------------------

def _decide_hot_water(
    inputs: Inputs,
    computed: Computed,
    main_mode: Mode,
) -> HwDecision:
    """v1 equivalent: ``hw_target_temp`` + ``hw_mode`` after the climate cascade.

    The temperature and mode were separate computations in v1; they are unified
    here into a single HwDecision that returns both at once.

    v1 cascade order (locked)::
        0. hot_water_extra switch ON
        1. cheap_price_conditions_met AND (not_using_addition OR winter-aux)
        2. (evening_expensive OR night_cold) AND 10≤h≤17 AND not_using_addition
        3. main_mode == PricePeak
        4. hw_reduction_active
        5. is_legionella_boost_active
        6. 0≤h≤4 AND cheaper_than_avg AND not_using_addition  (Night Boost)
        7. NOT not_using_addition AND (NOT winter OR aux > 1000)
        8. else → base_default_hw
    """
    d = computed.default_hw_temp_helper
    ch = computed.current_hour
    cq = computed.current_quarter
    aux_w = inputs.aux_power_w if inputs.aux_power_w is not None else 0.0
    hw_now = inputs.hot_water_temp

    # base_default_hw — used in branch 8 and internally
    if computed.is_current_hour_cheaper_than_avg or (
        computed.is_evening_expensive and computed.is_night_cold and ch <= 19
    ):
        base_default = d
    else:
        base_default = max(d - 5.0, MIN_HW_TEMP)

    # ---- Branch 0: HW extra switch is ON --------------------------------
    if inputs.hot_water_extra_on:
        if computed.is_legionella_boost_active:
            return HwDecision(mode=HwMode.LEGIONELLA_BOOST, target_temp=min(60.0, MAX_HW_TEMP))
        # Keep current setpoint (coordinator will not write if unchanged)
        return HwDecision(mode=HwMode.DEFAULT, target_temp=inputs.hot_water_setpoint)

    # ---- Branch 1: cheap price, aux off (or within winter tolerance) ----
    winter_aux_ok = computed.is_winter and aux_w < AUX_POWER_CHEAP_HW_WINTER_LIMIT_W
    if computed.cheap_price_conditions_met and (computed.not_using_addition or winter_aux_ok):
        if computed.is_legionella_boost_active:
            return HwDecision(mode=HwMode.LEGIONELLA_BOOST, target_temp=min(60.0, MAX_HW_TEMP))
        if inputs.has_pv_excess and computed.not_using_addition and hw_now is not None:
            return HwDecision(mode=HwMode.MIDDAY_BOOST, target_temp=min(hw_now + 5.0, MAX_HW_TEMP))
        return HwDecision(mode=HwMode.DEFAULT, target_temp=d)

    # ---- Branch 2: expensive evening / cold night, midday window --------
    if (
        (computed.is_evening_expensive or computed.is_night_cold)
        and 10 <= ch <= 17
        and computed.not_using_addition
    ):
        if computed.is_legionella_boost_active and main_mode != Mode.PRICE_PEAK_REDUCTION:
            return HwDecision(mode=HwMode.LEGIONELLA_BOOST, target_temp=min(60.0, MAX_HW_TEMP))
        # Prefer to heat HW now while free/cheap rather than at night
        if (cq in computed.cheap_midday_quarters) or (inputs.has_pv_excess and not computed.is_heating_alot):
            t = min((hw_now + 5.0 if hw_now is not None else d + 5.0), MAX_HW_TEMP)
            return HwDecision(mode=HwMode.MIDDAY_BOOST, target_temp=t)
        if inputs.is_on_battery or cq in computed.cheap_midday_quarters or computed.is_current_hour_cheaper_than_avg:
            return HwDecision(mode=HwMode.MIDDAY_BOOST, target_temp=min(d + 5.0, MAX_HW_TEMP))
        if cq in computed.cheap_quarters:
            return HwDecision(mode=HwMode.DEFAULT, target_temp=d)
        return HwDecision(mode=HwMode.DEFAULT, target_temp=max(d - 5.0, MIN_HW_TEMP))

    # ---- Branch 3: climate is in price peak → reduce HW -----------------
    if main_mode == Mode.PRICE_PEAK_REDUCTION:
        return HwDecision(mode=HwMode.PRICE_PEAK_REDUCTION, target_temp=max(40.0, MIN_HW_TEMP))

    # ---- Branch 4: HW reduction active (from sub-automation) -----------
    if inputs.hw_reduction_active:
        return HwDecision(mode=HwMode.HEATING_PRIORITY, target_temp=max(d - 15.0, MIN_HW_TEMP))

    # ---- Branch 5: legionella boost is active ---------------------------
    if computed.is_legionella_boost_active:
        return HwDecision(mode=HwMode.LEGIONELLA_BOOST, target_temp=min(60.0, MAX_HW_TEMP))

    # ---- Branch 6: night boost (00–04, cheap, aux off) ------------------
    if 0 <= ch <= 4 and computed.is_current_hour_cheaper_than_avg and computed.not_using_addition:
        return HwDecision(mode=HwMode.NIGHT_BOOST, target_temp=min(d + 5.0, MAX_HW_TEMP))

    # ---- Branch 7: aux running and not winter or aux too high -----------
    if not computed.not_using_addition and (not computed.is_winter or aux_w > AUX_POWER_CHEAP_HW_WINTER_LIMIT_W):
        return HwDecision(mode=HwMode.DEFAULT, target_temp=max(40.0, MIN_HW_TEMP))

    # ---- Branch 8: default ----------------------------------------------
    return HwDecision(mode=HwMode.DEFAULT, target_temp=base_default)


# ---------------------------------------------------------------------------
# Legionella trigger
# ---------------------------------------------------------------------------

def _should_start_legionella(
    inputs: Inputs,
    computed: Computed,
    climate: ClimateDecision,
) -> bool:
    """Determine whether to start a new legionella boost cycle this tick.

    v1 equivalent: the ``if:`` block that runs after the main HW cascade.

    v1 conditions::
        - legionella_boost_enabled is on
        - NOT is_legionella_boost_active
        - days_since_legionella >= min_legionella_days
        - current_main_mode != 'Price Peak Reduction'
        - days_since_legionella >= max_legionella_days OR is_current_hour_cheap_or_avg
        - current hw setpoint < 60.0  (extra guard inside the then-block)
    """
    if not inputs.legionella_boost_enabled:
        return False
    if computed.is_legionella_boost_active:
        return False
    if computed.days_since_legionella < inputs.legionella_min_days:
        return False
    if climate.mode == Mode.PRICE_PEAK_REDUCTION:
        return False
    if not (
        computed.days_since_legionella >= inputs.legionella_max_days
        or computed.is_current_hour_cheap_or_avg
    ):
        return False
    # Guard: don't re-trigger if setpoint already at 60
    current_setpoint = inputs.hot_water_setpoint or 0.0
    return current_setpoint < 60.0

