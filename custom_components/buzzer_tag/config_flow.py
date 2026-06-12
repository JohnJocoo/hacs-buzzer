"""Config flow for the Buzzer Tag integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import DEVICE_NAME, DOMAIN, SERVICE_UUID
from .coordinator import short_address


def _is_buzzer_tag(info: BluetoothServiceInfoBleak) -> bool:
    """Match an advertisement against the Buzzer Tag name or service UUID."""
    if info.name == DEVICE_NAME:
        return True
    return SERVICE_UUID.lower() in {u.lower() for u in info.service_uuids}


def _title(address: str) -> str:
    """Title that distinguishes multiple tags by an address suffix."""
    return f"{DEVICE_NAME} {short_address(address)}"


class BuzzerTagConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Buzzer Tag."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise discovery bookkeeping."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        # address -> service info, for the manual/user picker.
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a device discovered via the Bluetooth integration."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {"name": _title(discovery_info.address)}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a single discovered device."""
        assert self._discovery_info is not None
        address = self._discovery_info.address
        if user_input is not None:
            return self.async_create_entry(title=_title(address), data={CONF_ADDRESS: address})

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": _title(address)},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick from currently discovered Buzzer Tags."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=_title(address), data={CONF_ADDRESS: address})

        current_addresses = self._async_current_ids()
        for info in async_discovered_service_info(self.hass, connectable=True):
            if (
                info.address in current_addresses
                or info.address in self._discovered
                or not _is_buzzer_tag(info)
            ):
                continue
            self._discovered[info.address] = info

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        titles = {
            address: f"{_title(address)} ({address})"
            for address in self._discovered
        }
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(titles)}),
        )
