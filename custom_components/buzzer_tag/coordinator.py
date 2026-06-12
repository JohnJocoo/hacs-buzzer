"""Held BLE connection manager for a single Buzzer Tag device.

The firmware is designed to be HELD CONNECTED over a low-power link: once
connected the tag stays connected and requests a slow connection interval, so a
held link is cheap and commands land within ~2 s. We therefore keep one
connection open per device and:

  * subscribe to the status characteristic (buzz state + battery %) so entities
    update by push (local_push);
  * reconnect gracefully when the link drops. After a drop the tag advertises
    for ~10 s, then enters a recovery cycle (advertise ~10 s, sleep ~5 min), so
    we reconnect the moment we *see* it advertise rather than busy-polling. This
    keeps us off the radio while it sleeps and respects its energy budget.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .const import (
    BUZZ_CHAR_UUID,
    BUZZ_OFF,
    BUZZ_ON,
    BUZZ_STATUS,
    DEVICE_NAME,
    STATUS_CHAR_UUID,
)

_LOGGER = logging.getLogger(__name__)


class BuzzerTagConnection:
    """Owns the BLE connection and current state for one Buzzer Tag."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, address: str
    ) -> None:
        """Initialise the connection manager."""
        self.hass = hass
        self.entry = entry
        self._address = address
        # Use the device name plus a short, stable suffix of the address so logs
        # and the device registry can tell multiple tags apart.
        self._name = f"{DEVICE_NAME} {short_address(address)}"

        self._client: BleakClientWithServiceCache | None = None
        self._connect_lock = asyncio.Lock()
        self._reconnect_task: asyncio.Task | None = None
        self._cancel_advertisement: CALLBACK_TYPE | None = None

        self._connected = False
        self._expected_disconnect = False
        self._stopped = False

        self._buzz_state: bool | None = None
        self._battery: int | None = None

        self._listeners: set[CALLBACK_TYPE] = set()

    # --- Public properties ----------------------------------------------------

    @property
    def address(self) -> str:
        """Bluetooth address of the device."""
        return self._address

    @property
    def name(self) -> str:
        """Human-readable name including the address suffix."""
        return self._name

    @property
    def available(self) -> bool:
        """Whether the held connection is currently up."""
        return self._connected

    @property
    def buzz_state(self) -> bool | None:
        """True if the melody is playing, False if stopped, None if unknown."""
        return self._buzz_state

    @property
    def battery(self) -> int | None:
        """Coarse battery percentage (0-100), or None if not yet reported."""
        return self._battery

    # --- Lifecycle ------------------------------------------------------------

    async def async_start(self) -> None:
        """Register for advertisements and attempt the initial connection."""
        self._cancel_advertisement = bluetooth.async_register_callback(
            self.hass,
            self._on_advertisement,
            BluetoothCallbackMatcher(address=self._address, connectable=True),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        # Best effort: the tag may be asleep right now. If so, the advertisement
        # callback above will reconnect us as soon as it next advertises, so we
        # do NOT fail setup over a sleeping device.
        try:
            await self._async_connect()
        except BleakError as err:
            _LOGGER.debug(
                "Initial connect to %s failed; will reconnect when it next "
                "advertises: %s",
                self._address,
                err,
            )

    async def async_stop(self) -> None:
        """Tear down the connection and all callbacks."""
        self._stopped = True
        self._expected_disconnect = True
        if self._cancel_advertisement is not None:
            self._cancel_advertisement()
            self._cancel_advertisement = None
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        client = self._client
        self._client = None
        if client is not None and client.is_connected:
            try:
                await client.stop_notify(STATUS_CHAR_UUID)
            except BleakError:
                pass
            await client.disconnect()
        self._connected = False

    # --- Commands -------------------------------------------------------------

    async def async_buzz_on(self) -> None:
        """Start the looping melody."""
        await self._async_write(BUZZ_ON)
        self._buzz_state = True
        self._async_update_listeners()

    async def async_buzz_off(self) -> None:
        """Stop the melody immediately."""
        await self._async_write(BUZZ_OFF)
        self._buzz_state = False
        self._async_update_listeners()

    async def async_request_status(self) -> None:
        """Ask the device to emit a status notification without changing state."""
        await self._async_write(BUZZ_STATUS)

    @callback
    def async_set_buzz_state(self, state: bool | None) -> None:
        """Set the cached buzz state (e.g. an optimistic timeout) and notify."""
        self._buzz_state = state
        self._async_update_listeners()

    async def _async_write(self, payload: bytes) -> None:
        """Write a one-byte command, reconnecting once on a transient drop."""
        last_err: Exception | None = None
        for attempt in range(2):
            if self._client is None or not self._client.is_connected:
                await self._async_connect()
            try:
                # response=True so a rejected/failed write surfaces clearly.
                await self._client.write_gatt_char(  # type: ignore[union-attr]
                    BUZZ_CHAR_UUID, payload, response=True
                )
                return
            except BleakError as err:
                last_err = err
                _LOGGER.debug(
                    "Write to %s failed (attempt %d): %s",
                    self._address,
                    attempt + 1,
                    err,
                )
                # Drop the stale client so the next attempt reconnects.
                self._connected = False
                self._client = None
        assert last_err is not None
        raise last_err

    # --- Connection management ------------------------------------------------

    async def _async_connect(self) -> None:
        """Establish (or re-use) the held connection and subscribe to status."""
        async with self._connect_lock:
            if self._client is not None and self._client.is_connected:
                return

            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self._address, connectable=True
            )
            if ble_device is None:
                raise BleakError(
                    f"{self._address} is not currently advertising "
                    "(the tag is asleep)"
                )

            self._expected_disconnect = False
            _LOGGER.debug("Connecting to %s", self._address)
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                self._name,
                disconnected_callback=self._on_disconnected,
                ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
                    self.hass, self._address, connectable=True
                ),
            )
            self._client = client

            # Subscribe to status push: payload is [buzz_state, battery_pct].
            try:
                await client.start_notify(
                    STATUS_CHAR_UUID, self._on_status_notify
                )
            except BleakError as err:
                _LOGGER.debug(
                    "Could not subscribe to status on %s: %s",
                    self._address,
                    err,
                )

            self._connected = True
            _LOGGER.debug("Connected to %s", self._address)
            self._async_update_listeners()

        # Ask for an immediate status report (outside the lock).
        try:
            await client.write_gatt_char(BUZZ_CHAR_UUID, BUZZ_STATUS, response=True)
        except BleakError:
            pass

    @callback
    def _on_advertisement(
        self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        """Reconnect when the tag advertises and we are not connected."""
        if self._stopped or self._connected:
            return
        self._async_schedule_reconnect()

    def _on_disconnected(self, client: BleakClientWithServiceCache) -> None:
        """bleak disconnect callback (may run off the event loop)."""
        self.hass.loop.call_soon_threadsafe(self._handle_disconnected)

    @callback
    def _handle_disconnected(self) -> None:
        """Mark the link down and try to reconnect."""
        self._connected = False
        # The melody plays independently of the link, so the true buzz state is
        # now unknown until we reconnect and read it.
        self._buzz_state = None
        self._async_update_listeners()
        if self._stopped or self._expected_disconnect:
            return
        _LOGGER.debug(
            "%s disconnected; will reconnect on its next advertising window",
            self._address,
        )
        self._async_schedule_reconnect()

    @callback
    def _async_schedule_reconnect(self) -> None:
        """Kick off a single background reconnect attempt."""
        if self._stopped:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = self.entry.async_create_background_task(
            self.hass, self._async_reconnect(), f"buzzer_tag reconnect {self._address}"
        )

    async def _async_reconnect(self) -> None:
        """Attempt to reconnect; advertisement callback retries on failure."""
        try:
            await self._async_connect()
        except BleakError as err:
            _LOGGER.debug("Reconnect to %s failed: %s", self._address, err)

    # --- Status push ----------------------------------------------------------

    @callback
    def _on_status_notify(
        self, _char: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle a status notification: [buzz_state, battery_pct]."""
        if len(data) >= 1:
            self._buzz_state = data[0] != 0
        if len(data) >= 2:
            self._battery = max(0, min(100, int(data[1])))
        self._async_update_listeners()

    # --- Listener plumbing ----------------------------------------------------

    @callback
    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        """Register an entity update callback; returns an unsubscribe function."""
        self._listeners.add(update_callback)

        @callback
        def _remove() -> None:
            self._listeners.discard(update_callback)

        return _remove

    @callback
    def _async_update_listeners(self) -> None:
        """Notify all entities that state changed."""
        for update_callback in list(self._listeners):
            update_callback()


def short_address(address: str) -> str:
    """Return a short, stable suffix of a BLE address to label a device.

    e.g. "AA:BB:CC:DD:EE:FF" -> "EEFF". Used so several tags are
    distinguishable in entity names and the device registry.
    """
    cleaned = address.replace(":", "").replace("-", "")
    return cleaned[-4:].upper() if len(cleaned) >= 4 else cleaned.upper()
