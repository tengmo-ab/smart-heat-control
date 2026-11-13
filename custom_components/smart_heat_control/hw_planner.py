"""Hot-water planner — make water for N showers a day, when it is cheapest.

Kept free of Home Assistant imports (like hw_aux_guard.py) so the detector and
the planner can be exercised directly in tests and replayed against history.

Why this exists
---------------
The v1 hot-water cascade moves a setpoint around 45-55 °C with price. The pump
tops the tank up as soon as it is ~1.5 °C under that setpoint, so in practice
hot water was produced about 7 times a day (measured on an RX95, autumn 2026),
most of them small top-ups of standby loss, and a shower was reheated at once,
in the expensive evening. Nearly all elpatron energy in the period (41 of 47
kWh) went into those hot-water runs — almost always runs that started while
the compressor was already at maximum speed for space heating.

The planner turns it around. The user states how many showers the household
wants per day; the tank is filled for that, once, in the cheapest window, and
otherwise left alone:

    Shower Reserve       setpoint at the pump's minimum. No top-ups; after a
                         shower the pump only lifts the cold bottom to here.
    Planned Production   the daily fill: cheapest window of quarter prices
                         that ends before the shower day starts.
    Shower Refill        showers are still due today and the tank cannot
                         cover them (e.g. 4 a day from a tank that holds 2).
    Mid-day Boost        PV excess: free energy, heat above default.

Two parts:

* ``ShowerTracker`` — recognises tappings in the tank temperature, keeps the
  history (for the hour-of-day profile) and the tank bookkeeping (showers used
  since the tank was last full). Persisted by the coordinator.
* ``HwPlanner`` — the per-cycle decision plus the latches around a running
  production. Runtime-only.

The shower day
--------------
Showers are counted per day starting at 04:00. The daily fill has to be done
by the "ready by" hour: the hour before which almost none of the household's
showers have started (learned from the tappings, 06:00 until enough are
known). Between 04:00 and that hour the tank only has to cover what is still
due today; from then on a tank that cannot cover it is refilled at once, and
otherwise the next fill is planned for tomorrow's "ready by".

Keeping the elpatron out
------------------------
A production is not *started* while the compressor has just been at maximum
speed or the elpatron has just been on — it waits for a calmer moment, at most
HW_MAX_DEFER_HOURS, and never when the tank is empty and a shower is due. If
the elpatron comes on during a production anyway, the production is stopped
and retried later (a couple of times at most).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from .const import (
    HW_AUX_ABORT_BLOCK_MINUTES,
    HW_AUX_GUARD_AUX_ON_W,
    HW_DAY_START_HOUR,
    HW_DEPLETED_BELOW_DEFAULT_C,
    HW_DRAW_CLOSE_AFTER_MINUTES,
    HW_DRAW_COLD_END_C,
    HW_DRAW_COLD_MIN_DROP_C,
    HW_DRAW_CONTINUE_C_PER_MIN,
    HW_DRAW_MAX_SAMPLE_GAP_MINUTES,
    HW_DRAW_MIN_DROP_C,
    HW_DRAW_SHORT_DROP_C,
    HW_DRAW_SHOWER_DROP_C,
    HW_DRAW_START_C_PER_MIN,
    HW_EARLY_STOP_BLOCK_MINUTES,
    HW_EARLY_STOP_GRACE_MINUTES,
    HW_EVENT_HISTORY_DAYS,
    HW_FILL_MAX_AGE_AT_READY_HOURS,
    HW_FULL_BELOW_DEFAULT_C,
    HW_HEAT_RATE_C_PER_MIN,
    HW_MAX_AUX_ABORTS,
    HW_MAX_DEFER_HOURS,
    HW_NO_START_MINUTES,
    HW_PRODUCTION_MARGIN_MINUTES,
    HW_PRODUCTION_MAX_MINUTES,
    HW_PRODUCTION_MIN_MINUTES,
    HW_PRODUCTION_TIMEOUT_HOURS,
    HW_PROFILE_MIN_EVENTS,
    HW_PV_BOOST_C,
    HW_READY_EARLY_SHARE,
    HW_READY_HOUR_DEFAULT,
    HW_READY_HOUR_MAX,
    HW_READY_HOUR_MIN,
    HW_RESERVE_TEMP_C,
    HW_STALE_AFTER_HOURS,
    HW_VACATION_PREPARE_HOURS,
    MAX_HW_TEMP,
    QUARTERS_PER_DAY,
)
from .models import HwMode, PumpActivity

_LOGGER = logging.getLogger(__name__)

_EPS = 1e-6
_QUARTER = timedelta(minutes=15)
# A reading this much above the previous one means the tank is being heated;
# less is sensor flicker during standby.
_HEATED_MIN_RISE_C = 0.3
# Standby loss, for telling a tapping from plain cooling across a sensor gap.
_STANDBY_LOSS_C_PER_HOUR = 0.3


# ---------------------------------------------------------------------------
# Tapping detection
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Draw:
    """One tapping seen in the tank temperature."""

    start: datetime
    end: datetime
    from_c: float
    to_c: float

    @property
    def drop(self) -> float:
        return self.from_c - self.to_c

    @property
    def showers(self) -> float:
        """Shower equivalents: 1 for a shower/bath, 0.5 for a short one."""
        drop = self.drop
        if drop >= HW_DRAW_SHOWER_DROP_C or (
            drop >= HW_DRAW_COLD_MIN_DROP_C and self.to_c <= HW_DRAW_COLD_END_C
        ):
            return 1.0
        if drop >= HW_DRAW_SHORT_DROP_C:
            return 0.5
        return 0.0

    def as_list(self) -> list[Any]:
        return [self.start.isoformat(), self.end.isoformat(), self.from_c, self.to_c]

    @classmethod
    def from_list(cls, raw: list[Any]) -> Draw:
        return cls(
            datetime.fromisoformat(raw[0]),
            datetime.fromisoformat(raw[1]),
            float(raw[2]),
            float(raw[3]),
        )


class ShowerTracker:
    """Tapping detector, tapping history and tank bookkeeping.

    Feed it every *changed* tank reading via ``note_temp``. The detection was
    validated on change-based history, so repeated identical readings should
    not be fed: a flat sample would end a tapping early.
    """

    def __init__(self) -> None:
        self.events: list[Draw] = []
        # Consumption booked without a recognisable tapping (sensor outage,
        # many small tappings): (when noticed, shower equivalents). Counted
        # as showers taken, kept out of the hourly profile.
        self.untimed: list[tuple[datetime, float]] = []
        # Shower equivalents drawn since the tank was last full.
        self.used_since_full: float = 0.0
        # Last time the tank was *heated* to full (rising reading at or above
        # the full level) — not the last time it merely still was that warm.
        self.last_full_at: datetime | None = None
        # Set whenever something worth persisting changed.
        self.dirty: bool = False
        self._prev: tuple[datetime, float] | None = None
        self._open: tuple[datetime, float] | None = None
        self._ready_hour: tuple[Any, int] | None = None

    # -- feeding -----------------------------------------------------------

    def note_temp(
        self, when: datetime, temp: float, default_hw_temp: float
    ) -> Draw | None:
        """Take one reading; return the tapping it closed, if any."""
        closed: Draw | None = None
        rising = False
        prev = self._prev
        if prev is not None:
            minutes = (when - prev[0]).total_seconds() / 60
            if minutes <= 0:
                return None
            rate = (temp - prev[1]) / minutes
            rising = temp >= prev[1] + _HEATED_MIN_RISE_C
            comparable = minutes <= HW_DRAW_MAX_SAMPLE_GAP_MINUTES
            if self._open is not None:
                if not (comparable and rate <= HW_DRAW_CONTINUE_C_PER_MIN):
                    closed = self._close()
            elif comparable and rate <= HW_DRAW_START_C_PER_MIN:
                self._open = prev
            elif not comparable:
                self._account_unobserved(prev, when, temp)
        self._prev = (when, temp)
        self._update_tank(when, temp, default_hw_temp, rising or prev is None)
        return closed

    def flush(self, now: datetime) -> Draw | None:
        """Close an open tapping that has had no new reading for a while."""
        if self._open is None or self._prev is None:
            return None
        if now - self._prev[0] < timedelta(minutes=HW_DRAW_CLOSE_AFTER_MINUTES):
            return None
        return self._close()

    @property
    def draw_in_progress(self) -> bool:
        return self._open is not None

    def _close(self) -> Draw | None:
        assert self._open is not None and self._prev is not None
        draw = Draw(self._open[0], self._prev[0], self._open[1], self._prev[1])
        self._open = None
        if draw.drop < HW_DRAW_MIN_DROP_C:
            return None
        self.events.append(draw)
        self.used_since_full += draw.showers
        self._ready_hour = None
        self._prune(draw.end)
        self.dirty = True
        return draw

    def _account_unobserved(
        self, prev: tuple[datetime, float], when: datetime, temp: float
    ) -> None:
        """A tapping during a sensor outage: book it, but keep it out of the
        hourly profile since nobody knows when it happened."""
        hours = (when - prev[0]).total_seconds() / 3600
        ghost = Draw(prev[0], when, prev[1] - _STANDBY_LOSS_C_PER_HOUR * hours, temp)
        if ghost.showers > 0:
            self._book_untimed(when, ghost.showers)

    def _book_untimed(self, when: datetime, showers: float) -> None:
        self.untimed.append((when, showers))
        self.used_since_full += showers
        self.dirty = True

    def _update_tank(
        self, when: datetime, temp: float, default_hw_temp: float, heated: bool
    ) -> None:
        if temp >= default_hw_temp - HW_FULL_BELOW_DEFAULT_C:
            if self.used_since_full:
                self.used_since_full = 0.0
                self.dirty = True
            if heated or self.last_full_at is None:
                self.last_full_at = when
                self.dirty = True
        elif (
            self._open is None
            and temp < default_hw_temp - HW_DEPLETED_BELOW_DEFAULT_C
            and self.used_since_full < 1.0
        ):
            # Cold at the sensor without a whole shower recorded: many small
            # tappings. Book the difference so taken and used stay in step.
            self._book_untimed(when, 1.0 - self.used_since_full)

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(days=HW_EVENT_HISTORY_DAYS)
        if self.events and self.events[0].start < cutoff:
            self.events = [e for e in self.events if e.start >= cutoff]
        if self.untimed and self.untimed[0][0] < cutoff:
            self.untimed = [u for u in self.untimed if u[0] >= cutoff]

    # -- queries -------------------------------------------------------------

    def showers_since(self, start: datetime) -> float:
        return sum(e.showers for e in self.events if e.start >= start) + sum(
            showers for when, showers in self.untimed if when >= start
        )

    def draws_since(self, start: datetime) -> list[Draw]:
        return [e for e in self.events if e.start >= start]

    def last_shower(self) -> Draw | None:
        for event in reversed(self.events):
            if event.showers > 0:
                return event
        return None

    def hourly_profile(self, tz: tzinfo) -> list[float]:
        """Shower equivalents per local hour of day over the kept history."""
        hours = [0.0] * 24
        for event in self.events:
            hours[event.start.astimezone(tz).hour] += event.showers
        return hours

    def ready_hour(self, tz: tzinfo, now: datetime) -> int:
        """Local hour the daily fill should be done by.

        The latest hour before which at most HW_READY_EARLY_SHARE of the known
        showers started. Hours before HW_READY_HOUR_MIN count as the end of
        the previous evening. Recomputed once per local date (and when a new
        tapping arrives), so the deadline does not move around mid-day.
        """
        today = now.astimezone(tz).date()
        if self._ready_hour is not None and self._ready_hour[0] == today:
            return self._ready_hour[1]
        hour = HW_READY_HOUR_DEFAULT
        counted = [e for e in self.events if e.showers > 0]
        if len(counted) >= HW_PROFILE_MIN_EVENTS:
            profile = self.hourly_profile(tz)
            total = sum(profile)
            hour = HW_READY_HOUR_MIN
            before = 0.0
            for candidate in range(HW_READY_HOUR_MIN + 1, HW_READY_HOUR_MAX + 1):
                before += profile[candidate - 1]
                if before > HW_READY_EARLY_SHARE * total:
                    break
                hour = candidate
        self._ready_hour = (today, hour)
        return hour

    # -- persistence ---------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "events": [e.as_list() for e in self.events],
            "untimed": [[when.isoformat(), showers] for when, showers in self.untimed],
            "used_since_full": self.used_since_full,
            "last_full_at": self.last_full_at.isoformat() if self.last_full_at else None,
            "prev": [self._prev[0].isoformat(), self._prev[1]] if self._prev else None,
        }

    def restore(self, data: dict[str, Any] | None) -> None:
        """Load persisted state; anything malformed is dropped, not raised."""
        if not isinstance(data, dict):
            return
        events: list[Draw] = []
        for raw in data.get("events") or []:
            try:
                events.append(Draw.from_list(raw))
            except (ValueError, TypeError, IndexError):
                continue
        self.events = sorted(events, key=lambda e: e.start)
        untimed: list[tuple[datetime, float]] = []
        for raw in data.get("untimed") or []:
            try:
                untimed.append((datetime.fromisoformat(raw[0]), float(raw[1])))
            except (ValueError, TypeError, IndexError):
                continue
        self.untimed = sorted(untimed)
        try:
            self.used_since_full = max(0.0, float(data.get("used_since_full") or 0.0))
        except (ValueError, TypeError):
            self.used_since_full = 0.0
        try:
            raw_full = data.get("last_full_at")
            self.last_full_at = datetime.fromisoformat(raw_full) if raw_full else None
        except (ValueError, TypeError):
            self.last_full_at = None
        try:
            prev = data.get("prev")
            self._prev = (datetime.fromisoformat(prev[0]), float(prev[1])) if prev else None
        except (ValueError, TypeError, IndexError):
            self._prev = None
        self._open = None
        self._ready_hour = None


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class HwPlan:
    """One cycle's hot-water plan.

    ``enabled`` = the planner switch (and Master) is on. ``active`` = enabled
    and the tank temperature is known, so the plan decides the setpoint.
    Enabled but not active (tank sensor not loaded yet after a restart, or
    unavailable) means: leave the setpoint alone — not fall back to the v1
    cascade, which could start a hot-water run at 45-55 °C.
    """

    active: bool
    mode: HwMode
    target_temp: float
    state: str      # reserve / waiting / producing / refilling / deferred / pv_boost / paused / away / disabled
    reason: str
    showers_per_day: int
    showers_per_tank: float
    showers_taken: float
    showers_available: float
    showers_needed_now: float
    ready_hour: int
    day_start: datetime
    ready_by: datetime
    fill_needed: bool
    planned_start: datetime | None = None
    planned_end: datetime | None = None
    latest_start: datetime | None = None
    estimated_minutes: int | None = None
    # "actual" = real quarter prices for the whole horizon, "assumed" = part
    # of tomorrow taken from today's prices, None = no prices (latest start).
    price_basis: str | None = None
    strained: bool = False
    enabled: bool = False
    deferred_since: datetime | None = None
    blocked_until: datetime | None = None
    producing_since: datetime | None = None
    last_result: str | None = None


def production_minutes(tank_temp: float, target_temp: float) -> int:
    """Estimated run time to heat the tank sensor to target, in whole quarters."""
    minutes = HW_PRODUCTION_MARGIN_MINUTES + max(0.0, target_temp - tank_temp) / HW_HEAT_RATE_C_PER_MIN
    minutes = min(max(minutes, HW_PRODUCTION_MIN_MINUTES), HW_PRODUCTION_MAX_MINUTES)
    return int(math.ceil(minutes / 15.0) * 15)


def cheapest_window(
    now: datetime,
    deadline: datetime,
    minutes: int,
    prices_today: tuple[float, ...],
    prices_tomorrow: tuple[float, ...],
) -> tuple[datetime, datetime, str | None]:
    """Cheapest run of ``minutes`` between now and ``deadline`` (local times).

    Returns (latest_start, planned_start, price_basis). Without usable prices
    the plan is to start as late as possible: least standby loss, and the
    hours before a morning deadline are usually the cheap ones anyway.
    """
    quarters = max(1, minutes // 15)
    latest_start = deadline - quarters * _QUARTER
    if len(prices_today) != QUARTERS_PER_DAY:
        return latest_start, latest_start, None

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    slots: list[tuple[datetime, float]] = []
    basis = "actual"
    index = now.hour * 4 + now.minute // 15
    while True:
        start = midnight + index * _QUARTER
        if start >= deadline:
            break
        if index < QUARTERS_PER_DAY:
            price = prices_today[index]
        elif index < 2 * QUARTERS_PER_DAY:
            if len(prices_tomorrow) == QUARTERS_PER_DAY:
                price = prices_tomorrow[index - QUARTERS_PER_DAY]
            else:
                price = prices_today[index - QUARTERS_PER_DAY]
                basis = "assumed"
        else:
            break
        slots.append((start, price))
        index += 1

    if len(slots) <= quarters:
        return latest_start, now, basis
    best_start, best_cost = 0, math.inf
    running = sum(price for _, price in slots[:quarters])
    for first in range(len(slots) - quarters + 1):
        if first:
            running += slots[first + quarters - 1][1] - slots[first - 1][1]
        if running < best_cost - _EPS:
            best_start, best_cost = first, running
    return latest_start, slots[best_start][0], basis


class HwPlanner:
    """Per-cycle decision and the latches around a running production."""

    def __init__(self) -> None:
        self.producing_since: datetime | None = None
        self.producing_mode: HwMode | None = None
        self.producing_target: float | None = None
        self.deferred_since: datetime | None = None
        self.blocked_until: datetime | None = None
        self.aux_aborts: int = 0
        self.last_result: str | None = None
        self._hw_seen: datetime | None = None
        # End of the last pause: the pump is not judged on what it did while
        # someone else held the setpoint.
        self._judge_from: datetime | None = None

    def reset(self) -> None:
        """Forget any running production (planner switched off)."""
        self.producing_since = None
        self.producing_mode = None
        self.producing_target = None
        self.deferred_since = None
        self.blocked_until = None
        self.aux_aborts = 0
        self._hw_seen = None
        self._judge_from = None

    @property
    def producing(self) -> bool:
        return self.producing_since is not None

    def update(
        self,
        *,
        now: datetime,
        tz: tzinfo,
        tracker: ShowerTracker,
        enabled: bool,
        tank_temp: float | None,
        pump: PumpActivity | None,
        aux_power_w: float | None,
        strained: bool,
        has_pv_excess: bool,
        prices_today: tuple[float, ...],
        prices_tomorrow: tuple[float, ...],
        showers_per_day: float,
        showers_per_tank: float,
        default_hw_temp: float,
        vacation_hours_left: float | None,
        paused: bool,
    ) -> HwPlan:
        """Advance one cycle.

        ``paused`` = something with higher priority owns the setpoint this
        cycle (Extra hot water, a legionella boost, Heating Priority after an
        over-long run). The plan is still returned, but a running production
        is neither judged nor ended on what the pump does meanwhile.
        """
        local = now.astimezone(tz)
        day_start = local.replace(
            hour=HW_DAY_START_HOUR, minute=0, second=0, microsecond=0
        )
        if day_start > local:
            day_start -= timedelta(days=1)
        ready_hour = tracker.ready_hour(tz, now)
        ready_by = day_start.replace(hour=ready_hour)
        # Early morning, before today's showers normally begin?
        before_ready = local < ready_by
        if not before_ready:
            ready_by += timedelta(days=1)

        away = (
            vacation_hours_left is not None
            and vacation_hours_left > HW_VACATION_PREPARE_HOURS
        )
        per_day = 0 if away else max(0, int(showers_per_day))
        capacity = max(1.0, float(showers_per_tank))
        taken = tracker.showers_since(day_start)
        stale = tracker.last_full_at is not None and (
            now - tracker.last_full_at >= timedelta(hours=HW_STALE_AFTER_HOURS)
        )
        available = 0.0 if stale else max(0.0, capacity - tracker.used_since_full)
        need_now = min(capacity, max(0.0, per_day - taken))

        common: dict[str, Any] = {
            "showers_per_day": per_day,
            "showers_per_tank": capacity,
            "showers_taken": taken,
            "showers_available": available,
            "showers_needed_now": need_now,
            "ready_hour": ready_hour,
            "day_start": day_start,
            "ready_by": ready_by,
            "strained": strained,
        }

        def plan(mode: HwMode, target: float, state: str, reason: str, **extra: Any) -> HwPlan:
            return HwPlan(
                active=enabled and tank_temp is not None,
                enabled=enabled,
                mode=mode,
                target_temp=target,
                state=state,
                reason=reason,
                deferred_since=self.deferred_since,
                blocked_until=self.blocked_until,
                producing_since=self.producing_since,
                last_result=self.last_result,
                **common,
                **extra,
            )

        if not enabled or tank_temp is None:
            self.reset()
            return plan(
                HwMode.SHOWER_RESERVE, HW_RESERVE_TEMP_C, "disabled",
                "Planner switched off" if not enabled else "Tank temperature unknown",
                fill_needed=False,
            )

        target = min(default_hw_temp, MAX_HW_TEMP)
        full_c = default_hw_temp - HW_FULL_BELOW_DEFAULT_C
        # What the tank must hold at ready_by: today's remaining showers if
        # that is still ahead, else a fresh day's worth for tomorrow.
        ready_need = need_now if before_ready else min(capacity, float(per_day))
        # Showers are due now and the tank cannot cover them → refill at once.
        shortage = per_day > 0 and not before_ready and available + _EPS < need_now
        fill_needed = (
            per_day > 0
            and tank_temp < full_c
            and (
                available + _EPS < ready_need
                or tracker.last_full_at is None
                or ready_by - tracker.last_full_at
                > timedelta(hours=HW_FILL_MAX_AGE_AT_READY_HOURS)
            )
        )

        window: dict[str, Any] = {"fill_needed": fill_needed}
        past_latest = False
        start_now = False
        if fill_needed or shortage:
            minutes = production_minutes(tank_temp, target)
            latest_start, planned_start, basis = cheapest_window(
                local, ready_by, minutes, prices_today, prices_tomorrow
            )
            past_latest = local >= latest_start
            start_now = planned_start <= local or past_latest
            window.update(
                estimated_minutes=minutes,
                price_basis=basis,
                latest_start=latest_start,
            )
            if not shortage:
                begin = local if start_now else planned_start
                window.update(planned_start=begin, planned_end=begin + timedelta(minutes=minutes))
        # Tank empty and a shower is due (or the daily fill can wait no
        # longer): comfort wins over every gate below.
        critical = (
            per_day > 0
            and available < 1.0 - _EPS
            and (shortage or (past_latest and available + _EPS < ready_need))
        )
        aux_on = aux_power_w is not None and aux_power_w >= HW_AUX_GUARD_AUX_ON_W

        # ---- A production is running ------------------------------------
        if self.producing:
            if paused:
                self._judge_from = now
            ended = None if paused else self._judge_production(
                now=now, tank_temp=tank_temp, pump=pump, aux_on=aux_on,
                critical=critical, full_c=full_c, wanted=per_day > 0,
            )
            if ended is None:
                assert self.producing_mode is not None and self.producing_target is not None
                refill = self.producing_mode == HwMode.SHOWER_REFILL
                return plan(
                    self.producing_mode,
                    self.producing_target,
                    "paused" if paused else ("refilling" if refill else "producing"),
                    "Refilling: more showers are due today" if refill
                    else "Daily fill in the cheapest window",
                    **window,
                )
            _LOGGER.info("SHC hot-water planner: production ended (%s)", ended)

        if self.blocked_until is not None and now >= self.blocked_until:
            self.blocked_until = None

        if away:
            self.deferred_since = None
            return plan(
                HwMode.SHOWER_RESERVE, HW_RESERVE_TEMP_C, "away",
                "Vacation: no showers planned", **window,
            )

        # ---- Should a production start? ---------------------------------
        wanted: HwMode | None = None
        if shortage:
            wanted = HwMode.SHOWER_REFILL
        elif fill_needed and start_now:
            wanted = HwMode.PLANNED_PRODUCTION

        if wanted is not None and not paused:
            gate: str | None = None
            if not critical:
                if self.blocked_until is not None:
                    gate = "Waiting after a stopped production"
                elif strained:
                    gate = "Waiting: compressor at maximum or elpatron just on"
                if (
                    gate is not None
                    and self.deferred_since is not None
                    and now - self.deferred_since >= timedelta(hours=HW_MAX_DEFER_HOURS)
                ):
                    gate = None
            if gate is not None:
                if self.deferred_since is None:
                    self.deferred_since = now
                return plan(
                    HwMode.SHOWER_RESERVE, HW_RESERVE_TEMP_C, "deferred", gate, **window
                )
            self.producing_since = now
            self.producing_mode = wanted
            self.producing_target = target
            self.deferred_since = None
            self._hw_seen = now if pump == PumpActivity.HOT_WATER else None
            self._judge_from = None
            refill = wanted == HwMode.SHOWER_REFILL
            _LOGGER.info(
                "SHC hot-water planner: starting %s (taken %.1f of %d today, "
                "%.1f available, tank %.1f °C)",
                "refill" if refill else "daily fill", taken, per_day, available, tank_temp,
            )
            return plan(
                wanted, target, "refilling" if refill else "producing",
                "Refilling: more showers are due today" if refill
                else "Daily fill in the cheapest window",
                **window,
            )
        if wanted is None:
            self.deferred_since = None
        elif paused:
            return plan(
                HwMode.SHOWER_RESERVE, HW_RESERVE_TEMP_C, "paused",
                "Waiting: another hot-water mode has priority", **window,
            )

        # ---- Nothing to produce ------------------------------------------
        if per_day > 0 and has_pv_excess and not strained and not paused:
            return plan(
                HwMode.MIDDAY_BOOST,
                min(default_hw_temp + HW_PV_BOOST_C, MAX_HW_TEMP),
                "pv_boost", "PV excess: storing free energy", **window,
            )
        if fill_needed:
            return plan(
                HwMode.SHOWER_RESERVE, HW_RESERVE_TEMP_C, "waiting",
                "Daily fill planned for the cheapest window", **window,
            )
        return plan(
            HwMode.SHOWER_RESERVE, HW_RESERVE_TEMP_C, "reserve",
            "Tank covers the showers due" if per_day > 0 else "No showers requested",
            **window,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _judge_production(
        self,
        *,
        now: datetime,
        tank_temp: float,
        pump: PumpActivity | None,
        aux_on: bool,
        critical: bool,
        full_c: float,
        wanted: bool,
    ) -> str | None:
        """Return why the running production ends this cycle, or None."""
        assert self.producing_since is not None and self.producing_target is not None
        in_hw = pump == PumpActivity.HOT_WATER
        if in_hw:
            self._hw_seen = now

        if not wanted:
            return self._end(now, "cancelled")
        if tank_temp >= self.producing_target - 1.0:
            return self._end(now, "completed")
        if aux_on and in_hw and not critical and self.aux_aborts < HW_MAX_AUX_ABORTS:
            self.aux_aborts += 1
            return self._end(
                now, "stopped: elpatron came on",
                block_minutes=HW_AUX_ABORT_BLOCK_MINUTES,
            )
        if pump is not None and not in_hw:
            if self._hw_seen is not None:
                since, limit = self._hw_seen, HW_EARLY_STOP_GRACE_MINUTES
            else:
                since, limit = self.producing_since, HW_NO_START_MINUTES
            if self._judge_from is not None:
                since = max(since, self._judge_from)
            if now - since >= timedelta(minutes=limit):
                if tank_temp >= full_c:
                    return self._end(now, "completed")
                return self._end(
                    now,
                    "pump stopped early" if self._hw_seen is not None
                    else "pump did not start",
                    block_minutes=HW_EARLY_STOP_BLOCK_MINUTES,
                )
        if now - self.producing_since >= timedelta(hours=HW_PRODUCTION_TIMEOUT_HOURS):
            return self._end(now, "timeout", block_minutes=HW_EARLY_STOP_BLOCK_MINUTES)
        return None

    def _end(self, now: datetime, result: str, *, block_minutes: int = 0) -> str:
        self.producing_since = None
        self.producing_mode = None
        self.producing_target = None
        self._hw_seen = None
        self._judge_from = None
        self.blocked_until = (
            now + timedelta(minutes=block_minutes) if block_minutes else None
        )
        self.last_result = result
        if result == "completed":
            self.aux_aborts = 0
        return result
