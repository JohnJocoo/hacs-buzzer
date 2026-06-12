"""Binary sensor platform for the Buzzer Tag: buzzing / idle state."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BuzzerTagConfigEntry
from .entity import BuzzerTagEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BuzzerTagConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the buzz-state binary sensor."""
    async_add_entities([BuzzerTagBuzzingSensor(entry.runtime_data)])


class BuzzerTagBuzzingSensor(BuzzerTagEntity, BinarySensorEntity):
    """Reports the device's reported playback state from status notifications."""

    _attr_device_class = BinarySensorDeviceClass.SOUND
    _attr_translation_key = "buzzing"
    _attr_icon = "mdi:music-note"

    def __init__(self, connection) -> None:
        """Initialise the buzzing binary sensor."""
        super().__init__(connection)
        self._attr_unique_id = f"{connection.address}_buzzing"

    @property
    def is_on(self) -> bool | None:
        """True when the melody is playing per the device's status report."""
        return self._connection.buzz_state
