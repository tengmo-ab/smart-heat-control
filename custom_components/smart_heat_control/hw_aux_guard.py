"""HW aux guard — session latch that keeps the elpatron out of cold-weather HW.

Kept free of Home Assistant imports (like models.py and controller.py) so the
state machine can be exercised directly in tests. The coordinator owns one
instance, calls ``update()`` once per cycle after the cascade and the anti-flap
layer have run, and passes the result to ``controller.apply_hw_aux_guard``.

Two caps with separate lifetimes:

* **Climate cap** (indoor target + heat curve) lowers the space-heating demand
  so the pump stops racing to finish hot water. Held for the HW session,
  released early if the house gets genuinely cold or the cap has run too long.
* **Hot-water cap** (HW setpoint) makes the session itself shorter, so the
  house goes without heat for less time. Held for the session *and* a hold
  period after it: restoring the full setpoint the moment the pump reaches the
  capped one would make it start a fresh HW run straight away — a stop/restart
  loop driven by our own cap.

Lifecycle of one hot-water session::

    pump -> Making Hot Water      session starts
    aux seen (this session, or <= LOOKBACK before it), outdoor <= threshold,
    indoor above comfort floor    both caps arm
    aux stops                     caps HOLD — lowering the demand is what
                                  stopped it; releasing would bring it back
    indoor < comfort floor, or climate cap older than MAX_ACTIVE
                                  climate cap released for the rest of the
                                  session; HW cap stays (ending HW sooner is
                                  exactly what a cold house needs)
    pump -> Heating               session ends; climate restored at once,
                                  HW cap held for HW_HOLD, then restored
    pump -> Idle / unavailable    session ends only after RELEASE_GRACE

Hard releases, any time, cancelling both caps and any hold: guard switch off,
master off, legionella boost active or starting, Extra HW switch on. The last
two are HW runs where the elpatron is *meant* to run — high temperature by
design — and they can be long.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from .const import (
    HW_AUX_GUARD_AUX_LOOKBACK_MINUTES,
    HW_AUX_GUARD_AUX_ON_W,
    HW_AUX_GUARD_HW_HOLD_MINUTES,
    HW_AUX_GUARD_INDOOR_FLOOR_DELTA_C,
    HW_AUX_GUARD_MAX_ACTIVE_MINUTES,
    HW_AUX_GUARD_RELEASE_GRACE_MINUTES,
)
from .models import PumpActivity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GuardCaps:
    """What the guard wants capped this cycle."""

    climate: bool = False
    hot_water: bool = False

    @property
    def any(self) -> bool:
        return self.climate or self.hot_water


def _to_watts(value: str | float | None) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


class HwAuxGuard:
    """Per-HW-session latch. See module docstring for the lifecycle."""

    def __init__(self) -> None:
        self.aux_last_on: datetime | None = None
        self.session_start: datetime | None = None
        self.climate_active: bool = False
        self.hw_hold_until: datetime | None = None
        # Why the climate cap last let go — surfaced as a sensor attribute so
        # a session that ended early is explainable after the fact.
        self.last_release_reason: str | None = None
        self._session_last_seen: datetime | None = None
        # Armed at some point this session → HW cap holds until session end.
        self._session_guarded: bool = False
        # Climate cap soft-released this session → don't re-arm until the
        # next session, or we'd flap around the comfort floor.
        self._climate_blocked: bool = False
        self._armed_at: datetime | None = None

    # ------------------------------------------------------------------
    # Per-cycle update
    # ------------------------------------------------------------------

    def update(
        self,
        *,
        now: datetime,
        pump: PumpActivity | None,
        aux_power_w: float | None,
        outdoor_temp: float | None,
        indoor_temp: float | None,
        default_indoor_temp: float,
        outdoor_threshold_c: float,
        enabled: bool,
        master_enabled: bool,
        legionella_active: bool,
        hw_extra_on: bool,
    ) -> GuardCaps:
        """Advance the state machine one cycle; return which caps apply."""
        # Track aux regardless of session or switch state, so the lookback
        # window is populated when a session starts.
        if aux_power_w is not None and aux_power_w >= HW_AUX_GUARD_AUX_ON_W:
            self.note_aux_on(now)

        self._track_session(now, pump)

        hard_reason = (
            "disabled" if not (enabled and master_enabled)
            else "legionella boost" if legionella_active
            else "extra hot water" if hw_extra_on
            else None
        )
        if hard_reason is not None:
            if self.climate_active or self._session_guarded or self.hw_hold_until:
                _LOGGER.info("SHC HW aux guard: released (%s) — all caps lifted", hard_reason)
                self.last_release_reason = hard_reason
            self.climate_active = False
            self._session_guarded = False
            self.hw_hold_until = None
            return GuardCaps()

        if self.session_start is not None:
            if self.climate_active:
                self._check_soft_release(now, indoor_temp, default_indoor_temp)
            elif not self._climate_blocked and self._should_arm(
                pump, outdoor_temp, outdoor_threshold_c, indoor_temp, default_indoor_temp
            ):
                self.climate_active = True
                self._session_guarded = True
                self._armed_at = now
                self.last_release_reason = None
                _LOGGER.info(
                    "SHC HW aux guard: armed (outdoor %.1f °C <= %d °C, elpatron seen "
                    "around hot-water start) — lowering heating demand and HW setpoint",
                    outdoor_temp,
                    int(outdoor_threshold_c),
                )

        if self.hw_hold_until is not None and now >= self.hw_hold_until:
            self.hw_hold_until = None
        hw_cap = (
            (self.session_start is not None and self._session_guarded)
            or self.hw_hold_until is not None
        )
        return GuardCaps(climate=self.climate_active, hot_water=hw_cap)

    def note_aux_on(self, now: datetime) -> None:
        """Record an elpatron sighting. Also called straight from state events,
        so a short burst between two polling cycles still counts."""
        if self.aux_last_on is None or now > self.aux_last_on:
            self.aux_last_on = now

    # ------------------------------------------------------------------
    # Event filtering — which state changes deserve an immediate cycle
    # ------------------------------------------------------------------

    def wants_refresh_on_pump_change(
        self,
        old: str | None,
        new: str | None,
        *,
        outdoor_temp: float | None,
        outdoor_threshold_c: float,
    ) -> bool:
        """Entering HW in the cold: a session starts (and may arm on lookback
        aux) — skipped when it is known to be warmer than the threshold, so
        the guard costs no extra cycles in summer. Leaving HW for Heating
        while a climate cap is on: restore the house's demand now rather than
        up to a polling interval later. Everything else (Idle blips,
        Defrosting) is handled by the grace period on the next regular cycle
        anyway."""
        if old == new:
            return False
        hw = PumpActivity.HOT_WATER.value
        if new == hw:
            return outdoor_temp is None or outdoor_temp <= outdoor_threshold_c
        return old == hw and new == PumpActivity.HEATING.value and self.climate_active

    def wants_refresh_on_aux_change(self, old: str | None, new: str | None) -> bool:
        """Only an upward crossing of the aux threshold, and only while a HW
        session is running that could still arm. Aux turning *off* never
        matters — the latch deliberately ignores it."""
        old_w, new_w = _to_watts(old), _to_watts(new)
        crossed_up = (
            new_w is not None
            and new_w >= HW_AUX_GUARD_AUX_ON_W
            and (old_w is None or old_w < HW_AUX_GUARD_AUX_ON_W)
        )
        return (
            crossed_up
            and self.session_start is not None
            and not self.climate_active
            and not self._climate_blocked
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _track_session(self, now: datetime, pump: PumpActivity | None) -> None:
        if pump == PumpActivity.HOT_WATER:
            if self.session_start is None:
                self.session_start = now
            self._session_last_seen = now
            return
        if self.session_start is None:
            return
        # Heating means the HW run is definitively over and the house wants
        # its demand back now. Anything else may be a pause inside the run.
        gap = now - (self._session_last_seen or now)
        if pump == PumpActivity.HEATING or gap >= timedelta(
            minutes=HW_AUX_GUARD_RELEASE_GRACE_MINUTES
        ):
            self._end_session(now)

    def _end_session(self, now: datetime) -> None:
        if self._session_guarded:
            self.hw_hold_until = now + timedelta(minutes=HW_AUX_GUARD_HW_HOLD_MINUTES)
            _LOGGER.info(
                "SHC HW aux guard: hot-water session ended — heating demand restored, "
                "HW setpoint held low for %d min so the pump doesn't restart HW at once",
                HW_AUX_GUARD_HW_HOLD_MINUTES,
            )
            if self.climate_active:
                self.last_release_reason = "hot-water session ended"
        self.session_start = None
        self._session_last_seen = None
        self._session_guarded = False
        self._climate_blocked = False
        self._armed_at = None
        self.climate_active = False

    def _check_soft_release(
        self, now: datetime, indoor_temp: float | None, default_indoor_temp: float
    ) -> None:
        reason: str | None = None
        if (
            indoor_temp is not None
            and indoor_temp < default_indoor_temp - HW_AUX_GUARD_INDOOR_FLOOR_DELTA_C
        ):
            reason = "house below comfort floor"
        elif self._armed_at is not None and now - self._armed_at >= timedelta(
            minutes=HW_AUX_GUARD_MAX_ACTIVE_MINUTES
        ):
            reason = "max active time reached"
        if reason is None:
            return
        self.climate_active = False
        self._climate_blocked = True
        self.last_release_reason = reason
        _LOGGER.info(
            "SHC HW aux guard: heating demand restored (%s); HW setpoint stays "
            "capped until the session ends",
            reason,
        )

    def _should_arm(
        self,
        pump: PumpActivity | None,
        outdoor_temp: float | None,
        outdoor_threshold_c: float,
        indoor_temp: float | None,
        default_indoor_temp: float,
    ) -> bool:
        # Arm only while actually making HW; outdoor and aux are checked at
        # arming time only, so a -5.1 ↔ -4.9 sensor wobble can't flap the
        # latch mid-session.
        if pump != PumpActivity.HOT_WATER:
            return False
        if outdoor_temp is None or outdoor_temp > outdoor_threshold_c:
            return False
        # The comfort floor is the guard's safety net, so arming needs a real
        # indoor reading to lean on — and a house already below it gets its
        # heat, not a lower target.
        if (
            indoor_temp is None
            or indoor_temp < default_indoor_temp - HW_AUX_GUARD_INDOOR_FLOOR_DELTA_C
        ):
            return False
        if self.aux_last_on is None or self.session_start is None:
            return False
        window_start = self.session_start - timedelta(
            minutes=HW_AUX_GUARD_AUX_LOOKBACK_MINUTES
        )
        return self.aux_last_on >= window_start
