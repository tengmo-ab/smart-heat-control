"""Integration-owned number entities for Smart Heat Control."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEFAULT_HEAT_CURVE,
    CONF_DEFAULT_HW_TEMP,
    CONF_DEFAULT_INDOOR_TEMP,
    CONF_LEGIONELLA_DURATION_HOURS,
    CONF_LEGIONELLA_MAX_DAYS,
    CONF_LEGIONELLA_MIN_DAYS,
    CONF_PRICE_THRESHOLD,
    DEFAULT_HEAT_CURVE,
    DEFAULT_HW_TEMP,
    DEFAULT_INDOOR_TEMP,
    DEFAULT_LEGIONELLA_DURATION_HOURS,
    DEFAULT_LEGIONELLA_MAX_DAYS,
    DEFAULT_LEGIONELLA_MIN_DAYS,
    DEFAULT_PRICE_THRESHOLD,
    DOMAIN,
    MAX_CLIMATE_TEMP,
    MAX_HEAT_CURVE,
    MAX_HW_TEMP,
    MIN_CLIMATE_TEMP,
    MIN_HEAT_CURVE,
    MIN_HW_TEMP,
)
from .coordinator import SmartHeatControlCoordinator


@dataclass(frozen=True)
class NumberDef:
    key: str
    conf_key: str
    name: str
    default: float
    min_val: float
    max_val: float
    step: float
    unit: str | None
    icon: str


_NUMBERS: tuple[NumberDef, ...] = (
    NumberDef(
        "default_indoor_temp", CONF_DEFAULT_INDOOR_TEMP,
        "Default Indoor Temperature", DEFAULT_INDOOR_TEMP,
        MIN_CLIMATE_TEMP, MAX_CLIMATE_TEMP, 0.5, "°C", "mdi:thermometer",
    ),
    NumberDef(
        "default_heat_curve", CONF_DEFAULT_HEAT_CURVE,
        "Default Heat Curve", DEFAULT_HEAT_CURVE,
        MIN_HEAT_CURVE, MAX_HEAT_CURVE, 0.5, None, "mdi:chart-bell-curve",
    ),
    NumberDef(
        "default_hw_temp", CONF_DEFAULT_HW_TEMP,
        "Default Hot Water Temperature", DEFAULT_HW_TEMP,
        MIN_HW_TEMP, MAX_HW_TEMP, 1.0, "°C", "mdi:water-thermometer",
    ),
    NumberDef(
        "price_threshold", CONF_PRICE_THRESHOLD,
        "Price Threshold", DEFAULT_PRICE_THRESHOLD,
        0.0, 1000.0, 1.0, "öre/kWh", "mdi:cash",
    ),
    NumberDef(
        "legionella_min_days", CONF_LEGIONELLA_MIN_DAYS,
        "Legionella Min Interval", DEFAULT_LEGIONELLA_MIN_DAYS,
        1.0, 30.0, 1.0, "d", "mdi:bacteria-outline",
    ),
    NumberDef(
        "legionella_max_days", CONF_LEGIONELLA_MAX_DAYS,
        "Legionella Max Interval", DEFAULT_LEGIONELLA_MAX_DAYS,
        1.0, 30.0, 1.0, "d", "mdi:bacteria",
    ),
    NumberDef(
        "legionella_duration_hours", CONF_LEGIONELLA_DURATION_HOURS,
        "Legionella Boost Duration", DEFAULT_LEGIONELLA_DURATION_HOURS,
        1.0, 12.0, 1.0, "h", "mdi:timer",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartHeatControlCoordinator = hass.data[DOMAIN][entry.entry_id]
    cfg = {**entry.data, **entry.options}
    async_add_entities(
        SmartHeatControlNumber(coordinator, entry, defn, cfg)
        for defn in _NUMBERS
    )


class SmartHeatControlNumber(
    CoordinatorEntity[SmartHeatControlCoordinator], NumberEntity, RestoreEntity
):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: SmartHeatControlCoordinator,
        entry: ConfigEntry,
        defn: NumberDef,
        cfg: dict,
    ) -> None:
        super().__init__(coordinator)
        self._defn = defn
        self._attr_unique_id = f"{entry.entry_id}_{defn.key}"
        self._attr_name = defn.name
        self._attr_native_min_value = defn.min_val
        self._attr_native_max_value = defn.max_val
        self._attr_native_step = defn.step
        self._attr_native_unit_of_measurement = defn.unit
        self._attr_icon = defn.icon
        # Initial value from config flow (overridden by restore on restart)
        self._value: float = float(cfg.get(defn.conf_key, defn.default))

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
            try:
                self._value = float(last.state)
            except (ValueError, TypeError):
                pass
        setattr(self.coordinator, self._defn.key, self._cast(self._value))

    @property
    def native_value(self) -> float:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        self._value = value
        setattr(self.coordinator, self._defn.key, self._cast(value))
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    def _cast(self, value: float) -> float | int:
        # legionella_*_days and duration are used as int in the cascade
        if self._defn.step == 1.0 and self._defn.unit in ("d", "h"):
            return int(value)
        return value
