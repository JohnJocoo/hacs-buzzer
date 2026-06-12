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
from datetime import timedelta

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
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    BUZZ_CHAR_UUID,
    BUZZ_OFF,
    BUZZ_ON,
    BUZZ_STATUS,
    DEVICE_NAME,
    HEALTH_CHECK_INTERVAL_S,
    RECONNECT_BACKOFF_S,
    STATUS_CHAR_UUID,
    STATUS_POLL_INTERVAL_H,
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
        self._reconnect_wake = asyncio.Event()
        self._cancel_advertisement: CALLBACK_TYPE | None = None
        self._cancel_health_check: CALLBACK_TYPE | None = None
        self._cancel_status_poll: CALLBACK_TYPE | None = None

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
        # Backstop: independently of advertisement callbacks, periodically verify
        # the held link is alive and kick a reconnect if it is not.
        self._cancel_health_check = async_track_time_interval(
            self.hass,
            self._async_health_check,
            timedelta(seconds=HEALTH_CHECK_INTERVAL_S),
        )
        # Refresh the battery reading on an idle device by polling status daily.
        self._cancel_status_poll = async_track_time_interval(
            self.hass,
            self._async_poll_status,
            timedelta(hours=STATUS_POLL_INTERVAL_H),
        )
        # Best effort: the tag may be asleep right now. If so, the reconnect loop
        # (and the advertisement callback) will connect us as soon as it next
        # advertises, so we do NOT fail setup over a sleeping device.
        try:
            await self._async_connect()
        except Exception as err:  # noqa: BLE001 - any failure -> keep retrying
            _LOGGER.debug(
                "Initial connect to %s failed; will keep retrying: %s",
                self._address,
                err,
            )
            self._async_schedule_reconnect()

    async def async_stop(self) -> None:
        """Tear down the connection and all callbacks."""
        self._stopped = True
        self._expected_disconnect = True
        if self._cancel_advertisement is not None:
            self._cancel_advertisement()
            self._cancel_advertisement = None
        if self._cancel_health_check is not None:
            self._cancel_health_check()
            self._cancel_health_check = None
        if self._cancel_status_poll is not None:
            self._cancel_status_poll()
            self._cancel_status_poll = None
        # Wake the reconnect loop so it observes _stopped and exits promptly.
        self._reconnect_wake.set()
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
        """React to a connectable advertisement from our device.

        A connectable advertisement means the tag is NOT in a connection right
        now. So if we still believe we are connected, our held link is stale
        (e.g. the device rebooted after a battery swap before the OS noticed the
        drop) - treat it as disconnected and reconnect.
        """
        if self._stopped:
            return
        link_alive = self._client is not None and self._client.is_connected
        if self._connected and link_alive:
            # We genuinely hold the link; a stray advert needs no action.
            return
        if self._connected and not link_alive:
            self._note_disconnected()
        # Wake the reconnect loop (or start it) so we grab this advert window.
        self._async_schedule_reconnect()
        self._reconnect_wake.set()

    @callback
    def _async_health_check(self, _now) -> None:
        """Periodic backstop: detect a stale link even with no disconnect event."""
        if self._stopped:
            return
        link_alive = self._client is not None and self._client.is_connected
        if self._connected and not link_alive:
            _LOGGER.debug("%s link found dead during health check", self._address)
            self._note_disconnected()
        if not self._connected:
            self._async_schedule_reconnect()

    async def _async_poll_status(self, _now) -> None:
        """Daily: ask the device for a fresh status so battery % stays current.

        Only meaningful over the held link; if we are disconnected we skip it
        (waking a sleeping device just to read battery is not worth the energy,
        and a reconnect refreshes status on its own).
        """
        if self._stopped or not self._connected:
            return
        try:
            await self.async_request_status()
        except Exception as err:  # noqa: BLE001 - transient, just wait for next poll
            _LOGGER.debug("Status poll for %s failed: %s", self._address, err)

    def _on_disconnected(self, client: BleakClientWithServiceCache) -> None:
        """bleak disconnect callback (may run off the event loop)."""
        self.hass.loop.call_soon_threadsafe(self._handle_disconnected)

    @callback
    def _handle_disconnected(self) -> None:
        """Mark the link down and start trying to reconnect."""
        self._note_disconnected()
        if self._stopped or self._expected_disconnect:
            return
        _LOGGER.debug("%s disconnected; starting reconnect", self._address)
        self._async_schedule_reconnect()

    @callback
    def _note_disconnected(self) -> None:
        """Flip internal state to disconnected and notify entities."""
        if not self._connected and self._client is None:
            return
        self._connected = False
        self._client = None
        # The melody plays independently of the link, so the true buzz state is
        # now unknown until we reconnect and read it.
        self._buzz_state = None
        self._async_update_listeners()

    @callback
    def _async_schedule_reconnect(self) -> None:
        """Ensure the reconnect loop is running."""
        if self._stopped or self._connected:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = self.entry.async_create_background_task(
            self.hass,
            self._async_reconnect_loop(),
            f"buzzer_tag reconnect {self._address}",
        )

    async def _async_reconnect_loop(self) -> None:
        """Retry connecting until we succeed or are stopped.

        Runs on a backoff timer but is also woken immediately whenever the tag
        advertises, so we connect within its short (~10 s) advertising windows
        without busy-polling the radio while it sleeps.
        """
        attempt = 0
        while not self._stopped and not self._connected:
            attempt += 1
            try:
                await self._async_connect()
            except Exception as err:  # noqa: BLE001 - transient; keep retrying
                _LOGGER.debug(
                    "Reconnect attempt %d to %s failed: %s",
                    attempt,
                    self._address,
                    err,
                )
            if self._connected or self._stopped:
                break
            self._reconnect_wake.clear()
            try:
                await asyncio.wait_for(
                    self._reconnect_wake.wait(), timeout=RECONNECT_BACKOFF_S
                )
            except (asyncio.TimeoutError, TimeoutError):
                pass
        if self._connected:
            _LOGGER.debug("Reconnected to %s after %d attempt(s)", self._address, attempt)

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
