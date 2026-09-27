"""HW aux guard — session latch that keeps the elpatron out of cold-weather HW.

Kept free of Home Assistant imports (like models.py and controller.py) so the
state machine can be exercised directly in tests. The coordinator owns one
instance and calls ``update()`` once per cycle, after the cascade and the
anti-flap layer have run; ``controller.apply_hw_aux_guard`` then caps the
climate targets while ``update()`` returns True.

Lifecycle of one hot-water session::

    pump -> Making Hot Water      session starts
    aux seen (this session, or <= LOOKBACK before it) and outdoor <= threshold
                                  latch arms, targets capped
    aux stops                     latch HOLDS — lowering the demand is what
                                  stopped it; releasing would bring it back
    pump -> Heating               session ends, latch released, targets restored
    pump -> Idle / unavailable    session ends only after RELEASE_GRACE

Hard releases, any time: guard switch off, master off, legionella boost
active (the elpatron is *meant* to run then, and a 2 h boost is too long to
hold the house below its setpoint).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .const import (
    HW_AUX_GUARD_AUX_LOOKBACK_MINUTES,
    HW_AUX_GUARD_AUX_ON_W,
    HW_AUX_GUARD_RELEASE_GRACE_MINUTES,
)
from .models import PumpActivity

_LOGGER = logging.getLogger(__name__)


class HwAuxGuard:
    """Per-HW-session latch. See module docstring for the lifecycle."""

    def __init__(self) -> None:
        self.session_start: datetime | None = None
        self.aux_last_on: datetime | None = None
        self.active: bool = False
        self._session_last_seen: datetime | None = None

    def update(
        self,
        *,
        now: datetime,
        pump: PumpActivity | None,
        aux_power_w: float | None,
        outdoor_temp: float | None,
        outdoor_threshold_c: float,
        enabled: bool,
        master_enabled: bool,
        legionella_active: bool,
    ) -> bool:
        """Advance the state machine one cycle; return whether to cap targets."""
        # Track aux regardless of session or switch state, so the lookback
        # window is populated when a session starts.
        if aux_power_w is not None and aux_power_w >= HW_AUX_GUARD_AUX_ON_W:
            self.aux_last_on = now

        self._track_session(now, pump)

        if (
            not enabled
            or not master_enabled
            or legionella_active
            or self.session_start is None
        ):
            self._release(
                "disabled" if not (enabled and master_enabled)
                else "legionella boost" if legionella_active
                else None
            )
            return False

        if not self.active and self._should_arm(pump, outdoor_temp, outdoor_threshold_c):
            self.active = True
            _LOGGER.info(
                "SHC HW aux guard: armed (outdoor %.1f °C <= %d °C, elpatron seen "
                "during hot-water session) — capping heat curve and indoor target",
                outdoor_temp,
                int(outdoor_threshold_c),
            )
        return self.active

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
            self._release("hot-water session ended")
            self.session_start = None
            self._session_last_seen = None

    def _should_arm(
        self,
        pump: PumpActivity | None,
        outdoor_temp: float | None,
        outdoor_threshold_c: float,
    ) -> bool:
        # Arm only while actually making HW; outdoor and aux are checked at
        # arming time only, so a -5.1 ↔ -4.9 sensor wobble can't flap the
        # latch mid-session.
        if pump != PumpActivity.HOT_WATER:
            return False
        if outdoor_temp is None or outdoor_temp > outdoor_threshold_c:
            return False
        if self.aux_last_on is None or self.session_start is None:
            return False
        window_start = self.session_start - timedelta(
            minutes=HW_AUX_GUARD_AUX_LOOKBACK_MINUTES
        )
        return self.aux_last_on >= window_start

    def _release(self, reason: str | None) -> None:
        if self.active and reason is not None:
            _LOGGER.info("SHC HW aux guard: released (%s) — targets restored", reason)
        self.active = False
