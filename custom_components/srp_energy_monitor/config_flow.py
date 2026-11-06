"""Config flow for SRP Energy Monitor.

Two entry points, one validation rule.

``async_step_user`` sets the integration up. ``async_step_reauth`` is what Home
Assistant opens when the coordinator raises ``ConfigEntryAuthFailed`` -- which,
after the 2026-09-02 outage, happens only for a password SRP itself called
``invalid_grant``. That makes this flow rare and important: by the time a user
sees it, polling has already stopped, and the reauth prompt is the only way back
without deleting and re-adding the integration.

It has to actually work, which until now it did not. ``async_step_reauth``
delegated to ``async_step_user``, whose success path ends in
``_abort_if_unique_id_configured()``. The account being reauthenticated is by
definition already configured, so the flow aborted with "That account is already
set up." and the user could never enter a new password. The steps below are the
supported shape for Home Assistant 2026.8, read out of
``homeassistant/config_entries.py`` at that tag rather than remembered.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

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

# The account number is deliberately absent: it is the entry's unique id, so
# changing it here would mean reauthenticating as somebody else. Both remaining
# fields are asked for because SRP checks them together and its rejection --
# "The user name or password is incorrect" -- does not say which half was wrong.
REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class SRPEnergyMonitorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Collect credentials and prove they work before creating the entry."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Let Home Assistant bind the entry after constructing the handler."""
        return SRPEnergyMonitorOptionsFlow()

    async def _validate(self, account: str, username: str, password: str) -> str | None:
        """Round-trip one set of credentials. Returns an error key, or None on success.

        Shared by setup and reauth so the two cannot drift apart in how they
        read a failure. That matters more than the saved lines: the reauth
        prompt exists to answer a verdict the coordinator reached, and a flow
        that disagreed with the coordinator about what counts as a bad password
        would either refuse a correct one or accept a wrong one.
        """
        client = SRPClient(
            # async_create_clientsession, not async_get_clientsession -- the same
            # rule the coordinator follows, for the same reason. The shared
            # session means a shared cookie jar, and SRP's entire notion of being
            # logged in is one cookie in it. Validating credentials here used to
            # drop that cookie into everyone's jar, so a config flow could
            # quietly overwrite the session a running entry depended on.
            #
            # This session is not attached to a config entry: in a config flow
            # the `config_entries.current_entry` ContextVar that
            # _async_register_clientsession_shutdown reads is unset, so the only
            # cleanup registered is a detach on EVENT_HOMEASSISTANT_CLOSE. The
            # connector underneath is Home Assistant's shared one, so what
            # outlives the flow is a session object and one once-listener per
            # submitted form -- cheap, and bounded by how many times a human is
            # willing to retype a password.
            async_create_clientsession(self.hass),
            username,
            password,
            account,
        )
        try:
            await client.validate()
        except SRPAuthError:
            return "invalid_auth"
        except SRPAccessBlocked:
            # Worth its own message: the credentials may be perfect and sending
            # someone to retype their password wastes their time. This usually
            # clears by itself.
            return "access_blocked"
        except SRPApiError:
            return "cannot_connect"
        except Exception:
            LOGGER.exception("unexpected error validating SRP credentials")
            return "unknown"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            account = user_input[CONF_ACCOUNT_ID].replace("-", "").strip()
            if not ACCOUNT_ID.match(account):
                errors[CONF_ACCOUNT_ID] = "invalid_account"
            else:
                user_input[CONF_ACCOUNT_ID] = account
                error = await self._validate(
                    account, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
                if error:
                    errors["base"] = error
                else:
                    await self.async_set_unique_id(account)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title=f"Account {account}", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Credentials stopped working; hand straight to the confirm step.

        ``entry_data`` is the failing entry's stored data, passed by Home
        Assistant. Nothing is kept from it: the entry itself is reachable
        through :meth:`_get_reauth_entry`, which reads the flow context rather
        than trusting a snapshot taken at the moment the flow started.
        """
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a new username and password for an entry that already exists.

        The account number is not asked for and not changed. On success this
        updates the existing entry in place and schedules a reload -- which is
        the whole point, since the coordinator that stopped polling is the one
        that has to be restarted, not a new one alongside it.
        """
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        account = entry.data[CONF_ACCOUNT_ID]

        if user_input is not None:
            error = await self._validate(
                account, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            else:
                # data_updates rather than data: it merges over the stored
                # entry, so the account number survives without being restated
                # here and cannot be dropped by an edit to the schema above.
                # The abort reason defaults to "reauth_successful".
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                REAUTH_SCHEMA,
                user_input or {CONF_USERNAME: entry.data[CONF_USERNAME]},
            ),
            description_placeholders={"account": account},
            errors=errors,
        )


class SRPEnergyMonitorOptionsFlow(OptionsFlow):
    """Choose the audit's declared plan without changing account credentials."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Preserve unrelated options; framework saves the result and reloads."""
        current = self.config_entry.options
        selected = current.get("demand_audit_plan", "disabled")
        if selected not in ("disabled", "E15"):
            selected = "disabled"
        errors = {}
        if user_input is not None:
            selected = user_input.get("demand_audit_plan")
            if selected in ("disabled", "E15"):
                return self.async_create_entry(
                    title="", data={**current, "demand_audit_plan": selected}
                )
            errors["demand_audit_plan"] = "invalid_plan"
            selected = "disabled"
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required("demand_audit_plan", default=selected): vol.In({
                    "disabled": "Disabled", "E15": "E-15 Average Demand",
                }),
            }),
            errors=errors,
        )
