"""Dataclasses passed between the coordinator, computed-sensor layer, and the
decision cascade.

Three principles encoded in the types here:

1. **Optional roles use ``None``**. Every entity the user *might* not bind in
   the config flow is typed as ``X | None``. The decision cascade treats
   ``None`` as "feature unavailable" and skips branches that depend on it.

2. **Bad reads collapse to ``None``** too. A sensor that reports
   ``unavailable``/``unknown`` or an out-of-range value is read as ``None`` so
   downstream code only ever has to handle one absence model.

3. **``Health`` summarises capability**, not raw availability. The cascade asks
   "can I optimize for price?" instead of inspecting individual fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


# ---------------------------------------------------------------------------
# Mode enums — string values stay stable across releases. Changing them would
# break long-term graphs and automations that depend on the state string.
# ---------------------------------------------------------------------------
class Mode(StrEnum):
    DEFAULT = "Default"
    CHEAP_PRICE_INTENSIFY = "Cheap Price Intensify"
    PRICE_PEAK_REDUCTION = "Price Peak Reduction"
    WEATHER_ANTICIPATION = "Weather Anticipation Reduction"


class HwMode(StrEnum):
    DEFAULT = "Default"
    NIGHT_BOOST = "Night Boost"
    MIDDAY_BOOST = "Mid-day Boost"
    LEGIONELLA_BOOST = "Legionella Boost"
    HEATING_PRIORITY = "Heating Priority"
    PRICE_PEAK_REDUCTION = "Price Peak Reduction"


class PumpActivity(StrEnum):
    HEATING = "Heating"
    HOT_WATER = "Making Hot Water"
    IDLE = "Idle"
    DEFROSTING = "Defrosting"


# ---------------------------------------------------------------------------
# Forecast representation
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ForecastDay:
    """One day from a weather forecast attribute (SMHI daily format)."""

    day: date
    temperature: float | None   # daily high
    templow: float | None
    condition: str | None       # 'sunny' / 'partlycloudy' / 'rainy' / ...


# ---------------------------------------------------------------------------
# Inputs — snapshot of every entity value the cascade can touch
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Inputs:
    """Snapshot of upstream entities + integration-owned helpers.

    Every field that comes from an *optional* role or could be temporarily
    unavailable is typed as ``... | None`` (or defaults to a safe falsy value).
    The cascade is structured so that ``None`` in any one place degrades the
    affected branch without crashing the rest.
    """

    # --- Time -----------------------------------------------------------
    now: datetime               # timezone-aware, typically UTC

    # --- Heating system (required core) ---------------------------------
    indoor_temp: float | None
    outdoor_temp: float | None

    # --- Heating system (optional readbacks) ----------------------------
    climate_target_temp: float | None
    heat_curve: float | None
    hot_water_setpoint: float | None
    hot_water_temp: float | None
    hot_water_extra_on: bool | None
    pump_activity: PumpActivity | None

    # --- Power monitoring (optional) ------------------------------------
    compressor_power_w: float | None
    aux_power_w: float | None
    # When did aux_power_w last become 0?  Used to enforce the
    # "must have been 0 for ≥ NOT_USING_ADDITION_GRACE_SECONDS" rule.
    aux_power_zero_since: datetime | None
    # Integration-owned rolling sensor; None until enough samples exist.
    avg_compressor_power_last_hour_w: float | None
    heating_duration_last_hour: float | None    # 0..1 fraction of last hour

    # --- Pricing --------------------------------------------------------
    current_price: float | None         # öre/kWh from price sensor state
    today_avg_price: float | None       # öre/kWh from price_today sensor state
    # Raw quarter prices from the Nord Pool 'prices' attribute.
    # Tuple of exactly 96 floats (index 0 = 00:00 local, 95 = 23:45 local).
    # Empty tuple means the attribute was missing or malformed.
    price_quarters: tuple[float, ...] = field(default_factory=tuple)
    # Recent average — from an external statistics sensor (or None if unbound).
    # Represents electricity cost over the last ~7 days; used to detect whether
    # the current price is cheap even by historical standards.
    recent_avg_price: float | None = None

    # --- Weather (optional but strongly recommended) --------------------
    # Empty tuple = unavailable; cascade falls back to month-based winter
    # detection (WINTER_MONTHS_FALLBACK in const.py).
    weather_forecast: tuple[ForecastDay, ...] = field(default_factory=tuple)

    # --- Indoor statistics (optional) -----------------------------------
    # Max indoor temp over recent window — from an external statistics sensor
    # or computed internally by the coordinator's rolling buffer.
    indoor_max_recent_temp: float | None = None

    # --- Solar / battery (all optional) ---------------------------------
    has_pv_excess: bool = False
    is_on_battery: bool = False
    is_bridge_active: bool = False
    is_sunny_day: bool = False
    pv_forecast_today_kwh: float | None = None

    # --- Sun ------------------------------------------------------------
    is_sun_up: bool = False
    next_sunrise: datetime | None = None

    # --- User-controlled toggles (integration-owned switches) -----------
    master_enabled: bool = False
    cheap_price_enabled: bool = True
    price_peak_enabled: bool = True
    weather_anticipation_enabled: bool = True
    legionella_boost_enabled: bool = False
    survive_solar_enabled: bool = False

    # --- User-controlled scalars (integration-owned numbers) ------------
    default_indoor_temp: float = 22.0
    default_heat_curve: float = 4.0
    default_hw_temp: float = 50.0
    price_threshold: float = 100.0
    legionella_min_days: int = 6
    legionella_max_days: int = 10
    legionella_duration_hours: int = 2

    # --- Vacation / legionella state (integration-owned datetimes) ------
    vacation_end: datetime | None = None
    last_legionella_run: datetime | None = None
    legionella_boost_end: datetime | None = None

    # --- HW reduction state (integration-owned binary_sensor) -----------
    hw_reduction_active: bool = False


# ---------------------------------------------------------------------------
# Health — categorical "what can we still do?" view of upstream availability
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Health:
    """Capability summary so the cascade can degrade rather than crash.

    Robustness hierarchy:
        both price + weather ok  → full cascade
        only weather ok          → skip price-driven branches
        only price ok            → skip weather-driven branches
        neither                  → safe defaults only, no optimization
        no room-temp signal      → safe-mode, write nothing, wait
    """

    has_price: bool             # current_price is not None
    has_today_avg_price: bool   # today_avg_price is not None
    has_price_quarters: bool    # price_quarters has exactly 96 values
    has_weather: bool           # weather_forecast is non-empty
    has_indoor_temp: bool
    has_outdoor_temp: bool
    has_compressor_power: bool
    has_aux_power: bool
    has_pump_activity: bool

    @property
    def can_optimize_for_price(self) -> bool:
        return self.has_price and self.has_today_avg_price

    @property
    def can_optimize_for_weather(self) -> bool:
        return self.has_weather

    @property
    def has_room_temp_signal(self) -> bool:
        return self.has_indoor_temp and self.has_outdoor_temp

    @property
    def has_minimum_for_any_optimization(self) -> bool:
        return self.has_room_temp_signal and (
            self.can_optimize_for_price or self.can_optimize_for_weather
        )


# ---------------------------------------------------------------------------
# Computed — all derived booleans/values produced by computed.py.
# Kept in its own frozen dataclass so the cascade reads from a single,
# named bag and tests can supply synthetic values directly.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Computed:
    # --- Time ---
    current_hour: int           # local time
    current_minute: int
    current_quarter: int        # 0..95  (hour*4 + minute//15, local time)
    next_quarter: int           # (current_quarter + 1) % 96
    is_am: bool

    # --- Vacation / legionella ---
    is_vacation_active: bool
    hours_to_vacation_end: float
    is_legionella_boost_active: bool
    days_since_legionella: int

    # --- Price lists (quarter-based; empty frozensets when price unavailable) ---
    today_am_avg_price: float | None    # mean of quarters 0..47
    today_pm_avg_price: float | None    # mean of quarters 48..95
    cheap_quarters: frozenset[int]      # quarters where price < 90% of today_avg
    cheap_midday_quarters: frozenset[int]   # cheapest N quarters in AM + cheapest N in PM
    expensive_am_quarters: frozenset[int]   # most expensive N quarters in AM (00-12)
    expensive_pm_quarters: frozenset[int]   # most expensive N quarters in PM (12-24)
    peak_expensive_am_quarter: int | None   # single most expensive quarter in AM
    peak_expensive_pm_quarter: int | None   # single most expensive quarter in PM

    # --- Weather ---
    today_forecast_index: int           # which forecast[] entry is "today"
    future_highest_temp: float | None   # today or tomorrow high, depending on hour
    future_lowest_temp: float | None
    is_winter: bool
    is_night_cold: bool                 # future_lowest_temp <= 6
    today_forecast_condition: str | None  # 'sunny' / 'rainy' / ...
    time_to_sunrise_hours: float

    # --- Solar ---
    wait_for_sun: bool          # survive_solar_enabled AND pv_forecast > 20 kWh

    # --- Power state ---
    is_full_compressor: bool    # avg compressor power > FULL_COMPRESSOR_AVG_W
    is_heating_alot: bool       # is_full_compressor AND heating_duration > 0.8
    not_using_addition: bool    # aux == 0 AND has been 0 for grace period
    not_winter_addition: bool   # aux within winter-tolerance limits

    # --- Price cheapness ---
    today_is_cheap: bool
    current_hour_is_cheap: bool
    is_current_hour_cheap_or_avg: bool
    is_current_hour_cheaper_than_avg: bool
    is_next_pm_quarter_expensive: bool
    is_evening_expensive: bool          # any expensive PM quarters fall in 17-23

    # --- Vacation-adjusted defaults (used by both cascade branches and HW logic) ---
    default_indoor_temp_helper: float
    default_heat_curve_helper: float
    default_hw_temp_helper: float

    # --- Branch preconditions (pre-AND'd for readable cascade) ---
    weather_active_conditions_met: bool
    price_peak_conditions_met: bool
    cheap_price_conditions_met: bool
    # --- v2 overrides surfaced for diagnostics ---
    # True when summer-coast override is active (climate-only — HW unchanged).
    # When True, weather_active_conditions_met is also True, but separating the
    # signal lets users see *why* Weather Anticipation triggered.
    is_summer_coast: bool


# ---------------------------------------------------------------------------
# Decision — what to write back.  None means "leave entity untouched".
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ClimateDecision:
    mode: Mode
    target_temp: float | None
    target_curve: float | None


@dataclass(frozen=True, slots=True)
class HwDecision:
    mode: HwMode
    target_temp: float | None


@dataclass(frozen=True, slots=True)
class FullDecision:
    """Full output of one cascade evaluation."""

    climate: ClimateDecision
    hot_water: HwDecision
    health: Health
    # Set when health forced a degraded decision (weather-only / safe defaults).
    degraded_reason: str | None = None
    # Diagnostic trace — not persisted, surfaced as attributes.
    trace: tuple[str, ...] = field(default_factory=tuple)
    # True when the coordinator must write last_legionella_run + legionella_boost_end.
    start_legionella_boost: bool = False
