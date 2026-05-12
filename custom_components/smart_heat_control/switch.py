"""Integration-owned switch entities for Smart Heat Control."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SmartHeatControlCoordinator


@dataclass(frozen=True)
class SwitchDef:
    key: str
    name: str
    default: bool
    icon: str


_SWITCHES: tuple[SwitchDef, ...] = (
    SwitchDef("master_enabled", "Optimization Master", False, "mdi:thermostat-auto"),
    SwitchDef("cheap_price_enabled", "Cheap Price Boost", True, "mdi:currency-usd-off"),
    SwitchDef("price_peak_enabled", "Price Peak Reduction", True, "mdi:trending-down"),
    SwitchDef(
        "weather_anticipation_enabled",
        "Weather Anticipation",
        True,
        "mdi:weather-sunny-off",
    ),
    SwitchDef("legionella_boost_enabled", "Legionella Boost", False, "mdi:bacteria"),
    SwitchDef("survive_solar_enabled", "Survive on Solar", False, "mdi:solar-power"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartHeatControlCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        SmartHeatControlSwitch(coordinator, entry, sw) for sw in _SWITCHES
    )


class SmartHeatControlSwitch(
    CoordinatorEntity[SmartHeatControlCoordinator], SwitchEntity, RestoreEntity
):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SmartHeatControlCoordinator,
        entry: ConfigEntry,
        defn: SwitchDef,
    ) -> None:
        super().__init__(coordinator)
        self._defn = defn
        self._attr_unique_id = f"{entry.entry_id}_{defn.key}"
        self._attr_name = defn.name
        self._attr_icon = defn.icon
        self._state: bool = defn.default

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.entry.entry_id)},
            name="Smart Heat Control",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._state = last.state == STATE_ON
        # Push initial value to coordinator
        setattr(self.coordinator, self._defn.key, self._state)

    @property
    def is_on(self) -> bool:
        return self._state

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._state = True
        setattr(self.coordinator, self._defn.key, True)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._state = False
        setattr(self.coordinator, self._defn.key, False)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
