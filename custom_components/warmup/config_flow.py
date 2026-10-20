"""Config flow for the Warmup integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, OptionsFlow, SOURCE_IMPORT
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_LOCATION,
    CONF_LOCATIONS,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .warmup4ie.warmup4ie import (
    Warmup4IE,
    WarmupAuthError,
    location_selected,
)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


def validate_account(username, password):
    """Log in and return the location names, or an error key."""
    account = Warmup4IE(username, password)
    if isinstance(account.last_error, WarmupAuthError) or account._warmup_access_token is None:
        if isinstance(account.last_error, WarmupAuthError):
            return None, "invalid_auth"
        return None, "cannot_connect"
    if not account.setup_finished:
        return None, "cannot_connect"
    if not account.get_all_devices():
        return None, "no_devices"
    names = []
    for device in account.get_all_devices().values():
        name = device.get_location_name()
        if name and name not in names:
            names.append(name)
    return names, None


class WarmupConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a Warmup account config flow."""

    VERSION = 1

    def __init__(self):
        """Start with no credentials."""
        self._username = None
        self._password = None
        self._location_names = []

    async def async_step_user(self, user_input=None):
        """Ask for the my.warmup.com login."""
        errors = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]
            locations, error = await self.hass.async_add_executor_job(
                validate_account, username, password
            )
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(username.lower())
                self._abort_if_unique_id_configured()
                self._username = username
                self._password = password
                self._location_names = locations
                if self.context.get("source") == SOURCE_IMPORT or len(locations) <= 1:
                    chosen = locations
                    imported = getattr(self, "_imported_location", None)
                    if imported:
                        chosen = [
                            name for name in locations if location_selected(name, imported)
                        ] or locations
                    return self._create_entry(chosen)
                return await self.async_step_locations()

        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    async def async_step_locations(self, user_input=None):
        """Choose which locations to add when the account has more than one."""
        errors = {}
        if user_input is not None:
            chosen = user_input.get(CONF_LOCATIONS) or []
            if not chosen:
                errors["base"] = "no_devices"
            else:
                return self._create_entry(list(chosen))
        return self.async_show_form(
            step_id="locations",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LOCATIONS, default=self._location_names
                    ): cv.multi_select({name: name for name in self._location_names})
                }
            ),
            errors=errors,
        )

    async def async_step_import(self, user_input):
        """Import the old climate YAML block once."""
        self._imported_location = user_input.get(CONF_LOCATION)
        return await self.async_step_user(
            {
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
        )

    async def async_step_reauth(self, entry_data):
        """Start a repair when Warmup rejects the saved password."""
        self._username = entry_data[CONF_USERNAME]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Ask for the current password and save it on the existing entry."""
        errors = {}
        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            _locations, error = await self.hass.async_add_executor_job(
                validate_account, self._username, password
            )
            if error:
                errors["base"] = error
            else:
                entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_PASSWORD: password}
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"username": self._username},
        )

    def _create_entry(self, locations):
        """Create the config entry for the validated account."""
        title = locations[0] if len(locations) == 1 else self._username
        return self.async_create_entry(
            title=title,
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_LOCATIONS: list(locations),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return WarmupOptionsFlow()


class WarmupOptionsFlow(OptionsFlow):
    """Let the user slow the poll down if Warmup is rate limiting them."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Edit the poll interval."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    )
                }
            ),
        )
