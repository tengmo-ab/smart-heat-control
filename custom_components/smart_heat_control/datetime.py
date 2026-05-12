"""Datetime entities for vacation end and legionella tracking."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import homeassistant.util.dt as dt_util
from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SmartHeatControlCoordinator


@dataclass(frozen=True)
class DatetimeDef:
    key: str
    name: str
    icon: str
    category: EntityCategory


_DATETIMES: tuple[DatetimeDef, ...] = (
    DatetimeDef("vacation_end", "Vacation End", "mdi:beach", EntityCategory.CONFIG),
    DatetimeDef(
        "last_legionella_run",
        "Last Legionella Run",
        "mdi:bacteria",
        EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartHeatControlCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        SmartHeatControlDatetime(coordinator, entry, defn) for defn in _DATETIMES
    )


class SmartHeatControlDatetime(
    CoordinatorEntity[SmartHeatControlCoordinator], DateTimeEntity, RestoreEntity
):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SmartHeatControlCoordinator,
        entry: ConfigEntry,
        defn: DatetimeDef,
    ) -> None:
        super().__init__(coordinator)
        self._defn = defn
        self._attr_unique_id = f"{entry.entry_id}_{defn.key}"
        self._attr_name = defn.name
        self._attr_icon = defn.icon
        self._attr_entity_category = defn.category
        self._value: datetime | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.entry.entry_id)},
            name="Smart Heat Control",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None and last.state not in (
            "unknown", "unavailable", ""
        ):
            try:
                dt = dt_util.parse_datetime(last.state)
                if dt is not None:
                    self._value = dt
            except (ValueError, TypeError):
                pass
        setattr(self.coordinator, self._defn.key, self._value)

    @property
    def native_value(self) -> datetime | None:
        # Always reflect coordinator state so legionella-boost writes show immediately
        return getattr(self.coordinator, self._defn.key, self._value)

    async def async_set_value(self, value: datetime) -> None:
        self._value = value
        setattr(self.coordinator, self._defn.key, value)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    def _handle_coordinator_update(self) -> None:
        """Sync coordinator state (e.g. last_legionella_run updated by cascade)."""
        coord_val = getattr(self.coordinator, self._defn.key, None)
        if coord_val != self._value:
            self._value = coord_val
        super()._handle_coordinator_update()
