"""The Smart Heat Control integration.

Debug notes
-----------
This module sets up the DataUpdateCoordinator and forwards entry setup to each
platform. Every failure path logs a clear, actionable message under the logger
`custom_components.smart_heat_control` — increase verbosity at runtime with::

    service: logger.set_level
    data:
      custom_components.smart_heat_control: debug
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN
from .coordinator import SmartHeatControlCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.DATETIME,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Smart Heat Control from a config entry.

    Order matters here:
      1. Build the coordinator (no I/O yet).
      2. Set up platform entities so RestoreEntity can push restored state
         into coordinator attributes BEFORE the first cascade runs.
      3. Run the first refresh, which evaluates the cascade with restored
         state already loaded — preventing a "blip" of default values right
         after HA restart.
    """
    _LOGGER.debug(
        "Smart Heat Control: async_setup_entry start (entry_id=%s, title=%r)",
        entry.entry_id,
        entry.title,
    )

    try:
        coordinator = SmartHeatControlCoordinator(hass, entry)
    except Exception as ex:
        _LOGGER.exception(
            "Smart Heat Control: failed to construct coordinator — "
            "check that all required entities in the config entry exist "
            "(climate_entity, outdoor_temp_sensor). Entry data keys=%s",
            sorted(entry.data.keys()),
        )
        raise ConfigEntryNotReady(f"coordinator init failed: {ex}") from ex

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception as ex:
        _LOGGER.exception(
            "Smart Heat Control: forwarding platform setup raised. "
            "This usually means one platform file (switch.py / number.py / etc.) "
            "fails to import or instantiate. The traceback above identifies which."
        )
        raise ConfigEntryNotReady(f"platform setup failed: {ex}") from ex

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as ex:
        _LOGGER.exception(
            "Smart Heat Control: first cascade refresh failed. "
            "Common causes: an entity you pointed at doesn't exist yet, "
            "or one returns a value the controller can't parse. "
            "The integration will keep retrying on its normal interval."
        )
        # Do NOT raise — the coordinator handles transient unavailability and
        # the user can fix the upstream entity without re-adding the integration.
        # We still want the platforms loaded so users see the switches/numbers.

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _LOGGER.info(
        "Smart Heat Control: setup complete for entry %s (%d roles configured)",
        entry.entry_id,
        len(entry.data),
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down the integration."""
    _LOGGER.debug("Smart Heat Control: unloading entry %s", entry.entry_id)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        _LOGGER.warning(
            "Smart Heat Control: unload_platforms reported failure for %s",
            entry.entry_id,
        )
        return False

    coordinator: SmartHeatControlCoordinator | None = (
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    )
    if coordinator is not None:
        try:
            await coordinator.async_shutdown()
        except Exception:
            _LOGGER.exception(
                "Smart Heat Control: coordinator shutdown raised for entry %s",
                entry.entry_id,
            )
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry after options change."""
    _LOGGER.debug("Smart Heat Control: reloading entry %s after options update", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)
