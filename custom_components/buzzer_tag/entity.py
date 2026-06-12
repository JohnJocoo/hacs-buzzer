"""Base entity for the Buzzer Tag integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DEVICE_NAME, DOMAIN
from .coordinator import BuzzerTagConnection, short_address


class BuzzerTagEntity(Entity):
    """Common base: device info, availability and push updates."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, connection: BuzzerTagConnection) -> None:
        """Initialise the entity from its connection manager."""
        self._connection = connection
        address = connection.address
        # Subclasses set their own unique_id as f"{address}_{key}".
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            name=f"{DEVICE_NAME} {short_address(address)}",
            manufacturer="Buzzer Tag",
            model="Buzzer Tag",
        )

    @property
    def available(self) -> bool:
        """Entity is available only while the held link is up."""
        return self._connection.available

    async def async_added_to_hass(self) -> None:
        """Subscribe to push updates from the connection manager."""
        self.async_on_remove(
            self._connection.async_add_listener(self.async_write_ha_state)
        )
