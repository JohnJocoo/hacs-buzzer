"""The Buzzer Tag integration.

A small BLE peripheral that plays a looping melody on command. There is no
pairing/bonding/encryption: we just connect and write a one-byte command to a
plain GATT characteristic. The device is meant to be HELD CONNECTED over a
low-power link, so this integration keeps a single connection open per device
and reconnects gracefully when the link drops.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN
from .coordinator import BuzzerTagConnection

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]

BuzzerTagConfigEntry = ConfigEntry[BuzzerTagConnection]


async def async_setup_entry(
    hass: HomeAssistant, entry: BuzzerTagConfigEntry
) -> bool:
    """Set up Buzzer Tag from a config entry."""
    address: str = entry.data[CONF_ADDRESS]

    connection = BuzzerTagConnection(hass, entry, address)
    try:
        await connection.async_start()
    except Exception as err:  # noqa: BLE001 - surface as a retryable setup error
        # The tag may simply be asleep right now. ConfigEntryNotReady makes HA
        # retry setup later; meanwhile the advertisement callback registered in
        # async_start() will also reconnect on its own once the tag is seen.
        raise ConfigEntryNotReady(
            f"Could not connect to Buzzer Tag {address}: {err}"
        ) from err

    entry.runtime_data = connection
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: BuzzerTagConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant, entry: BuzzerTagConfigEntry
) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
