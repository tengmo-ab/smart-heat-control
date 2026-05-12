"""Read-only select entities that expose the current optimization modes."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, HW_MODES, OPTIMIZATION_MODES
from .coordinator import SmartHeatControlCoordinator
from .models import FullDecision, HwMode, Mode


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartHeatControlCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        OptimizationModeSelect(coordinator, entry),
        HwModeSelect(coordinator, entry),
    ])


class _BaseModeSelect(CoordinatorEntity[SmartHeatControlCoordinator], SelectEntity):
    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.entry.entry_id)},
            name="Smart Heat Control",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_select_option(self, option: str) -> None:
        # These selects are read-only outputs — the cascade decides the mode.
        pass


class OptimizationModeSelect(_BaseModeSelect):
    _attr_options = list(OPTIMIZATION_MODES)
    _attr_icon = "mdi:thermostat-auto"

    def __init__(
        self,
        coordinator: SmartHeatControlCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_optimization_mode"
        self._attr_name = "Optimization Mode"

    @property
    def current_option(self) -> str:
        decision: FullDecision | None = self.coordinator.data
        if decision is None:
            return Mode.DEFAULT
        return str(decision.climate.mode)


class HwModeSelect(_BaseModeSelect):
    _attr_options = list(HW_MODES)
    _attr_icon = "mdi:water-boiler"

    def __init__(
        self,
        coordinator: SmartHeatControlCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_hw_mode"
        self._attr_name = "Hot Water Mode"

    @property
    def current_option(self) -> str:
        decision: FullDecision | None = self.coordinator.data
        if decision is None:
            return HwMode.DEFAULT
        return str(decision.hot_water.mode)
