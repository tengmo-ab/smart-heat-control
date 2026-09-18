"""Constants for the Smart Heat Control integration."""
from __future__ import annotations

DOMAIN = "smart_heat_control"

# ---------------------------------------------------------------------------
# Configuration keys (entity-role bindings the user picks in the config flow)
# ---------------------------------------------------------------------------
# Required heating-system roles
CONF_CLIMATE_ENTITY = "climate_entity"
CONF_OUTDOOR_TEMP_SENSOR = "outdoor_temp_sensor"

# Optional indoor-temp override if climate.<entity>.attributes.current_temperature
# isn't reliable (e.g. exhaust-air pumps that report a smoothed value).
CONF_INDOOR_TEMP_SENSOR = "indoor_temp_sensor"

# Optional heating-system roles — omitting any of these disables the
# corresponding branch in the controller cascade.
CONF_HEAT_CURVE_NUMBER = "heat_curve_number"
CONF_HOT_WATER_SETPOINT_NUMBER = "hot_water_setpoint_number"
CONF_HOT_WATER_EXTRA_SWITCH = "hot_water_extra_switch"
CONF_HOT_WATER_TEMP_SENSOR = "hot_water_temp_sensor"
# Sensor that reports one of: Heating / Making Hot Water / Idle / Defrosting.
CONF_PUMP_ACTIVITY_SENSOR = "pump_activity_sensor"

# Power monitoring (optional)
CONF_COMPRESSOR_POWER_SENSOR = "compressor_power_sensor"
CONF_AUX_POWER_SENSOR = "aux_power_sensor"

# Pricing
CONF_PRICE_SENSOR = "price_sensor"
# Today's quarter-resolution prices. Required for Price Peak Reduction,
# AM/PM split, expensive_evening detection, and today's average price.
# Without this sensor we operate on the current price only — much weaker
# optimization. Nord Pool exposes 96 quarters in the ``prices`` attribute
# (or 96 dicts in ``data``). The state itself is the *current* quarter
# price, not the average — today_avg is computed from the quarters.
CONF_PRICE_TODAY_SENSOR = "price_today_sensor"

# Weather (recommended)
CONF_WEATHER_FORECAST_ENTITY = "weather_forecast_entity"

# Solar / PV / battery (all optional)
CONF_PV_EXCESS_BINARY_SENSOR = "pv_excess_binary_sensor"
CONF_SOLAR_FORECAST_TODAY_SENSOR = "solar_forecast_today_sensor"
CONF_BATTERY_DISCHARGING_BINARY_SENSOR = "battery_discharging_binary_sensor"
CONF_BRIDGE_TO_SOLAR_BINARY_SENSOR = "bridge_to_solar_binary_sensor"
CONF_IS_SUNNY_DAY_BINARY_SENSOR = "is_sunny_day_binary_sensor"

# Defaults the user can tune at install-time. These also seed the integration-
# owned number entities (which the controller reads at runtime).
CONF_DEFAULT_INDOOR_TEMP = "default_indoor_temp"
CONF_DEFAULT_HEAT_CURVE = "default_heat_curve"
CONF_DEFAULT_HW_TEMP = "default_hw_temp"
CONF_PRICE_THRESHOLD = "price_threshold"
CONF_LEGIONELLA_MIN_DAYS = "legionella_min_days"
CONF_LEGIONELLA_MAX_DAYS = "legionella_max_days"
CONF_LEGIONELLA_DURATION_HOURS = "legionella_duration_hours"

# ---------------------------------------------------------------------------
# Default values (mirror v1's input_number defaults so behaviour ports 1:1)
# ---------------------------------------------------------------------------
DEFAULT_INDOOR_TEMP = 22.0
DEFAULT_HEAT_CURVE = 4.0
DEFAULT_HW_TEMP = 50.0
DEFAULT_PRICE_THRESHOLD = 100  # öre/kWh — tröskel ovan vilken "billigt" inte tillämpas
DEFAULT_LEGIONELLA_MIN_DAYS = 6
DEFAULT_LEGIONELLA_MAX_DAYS = 10
DEFAULT_LEGIONELLA_DURATION_HOURS = 2

# Passive legionella detection — credit a pasteurization event whenever the
# hot-water tank reaches LEGIONELLA_PASSIVE_THRESHOLD_C and sustains it for
# LEGIONELLA_PASSIVE_SUSTAIN_MINUTES, regardless of which mode caused the
# heat (Mid-day Boost, manual extra HW, Comfortzone-internal, etc.). Without
# this, days_since_legionella only counts SHC-triggered boosts and grows
# unbounded when the user has the legionella switch off — even though the
# tank gets hot regularly via normal operation. WHO/Boverket guidance:
# 60 °C sustained for 30 min kills legionella.
LEGIONELLA_PASSIVE_THRESHOLD_C = 60.0
LEGIONELLA_PASSIVE_SUSTAIN_MINUTES = 30
# 1 °C hysteresis below threshold before resetting the session timer — so a
# 5-min cycle that reads 59.5 °C doesn't restart the 30-min count.
LEGIONELLA_PASSIVE_EXIT_C = 59.0

# Safety bounds (matchar v1 min_climate_temp / max_climate_temp osv.)
MIN_CLIMATE_TEMP = 10.0
MAX_CLIMATE_TEMP = 25.0
MIN_HEAT_CURVE = 0.0
MAX_HEAT_CURVE = 6.0
MIN_HW_TEMP = 30.0
MAX_HW_TEMP = 60.0

# Re-evaluation interval. Matchar v1:s `time_pattern: minutes: /5`.
CONTROL_INTERVAL_SECONDS = 300

# Delay applied between writes inside one cycle, mirroring v1:s `delay: 00:00:05`
# som finns för att den underliggande integrationen inte ska tappa skrivningar.
WRITE_SETTLE_DELAY_SECONDS = 5

# Summer mode — climate-only "no heating needed" override modelled on the
# Comfortzone built-in summer mode, but driven by a *regime* signal rather
# than a spot reading.
#
# WHY A REGIME SIGNAL (fixed 2026-09-18):
# The first implementation gated on a rolling 6 h outdoor max against a fixed
# 12 °C threshold. That works from June to August, when the overnight low
# stays above the threshold. In the shoulder season the diurnal swing is
# 9-11 K, so the same signal crosses the threshold *twice every day*: summer
# mode dropped out in the small hours and came back mid-morning, every night,
# for weeks. A detector whose input has a daily cycle cannot describe a
# season no matter how much hysteresis is layered on top.
#
# The replacement uses the forecast daily MEAN — (high + low) / 2 — weighted
# across today and tomorrow. The mean has no daily cycle, and it carries the
# season by construction: a 17/7 September day means 12 °C while a 20/13 June
# day means 16.5 °C, even though both "feel" like a 17-20 °C afternoon. No
# calendar date and no user-tunable number is involved.
SUMMER_REGIME_TODAY_WEIGHT = 0.6   # today vs tomorrow in the weighted mean

# Schmitt trigger. The exit threshold is a fixed comfort floor: below this
# daily mean the building wants base heat whatever the afternoon peak looks
# like. The entry threshold sits a band above it, so leaving summer mode is
# easy and re-entering requires a genuinely summer-like regime.
SUMMER_REGIME_EXIT_C = 12.5

# The band is *dynamic*: it scales with the forecast diurnal swing, which is
# precisely the quantity that made the old detector noisy. A tight midsummer
# swing (6-8 K) gives a ~2 K band; a 10-11 K shoulder-season swing gives the
# full 3 K, which is what keeps a mild October afternoon from re-arming the
# gate. Unknown swing → widest band (hardest to enter = safest).
SUMMER_REGIME_BAND_FACTOR = 0.3
SUMMER_REGIME_BAND_MIN_C = 1.0
SUMMER_REGIME_BAND_MAX_C = 3.0

# Entry-only sanity checks. These never force an *exit* — an exit driven by
# a daily-cycle signal is exactly the bug above. They exist so a single warm
# forecast day, or a forecast that disagrees with reality, cannot arm the
# gate on its own.
SUMMER_TODAY_HIGH_C = 15.0     # °C — today's daily high (SMHI shade-measured;
                               # in sun it feels ~3-5 °C warmer)
SUMMER_TOMORROW_HIGH_C = 13.0  # °C — tomorrow's daily high (don't coast if a cold day follows)
SUMMER_OUTDOOR_C = 12.0        # °C — measured outdoor "recently warm" confirmation
# Rolling window for the measured-outdoor confirmation. Kept at 6 h: as an
# entry-only check it just answers "has it actually been mild today", and a
# longer window would let yesterday's warmth arm the gate on a cold morning.
SUMMER_OUTDOOR_MAX_WINDOW_HOURS = 6
# How many °C below `default_indoor_temp` the indoor reading is allowed to
# drift before summer mode bails out. This is the one check that acts
# immediately in both directions — comfort always wins over the regime.
# 1.0 °C (so default 21 → 20.0 °C floor) is wide enough that a routine
# pre-dawn dip doesn't trigger it.
SUMMER_INDOOR_FLOOR_DELTA_C = 1.0

# Anti-overheat override thresholds for Weather Anticipation.
# When indoor_max_recent_temp >= default + WEATHER_OVERRIDE_INDOOR_DELTA_C
# AND future_highest_temp >= WEATHER_OVERRIDE_FUTURE_HIGH_C (and not winter),
# Weather Anticipation triggers regardless of current price.
# v1 always required current_price >= today_avg to trigger Weather
# Anticipation — meaning that on a cheap-but-warm night before a sunny
# warm day, the cascade would fall through to Cheap Price Intensify and
# pre-heat an already-warm house. v2 adds this override so we trust the
# coming solar gain instead of dumping electricity into a building that
# will overheat anyway.
WEATHER_OVERRIDE_INDOOR_DELTA_C = 1.0   # °C above default_indoor_temp
WEATHER_OVERRIDE_FUTURE_HIGH_C = 12.0   # °C — clearly warm spring/summer day

# Anti-flap: block re-entry into a higher-demand mode for this long after
# leaving it. Downgrades (toward less compressor strain) are never blocked —
# we always allow Cheap Price → Default → Price Peak transitions immediately.
# Only UPGRADE transitions (Default → Cheap Price, Price Peak → Default,
# Default → Night Boost, …) are subject to the cooldown. Prevents the
# compressor from being asked to accelerate-then-decelerate every few minutes
# when prices/conditions oscillate near a threshold. User-initiated switch
# toggles bypass this cooldown (see _detect_user_action in coordinator).
MODE_REENTRY_COOLDOWN_SECONDS = 1200  # 20 min

# ---------------------------------------------------------------------------
# Optimization mode strings — exposed via select.<>_optimization_mode and
# select.<>_hw_mode. Stable strings: byts inte utan migrations-script eftersom
# de hamnar i state-historiken.
# ---------------------------------------------------------------------------
MODE_DEFAULT = "Default"
MODE_CHEAP_PRICE_INTENSIFY = "Cheap Price Intensify"
MODE_PRICE_PEAK_REDUCTION = "Price Peak Reduction"
MODE_WEATHER_ANTICIPATION = "Weather Anticipation Reduction"

OPTIMIZATION_MODES: tuple[str, ...] = (
    MODE_DEFAULT,
    MODE_CHEAP_PRICE_INTENSIFY,
    MODE_PRICE_PEAK_REDUCTION,
    MODE_WEATHER_ANTICIPATION,
)

HW_MODE_DEFAULT = "Default"
HW_MODE_NIGHT_BOOST = "Night Boost"
HW_MODE_MIDDAY_BOOST = "Mid-day Boost"
HW_MODE_LEGIONELLA_BOOST = "Legionella Boost"
HW_MODE_HEATING_PRIORITY = "Heating Priority"
HW_MODE_PRICE_PEAK_REDUCTION = "Price Peak Reduction"

HW_MODES: tuple[str, ...] = (
    HW_MODE_DEFAULT,
    HW_MODE_NIGHT_BOOST,
    HW_MODE_MIDDAY_BOOST,
    HW_MODE_LEGIONELLA_BOOST,
    HW_MODE_HEATING_PRIORITY,
    HW_MODE_PRICE_PEAK_REDUCTION,
)

# Pump-activity sensor states the controller reasons over.
PUMP_STATE_HEATING = "Heating"
PUMP_STATE_HOT_WATER = "Making Hot Water"
PUMP_STATE_IDLE = "Idle"
PUMP_STATE_DEFROSTING = "Defrosting"

# ---------------------------------------------------------------------------
# Hard-coded thresholds in the v1 logic. Kept named here so the controller
# stays declarative; values are not exposed to the user since changing them
# would alter the proven 2-year cascade behaviour.
# ---------------------------------------------------------------------------
# Average compressor power (W) over the last hour above which we treat the
# pump as running flat-out.
FULL_COMPRESSOR_AVG_W = 2000

# Aux/electrical-heater must have read 0 W for at least this long before we
# treat it as truly off (v1: 1800 s grace inside `not_using_addition`).
NOT_USING_ADDITION_GRACE_SECONDS = 1800

# In hard winter we tolerate some aux draw without abandoning a mode.
AUX_ALLOWED_HARD_WINTER_W = 600  # vid future_highest < 0 eller future_lowest < -4
AUX_ALLOWED_DEFAULT_W = 0

# HW reduction aux-automation timings (matchar v1 sub-automationerna).
HW_REDUCTION_TRIGGER_HOURS = 1.5
HW_REDUCTION_HEATING_TIMEOUT_HOURS = 1
HW_REDUCTION_NO_HEATING_TIMEOUT_MINUTES = 10

# Day-half boundary (v1: current_hour < 12 ⇒ AM).
AM_BOUNDARY_HOUR = 12

# Fallback "is_winter" months when no usable weather forecast is available.
WINTER_MONTHS_FALLBACK: tuple[int, ...] = (11, 12, 1, 2)

# ---------------------------------------------------------------------------
# Price analysis — quarter-based (Nord Pool now delivers 15-min resolution)
# ---------------------------------------------------------------------------
QUARTERS_PER_HOUR = 4
QUARTERS_PER_DAY = 96           # 24 × 4

# A quarter is "cheap" when its price is below this fraction of today's avg.
# Mirrors v1's `current_price <= 0.9 * today_avg_price`.
CHEAP_PRICE_RATIO = 0.9

# A quarter is "cheap-or-avg" at 100% of today's avg (used for weaker signal).
CHEAP_OR_AVG_PRICE_RATIO = 1.0

# "Really cheap" compared to recent history — extra boost signal.
VERY_CHEAP_VS_RECENT_RATIO = 0.5   # v1: current_price < 0.5 * recent_avg_price

# Expensive quarter detection — top N quarters per half-day.
# 12 quarters = 3 hours, matching v1's 3-most-expensive-hours logic.
EXPENSIVE_QUARTER_COUNT = 12

# Cheapest midday selection — same count (3 hours × 4 quarters).
# Used for cheap_midday_quarters: cheapest N in AM + cheapest N in PM.
CHEAPEST_MIDDAY_QUARTER_COUNT = 12

# Evening window used for is_evening_expensive (17:00–24:00 local).
EXPENSIVE_EVENING_START_HOUR = 17

# From what local hour onward we look at "future_highest_temp" as tomorrow's
# high rather than today's (mirrors v1: `current_hour < 16`).
FUTURE_TEMP_TOMORROW_THRESHOLD_HOUR = 16

# PV forecast threshold for "survive on solar" mode (kWh expected today).
WAIT_FOR_SUN_PV_KWH_THRESHOLD = 20.0

# Compressor power (W) threshold used in Cheap Price curve boost:
# if compressor < this AND not at full power → set curve to MAX.
COMPRESSOR_LOW_POWER_CHEAP_W = 3400

# Aux/addition power (W) limit for the HW winter-mode bypass:
# if aux < this we still allow cheap-mode and mid-day HW boost in winter.
AUX_POWER_CHEAP_HW_WINTER_LIMIT_W = 1000
