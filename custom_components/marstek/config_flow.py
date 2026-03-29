"""Config flow for Marstek integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pymarstek import MarstekUDPClient
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_MAC
from homeassistant.helpers.device_registry import format_mac

try:
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
except ImportError:
    # Fallback for older Home Assistant versions (pre-2025.1)
    try:
        from homeassistant.components.dhcp import DhcpServiceInfo  # type: ignore[assignment,no-redef]
    except ImportError:
        # If DHCP service info is not available, create a minimal stub
        from dataclasses import dataclass

        @dataclass
        class DhcpServiceInfo:  # type: ignore[no-redef]
            """Fallback DHCP service info for older Home Assistant versions."""

            ip: str
            hostname: str
            macaddress: str

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class MarstekConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Marstek."""

    VERSION = 1
    domain = DOMAIN
    discovered_devices: list[dict[str, Any]]
    _discovered_ip: str | None = None

    def _normalized_mac(self, mac: str | None) -> str | None:
        """Return normalized MAC or None."""
        if not mac:
            return None
        try:
            return format_mac(mac)
        except Exception:  # noqa: BLE001
            return None

    def _entry_matches_mac(self, entry, discovered_mac: str) -> bool:
        """Check whether a config entry matches the discovered MAC.

        Compare against all known MAC-related fields because DHCP or integration
        discovery may report a different MAC than the one originally used for
        unique_id/config entry creation.
        """
        candidates = [
            entry.data.get("ble_mac"),
            entry.data.get("mac"),
            entry.data.get("wifi_mac"),
            entry.unique_id,
        ]

        normalized_candidates = {
            normalized
            for candidate in candidates
            if (normalized := self._normalized_mac(candidate)) is not None
        }

        return discovered_mac in normalized_candidates

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step - broadcast device discovery."""
        if user_input is not None:
            # User has selected a device from the discovered list
            device_index = int(user_input["device"])
            device = self.discovered_devices[device_index]

            # Check if device is already configured using host/mac
            self._async_abort_entries_match({CONF_HOST: device["ip"]})

            # Use BLE-MAC as unique_id for stability
            unique_id_mac = (
                device.get("ble_mac") or device.get("mac") or device.get("wifi_mac")
            )
            if unique_id_mac:
                self._async_abort_entries_match({CONF_MAC: unique_id_mac})
                await self.async_set_unique_id(format_mac(unique_id_mac))
                self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"Marstek {device['device_type']} ({device['ip']})",
                data={
                    CONF_HOST: device["ip"],
                    CONF_MAC: device["mac"],
                    "device_type": device["device_type"],
                    "version": device["version"],
                    "wifi_name": device["wifi_name"],
                    "wifi_mac": device["wifi_mac"],
                    "ble_mac": device["ble_mac"],
                    "model": device["model"],  # Compatibility field
                    "firmware": device["firmware"],  # Compatibility field
                },
            )

        # Start broadcast device discovery
        try:
            _LOGGER.info("Starting device discovery")
            udp_client = MarstekUDPClient()
            await udp_client.async_setup()

            # Execute broadcast discovery with retry mechanism
            devices = await self._discover_devices_with_retry(udp_client)
            await udp_client.async_cleanup()

            if not devices:
                return self.async_show_form(
                    step_id="user",
                    data_schema=vol.Schema({}),
                    errors={"base": "no_devices_found"},
                )

            # Store discovered devices for selection
            self.discovered_devices = devices
            _LOGGER.info("Discovered %d devices", len(devices))

            # Show device selection form with detailed device information
            device_options = {}
            for i, device in enumerate(devices):
                device_name = (
                    f"{device.get('device_type', 'Unknown')} "
                    f"v{device.get('version', 'Unknown')} "
                    f"({device.get('wifi_name', 'No WiFi')}) "
                    f"- {device.get('ip', 'Unknown')}"
                )
                device_options[str(i)] = device_name

            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {vol.Required("device"): vol.In(device_options)}
                ),
                description_placeholders={
                    "devices": "\n".join(
                        [f"- {name}" for name in device_options.values()]
                    )
                },
            )

        except (OSError, TimeoutError, ValueError) as err:
            _LOGGER.error("Device discovery failed: %s", err)
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({}),
                errors={"base": "discovery_failed"},
            )

    async def _discover_devices_with_retry(
        self, udp_client, max_retries=2, retry_delay=3000
    ):
        """Device discovery retry mechanism."""
        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    _LOGGER.info("Device discovery, attempt %d", attempt)
                    await asyncio.sleep(retry_delay / 1000)
                    udp_client.clear_discovery_cache()

                # First attempt uses cache, retries force refresh
                use_cache = attempt == 1
                devices = await udp_client.discover_devices(use_cache=use_cache)

                if devices:
                    if attempt > 1:
                        _LOGGER.info("Device discovery retry successful")
                    return devices
                _LOGGER.warning("Attempt %d found no devices", attempt)

            except (OSError, TimeoutError, ValueError) as error:
                _LOGGER.error("Device discovery failed, attempt %d: %s", attempt, error)

                if attempt == max_retries:
                    _LOGGER.error(
                        "Device discovery failed after %d retries: %s",
                        max_retries,
                        error,
                    )
                    # Try using cached data as fallback
                    if udp_client._discovery_cache:  # noqa: SLF001
                        _LOGGER.info("Using cached device data as fallback")
                        return udp_client._discovery_cache.copy()  # noqa: SLF001
                    raise

        return []

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> config_entries.ConfigFlowResult:
        """Handle DHCP discovery to update IP address when it changes."""
        mac = self._normalized_mac(discovery_info.macaddress)
        if not mac:
            _LOGGER.warning(
                "DHCP discovery ignored because MAC could not be normalized: %s",
                discovery_info.macaddress,
            )
            return self.async_abort(reason="invalid_discovery_info")

        _LOGGER.info(
            "DHCP discovery triggered: MAC=%s, IP=%s, Hostname=%s",
            mac,
            discovery_info.ip,
            discovery_info.hostname,
        )

        # Match against all known MACs of each existing entry
        for entry in self._async_current_entries(include_ignore=False):
            if self._entry_matches_mac(entry, mac):
                if entry.data.get(CONF_HOST) != discovery_info.ip:
                    _LOGGER.info(
                        "DHCP discovery: Device %s IP changed from %s to %s, updating config entry",
                        mac,
                        entry.data.get(CONF_HOST),
                        discovery_info.ip,
                    )
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={**entry.data, CONF_HOST: discovery_info.ip},
                    )
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(entry.entry_id)
                    )
                else:
                    _LOGGER.debug(
                        "DHCP discovery: Device %s IP unchanged (%s)",
                        mac,
                        discovery_info.ip,
                    )
                return self.async_abort(reason="already_configured")

        _LOGGER.debug(
            "DHCP discovery: No existing entry found for MAC %s, continuing to user flow",
            mac,
        )
        return await self.async_step_user()

    async def async_step_integration_discovery(
        self, discovery_info: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Handle discovery from Scanner (integration discovery)."""
        discovered_ip = discovery_info.get("ip")
        discovered_ble_mac = self._normalized_mac(discovery_info.get("ble_mac"))

        if not discovered_ble_mac:
            return self.async_abort(reason="invalid_discovery_info")

        await self.async_set_unique_id(discovered_ble_mac)
        self._discovered_ip = discovered_ip

        return await self._async_handle_discovery_with_unique_id()

    async def _async_handle_discovery_with_unique_id(
        self,
    ) -> config_entries.ConfigFlowResult:
        """Handle any discovery with a unique id."""
        if not self.unique_id:
            return await self.async_step_user()

        for entry in self._async_current_entries(include_ignore=False):
            unique_id_match = entry.unique_id == self.unique_id
            fallback_mac_match = self._entry_matches_mac(entry, self.unique_id)

            if not unique_id_match and not fallback_mac_match:
                continue

            reload = entry.state == ConfigEntryState.SETUP_RETRY
            update_kwargs: dict[str, Any] = {}

            # Backfill unique_id for older entries that were created without it
            if entry.unique_id != self.unique_id:
                _LOGGER.info(
                    "Discovery: Updating entry unique_id from %s to %s",
                    entry.unique_id,
                    self.unique_id,
                )
                update_kwargs["unique_id"] = self.unique_id

            if entry.data.get(CONF_HOST) != self._discovered_ip:
                _LOGGER.info(
                    "Discovery: Device %s IP changed from %s to %s, updating config entry",
                    self.unique_id,
                    entry.data.get(CONF_HOST),
                    self._discovered_ip,
                )
                update_kwargs["data"] = {
                    **entry.data,
                    CONF_HOST: self._discovered_ip,
                }
                reload = entry.state in (
                    ConfigEntryState.SETUP_RETRY,
                    ConfigEntryState.LOADED,
                )

            if update_kwargs:
                self.hass.config_entries.async_update_entry(entry, **update_kwargs)

            if reload:
                self.hass.config_entries.async_schedule_reload(entry.entry_id)

            return self.async_abort(reason="already_configured")

        return await self.async_step_user()