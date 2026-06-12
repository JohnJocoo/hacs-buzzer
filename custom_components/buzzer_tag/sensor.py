"""Sensor platform for the Buzzer Tag: battery percentage."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BuzzerTagConfigEntry
from .entity import BuzzerTagEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BuzzerTagConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the battery sensor."""
    async_add_entities([BuzzerTagBatterySensor(entry.runtime_data)])


class BuzzerTagBatterySensor(BuzzerTagEntity, SensorEntity):
    """Coarse CR2032 battery percentage reported on the status characteristic."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, connection) -> None:
        """Initialise the battery sensor."""
        super().__init__(connection)
        self._attr_unique_id = f"{connection.address}_battery"

    @property
    def native_value(self) -> int | None:
        """Battery percentage (0-100)."""
        return self._connection.battery
