"""Binary sensor entities for Smart Heat Control."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SmartHeatControlCoordinator
from .models import FullDecision


@dataclass(frozen=True)
class BinarySensorDef:
    key: str
    name: str
    icon_on: str
    icon_off: str


_BINARY_SENSORS: tuple[BinarySensorDef, ...] = (
    BinarySensorDef(
        "hw_reduction_active",
        "HW Reduction Active",
        "mdi:water-boiler-alert",
        "mdi:water-boiler",
    ),
    BinarySensorDef(
        "is_evening_expensive",
        "Expensive Evening Forecast",
        "mdi:lightning-bolt",
        "mdi:lightning-bolt-outline",
    ),
    BinarySensorDef(
        "wait_for_sun",
        "Waiting for Solar",
        "mdi:solar-power",
        "mdi:solar-power-variant-outline",
    ),
    BinarySensorDef(
        # Renamed 2026-06-03 from "is_summer_coast" → "is_summer_mode" to
        # match the user-facing label. unique_id changes with the key, so
        # HA will register this as a *new* entity (history before the
        # rename is orphaned in the registry — clean up via Settings →
        # Devices → Entities → search "summer coast" → delete).
        "is_summer_mode",
        "Summer Mode Active",
        "mdi:weather-sunny",
        "mdi:weather-sunny-off",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartHeatControlCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        SmartHeatControlBinarySensor(coordinator, entry, defn)
        for defn in _BINARY_SENSORS
    )


class SmartHeatControlBinarySensor(
    CoordinatorEntity[SmartHeatControlCoordinator], BinarySensorEntity
):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: SmartHeatControlCoordinator,
        entry: ConfigEntry,
        defn: BinarySensorDef,
    ) -> None:
        super().__init__(coordinator)
        self._defn = defn
        self._attr_unique_id = f"{entry.entry_id}_{defn.key}"
        self._attr_name = defn.name

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.entry.entry_id)},
            name="Smart Heat Control",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def is_on(self) -> bool | None:
        key = self._defn.key

        if key == "hw_reduction_active":
            return self.coordinator._hw_reduction_active

        computed = getattr(self.coordinator, "_last_computed", None)
        if computed is None:
            return None

        if key == "is_evening_expensive":
            return computed.is_evening_expensive
        if key == "wait_for_sun":
            return computed.wait_for_sun
        if key == "is_summer_mode":
            return computed.is_summer_mode
        return None

    @property
    def icon(self) -> str:
        return self._defn.icon_on if self.is_on else self._defn.icon_off
