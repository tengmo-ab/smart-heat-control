"""Diagnostic sensor entities exposing computed values."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SmartHeatControlCoordinator
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
