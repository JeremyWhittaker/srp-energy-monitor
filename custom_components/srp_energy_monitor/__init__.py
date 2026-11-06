"""The SRP Energy Monitor integration.

Reads a Salt River Project account through its private My Account JSON API --
no browser, no scraping, nothing beyond what Home Assistant already ships.

Companion to https://github.com/JeremyWhittaker/srp-energy-monitor, which also
offers a Playwright backend for when Cloudflare declines to serve this one.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import SRPConfigEntry, SRPCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: SRPConfigEntry) -> bool:
    """Set up SRP Energy Monitor from a config entry."""
    coordinator = SRPCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SRPConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(hass: HomeAssistant, entry: SRPConfigEntry) -> None:
    """Reload through the supported manager so the coordinator reads new options."""
    await hass.config_entries.async_reload(entry.entry_id)
