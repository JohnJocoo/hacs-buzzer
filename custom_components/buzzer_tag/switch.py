"""Switch platform for the Buzzer Tag: on = play looping melody, off = stop."""

from __future__ import annotations

import logging
from typing import Any

from bleak.exc import BleakError

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import BuzzerTagConfigEntry
from .const import MELODY_TIMEOUT_S
from .entity import BuzzerTagEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BuzzerTagConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the buzzer switch."""
    async_add_entities([BuzzerTagSwitch(entry.runtime_data)])


class BuzzerTagSwitch(BuzzerTagEntity, SwitchEntity):
    """Control the looping melody.

    The firmware stops the melody by itself after ~2 minutes, so when we turn
    it on we also arm an optimistic timer that flips the switch back off if we
    never receive a "playback ended" status notification.
    """

    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_name = None  # primary feature -> uses the device name
    _attr_icon = "mdi:bullhorn"

    def __init__(self, connection) -> None:
        """Initialise the switch."""
        super().__init__(connection)
        self._attr_unique_id = f"{connection.address}_buzz"
        self._cancel_timeout = None

    @property
    def is_on(self) -> bool | None:
        """Whether the melody is currently playing."""
        return self._connection.buzz_state

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the melody."""
        try:
            await self._connection.async_buzz_on()
        except BleakError as err:
            raise HomeAssistantError(f"Failed to start buzzer: {err}") from err
        self._arm_timeout()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the melody."""
        try:
            await self._connection.async_buzz_off()
        except BleakError as err:
            raise HomeAssistantError(f"Failed to stop buzzer: {err}") from err
        self._cancel_timeout_timer()
        self.async_write_ha_state()

    @callback
    def _arm_timeout(self) -> None:
        """Flip the switch off after the firmware's auto-stop window."""
        self._cancel_timeout_timer()

        @callback
        def _expire(_now) -> None:
            self._cancel_timeout = None
            # Only correct the optimistic state; real notifications win.
            if self._connection.buzz_state:
                self._connection.async_set_buzz_state(False)

        self._cancel_timeout = async_call_later(
            self.hass, MELODY_TIMEOUT_S, _expire
        )

    @callback
    def _cancel_timeout_timer(self) -> None:
        if self._cancel_timeout is not None:
            self._cancel_timeout()
            self._cancel_timeout = None

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the optimistic timer on removal."""
        self._cancel_timeout_timer()
