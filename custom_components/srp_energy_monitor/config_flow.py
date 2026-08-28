"""Config flow for SRP Energy Monitor."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_ACCOUNT_ID, DOMAIN, LOGGER
from .srp_api import SRPAccessBlocked, SRPApiError, SRPAuthError, SRPClient

ACCOUNT_ID = re.compile(r"^\d{9}$")

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCOUNT_ID): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class SRPEnergyMonitorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Collect credentials and prove they work before creating the entry."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            account = user_input[CONF_ACCOUNT_ID].replace("-", "").strip()
            if not ACCOUNT_ID.match(account):
                errors[CONF_ACCOUNT_ID] = "invalid_account"
            else:
                user_input[CONF_ACCOUNT_ID] = account
                client = SRPClient(
                    async_get_clientsession(self.hass),
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    account,
                )
                try:
                    await client.validate()
                except SRPAuthError:
                    errors["base"] = "invalid_auth"
                except SRPAccessBlocked:
                    # Worth its own message: the credentials may be perfect and
                    # sending someone to retype their password wastes their
                    # time. This usually clears by itself.
                    errors["base"] = "access_blocked"
                except SRPApiError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    LOGGER.exception("unexpected error validating SRP credentials")
                    errors["base"] = "unknown"
                else:
                    await self.async_set_unique_id(account)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title=f"Account {account}", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Credentials stopped working; ask for them again."""
        return await self.async_step_user()
