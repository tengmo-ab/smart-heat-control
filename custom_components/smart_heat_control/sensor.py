"""Diagnostic sensor entities exposing computed values."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SmartHeatControlCoordinator
from .hw_planner import HwPlan
from .models import FullDecision


@dataclass(frozen=True)
class SensorDef:
    key: str
    name: str
    unit: str | None
    icon: str
    state_class: SensorStateClass | None = None
    # None = main "Sensors" section on the device page (prominent).
    # EntityCategory.DIAGNOSTIC = grouped under "Diagnostic" (collapsed by default).
    category: EntityCategory | None = None


_SENSORS: tuple[SensorDef, ...] = (
    # Main outputs — what the cascade actually decided. Keep prominent.
    SensorDef(
        "target_temp", "Target Temperature",
        UnitOfTemperature.CELSIUS, "mdi:thermometer", SensorStateClass.MEASUREMENT,
    ),
    SensorDef(
        "target_heat_curve", "Target Heat Curve",
        None, "mdi:chart-bell-curve", SensorStateClass.MEASUREMENT,
    ),
    # Diagnostic — useful info but not the headline.
    SensorDef(
        "today_am_avg_price", "Today AM Avg Price",
        "öre/kWh", "mdi:cash-clock", SensorStateClass.MEASUREMENT,
        EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        "today_pm_avg_price", "Today PM Avg Price",
        "öre/kWh", "mdi:cash-clock", SensorStateClass.MEASUREMENT,
        EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        "future_highest_temp", "Future Highest Temperature",
        UnitOfTemperature.CELSIUS, "mdi:thermometer-chevron-up",
        SensorStateClass.MEASUREMENT,
        EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        "days_since_legionella", "Days Since Legionella",
        "d", "mdi:bacteria-outline", SensorStateClass.MEASUREMENT,
        EntityCategory.DIAGNOSTIC,
    ),
    SensorDef(
        "degraded_reason", "Degraded Reason",
        None, "mdi:alert-circle-outline",
        category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartHeatControlCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        SmartHeatControlSensor(coordinator, entry, defn) for defn in _SENSORS
    )
    async_add_entities(
        HotWaterPlannerSensor(coordinator, entry, defn) for defn in _HW_SENSORS
    )


class SmartHeatControlSensor(
    CoordinatorEntity[SmartHeatControlCoordinator], SensorEntity
):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SmartHeatControlCoordinator,
        entry: ConfigEntry,
        defn: SensorDef,
    ) -> None:
        super().__init__(coordinator)
        self._defn = defn
        self._attr_unique_id = f"{entry.entry_id}_{defn.key}"
        self._attr_name = defn.name
        self._attr_native_unit_of_measurement = defn.unit
        self._attr_icon = defn.icon
        self._attr_state_class = defn.state_class
        self._attr_entity_category = defn.category

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.entry.entry_id)},
            name="Smart Heat Control",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> Any:
        decision: FullDecision | None = self.coordinator.data
        if decision is None:
            return None

        key = self._defn.key
        if key == "today_am_avg_price":
            # Computed values not stored in FullDecision — re-derive from coordinator
            val = self._from_computed("today_am_avg_price")
            return round(val) if val is not None else None
        if key == "today_pm_avg_price":
            val = self._from_computed("today_pm_avg_price")
            return round(val) if val is not None else None
        if key == "future_highest_temp":
            return self._from_computed("future_highest_temp")
        if key == "days_since_legionella":
            return self._from_computed("days_since_legionella")
        if key == "target_temp":
            return decision.climate.target_temp
        if key == "target_heat_curve":
            return decision.climate.target_curve
        if key == "degraded_reason":
            # None → "ok" so users can tell "everything healthy" from
            # "the sensor itself is broken" (the latter renders as unknown).
            return decision.degraded_reason or "ok"
        return None

    def _from_computed(self, attr: str) -> Any:
        # The coordinator doesn't expose the Computed object, but we can read
        # it from the decision trace or re-derive. For diagnostic sensors,
        # we store a snapshot of the last Computed on the coordinator.
        computed = getattr(self.coordinator, "_last_computed", None)
        if computed is None:
            return None
        return getattr(computed, attr, None)


# ---------------------------------------------------------------------------
# Hot-water planner sensors (hw_planner.py). They follow the tapping detector,
# which runs whenever a tank sensor is bound — also with the planner switch
# off — so the household's shower pattern is visible before opting in.
# ---------------------------------------------------------------------------
_HW_SENSORS: tuple[SensorDef, ...] = (
    SensorDef(
        "hw_showers_today", "Showers Today",
        None, "mdi:shower-head", SensorStateClass.MEASUREMENT,
    ),
    SensorDef(
        "hw_showers_available", "Showers Available",
        None, "mdi:water-boiler", SensorStateClass.MEASUREMENT,
    ),
    SensorDef("hw_last_shower", "Last Shower", None, "mdi:shower"),
    SensorDef("hw_plan", "Hot Water Plan", None, "mdi:calendar-clock"),
)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


class HotWaterPlannerSensor(
    CoordinatorEntity[SmartHeatControlCoordinator], SensorEntity
):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SmartHeatControlCoordinator,
        entry: ConfigEntry,
        defn: SensorDef,
    ) -> None:
        super().__init__(coordinator)
        self._defn = defn
        self._attr_unique_id = f"{entry.entry_id}_{defn.key}"
        self._attr_name = defn.name
        self._attr_icon = defn.icon
        self._attr_state_class = defn.state_class
        if defn.key == "hw_last_shower":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        if defn.state_class is not None:
            self._attr_suggested_display_precision = 1

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.entry.entry_id)},
            name="Smart Heat Control",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _plan(self) -> HwPlan | None:
        return self.coordinator._hw_plan

    @property
    def native_value(self) -> Any:
        key = self._defn.key
        if key == "hw_last_shower":
            last = self.coordinator._shower_tracker.last_shower()
            return last.start if last is not None else None
        plan = self._plan
        if plan is None:
            return None
        if key == "hw_showers_today":
            return round(plan.showers_taken, 1)
        if key == "hw_showers_available":
            return round(plan.showers_available, 1)
        if key == "hw_plan":
            return plan.state
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        key = self._defn.key
        tracker = self.coordinator._shower_tracker
        if key == "hw_last_shower":
            last = tracker.last_shower()
            if last is None:
                return None
            return {
                "minutes": round((last.end - last.start).total_seconds() / 60),
                "from_c": last.from_c,
                "to_c": last.to_c,
                "showers": last.showers,
            }
        plan = self._plan
        if plan is None:
            return None
        if key == "hw_showers_today":
            return {
                "showers_per_day": plan.showers_per_day,
                "counted_since": _iso(plan.day_start),
                "tappings": [
                    {
                        "start": _iso(d.start),
                        "minutes": round((d.end - d.start).total_seconds() / 60),
                        "from_c": d.from_c,
                        "to_c": d.to_c,
                        "showers": d.showers,
                    }
                    for d in tracker.draws_since(plan.day_start)
                ],
            }
        if key == "hw_showers_available":
            return {
                "showers_per_tank": plan.showers_per_tank,
                "used_since_full": round(tracker.used_since_full, 1),
                "tank_last_heated_full": _iso(tracker.last_full_at),
                "showers_needed_now": round(plan.showers_needed_now, 1),
            }
        if key == "hw_plan":
            return {
                "planner_active": plan.active,
                "reason": plan.reason,
                "hot_water_mode": str(plan.mode) if plan.active else None,
                "target_temp": plan.target_temp if plan.active else None,
                "fill_needed": plan.fill_needed,
                "ready_by": _iso(plan.ready_by),
                "ready_hour": plan.ready_hour,
                "planned_start": _iso(plan.planned_start),
                "planned_end": _iso(plan.planned_end),
                "latest_start": _iso(plan.latest_start),
                "estimated_minutes": plan.estimated_minutes,
                "price_basis": plan.price_basis,
                "compressor_strained": plan.strained,
                "deferred_since": _iso(plan.deferred_since),
                "blocked_until": _iso(plan.blocked_until),
                "producing_since": _iso(plan.producing_since),
                "last_production_result": plan.last_result,
                # Shower equivalents per local hour of day, last 60 days.
                "hourly_profile": [
                    round(v, 1)
                    for v in tracker.hourly_profile(plan.day_start.tzinfo)
                ],
            }
        return None
