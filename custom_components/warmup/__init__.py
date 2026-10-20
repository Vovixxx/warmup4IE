"""Warmup integration. One config entry polls the account and owns the devices."""

from datetime import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
import homeassistant.helpers.config_validation as cv
import voluptuous as vol

from .const import (
    ATTR_DAYS,
    ATTR_DURATION,
    ATTR_MODE,
    ATTR_UNTIL,
    BOOST_MINUTES,
    CONF_LOCATION,
    CONST_MODE_FROST,
    CONST_MODE_OFF,
    CONST_MODE_PROGRAM,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import WarmupCoordinator
from .warmup4ie import Warmup4IE, WarmupAuthError
from .warmup4ie.warmup4ie import location_selected, minutes_until, snap_setpoint, to_celsius

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SET_OVERRIDE = "set_override"
SERVICE_SET_LOCATION_MODE = "set_location_mode"
SERVICE_SET_HOLIDAY = "set_holiday"
SERVICE_CANCEL_HOLIDAY = "cancel_holiday"


async def async_setup(hass: HomeAssistant, config):
    """YAML lives under the climate platform and is imported from there."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Log in, start the shared poll, and create the platforms."""
    account = await hass.async_add_executor_job(
        Warmup4IE, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
    )
    if isinstance(account.last_error, WarmupAuthError):
        raise ConfigEntryAuthFailed(str(account.last_error)) from account.last_error
    if not account.setup_finished:
        raise ConfigEntryNotReady(str(account.last_error or "Warmup is not ready"))

    coordinator = WarmupCoordinator(hass, account, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "account": account,
        "coordinator": coordinator,
        "entities": [],
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _async_register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Remove an account and its entities."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            for service in (
                SERVICE_SET_OVERRIDE,
                SERVICE_SET_LOCATION_MODE,
                SERVICE_SET_HOLIDAY,
                SERVICE_CANCEL_HOLIDAY,
            ):
                hass.services.async_remove(DOMAIN, service)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Reload when the poll interval changes."""
    await hass.config_entries.async_reload(entry.entry_id)


def _iter_entities(hass):
    """Yield climate entities from every configured account."""
    for runtime in hass.data.get(DOMAIN, {}).values():
        for entity in runtime.get("entities", []):
            yield entity


def _selected_entities(hass, call):
    """Return entities named by the call, or every Warmup climate entity."""
    entities = list(_iter_entities(hass))
    raw = call.data.get(ATTR_ENTITY_ID)
    if not raw:
        return entities
    wanted = {raw} if isinstance(raw, str) else set(raw)
    return [entity for entity in entities if entity.entity_id in wanted]


def _location_ids(hass, call):
    """Return the location ids the service should change."""
    wanted = call.data.get(CONF_LOCATION)
    found = {}
    for entity in _selected_entities(hass, call):
        device = entity.device
        if device is None:
            continue
        if location_selected(device.get_location_name(), wanted):
            found[device.get_location_id()] = device.get_location_name()
    return found


async def _refresh_all(hass):
    """Ask every account to poll again after a command."""
    for runtime in hass.data.get(DOMAIN, {}).values():
        await runtime["coordinator"].async_request_refresh()


async def _async_register_services(hass: HomeAssistant):
    """Register account services once, even when several entries exist."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_OVERRIDE):
        return

    async def async_set_override(call):
        """Hold the selected rooms at a temperature."""
        temperature = call.data.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        temperature = snap_setpoint(
            to_celsius(temperature, hass.config.units.temperature_unit)
        )
        duration = call.data.get(ATTR_DURATION)
        until = call.data.get(ATTR_UNTIL)
        if duration is not None:
            minutes = int(duration)
        elif until:
            minutes = minutes_until(str(until), datetime.now())
        else:
            minutes = BOOST_MINUTES
        minutes = min(1440, max(1, minutes))
        targets = [entity for entity in _selected_entities(hass, call) if entity.device]
        for entity in targets:
            entity.remember_target(temperature, "override")
        await hass.async_add_executor_job(_override_many, targets, temperature, minutes)
        await _refresh_all(hass)

    async def async_set_location_mode(call):
        """Change every room at the selected locations."""
        mode = call.data.get(ATTR_MODE)
        location_ids = _location_ids(hass, call)
        accounts = _accounts_for(hass, location_ids)
        await hass.async_add_executor_job(_apply_location_mode, accounts, location_ids, mode)
        await _refresh_all(hass)

    async def async_set_holiday(call):
        """Start holiday mode for the selected locations."""
        temperature = to_celsius(
            call.data.get(ATTR_TEMPERATURE, 5), hass.config.units.temperature_unit
        )
        days = int(call.data.get(ATTR_DAYS, 7))
        location_ids = _location_ids(hass, call)
        accounts = _accounts_for(hass, location_ids)
        await hass.async_add_executor_job(_apply_holiday, accounts, location_ids, temperature, days)
        await _refresh_all(hass)

    async def async_cancel_holiday(call):
        """Cancel holiday mode for the selected locations."""
        location_ids = _location_ids(hass, call)
        accounts = _accounts_for(hass, location_ids)
        await hass.async_add_executor_job(_cancel_holiday, accounts, location_ids)
        await _refresh_all(hass)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_OVERRIDE,
        async_set_override,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
                vol.Required(ATTR_TEMPERATURE): vol.Coerce(float),
                vol.Optional(ATTR_DURATION): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=1440)
                ),
                vol.Optional(ATTR_UNTIL): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_LOCATION_MODE,
        async_set_location_mode,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
                vol.Optional(CONF_LOCATION): cv.string,
                vol.Required(ATTR_MODE): vol.In(
                    [CONST_MODE_OFF, CONST_MODE_FROST, CONST_MODE_PROGRAM, "auto", "schedule"]
                ),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_HOLIDAY,
        async_set_holiday,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
                vol.Optional(CONF_LOCATION): cv.string,
                vol.Optional(ATTR_TEMPERATURE, default=5): vol.Coerce(float),
                vol.Optional(ATTR_DAYS, default=7): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=365)
                ),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CANCEL_HOLIDAY,
        async_cancel_holiday,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
                vol.Optional(CONF_LOCATION): cv.string,
            }
        ),
    )


def _accounts_for(hass, location_ids):
    """Return account objects that own one of the location ids."""
    accounts = []
    for runtime in hass.data.get(DOMAIN, {}).values():
        account = runtime["account"]
        if any(location_id in account._locations for location_id in location_ids):
            accounts.append(account)
    return accounts


def _override_many(entities, temperature, minutes):
    """Apply overrides from a worker thread."""
    for entity in entities:
        device = entity.device
        if device is not None:
            device.set_override(temperature, minutes)


def _apply_location_mode(accounts, location_ids, mode):
    """Apply a location-wide mode from a worker thread."""
    for account in accounts:
        for location_id in location_ids:
            if location_id not in account._locations:
                continue
            if mode == CONST_MODE_OFF:
                account.set_location_to_off(location_id)
            elif mode == CONST_MODE_FROST:
                account.set_location_to_frost(location_id)
            elif mode in (CONST_MODE_PROGRAM, "auto", "schedule"):
                account.set_location_to_auto(location_id)
            else:
                _LOGGER.error("Unknown Warmup location mode %s", mode)


def _apply_holiday(accounts, location_ids, temperature, days):
    """Start holiday mode from a worker thread."""
    for account in accounts:
        for location_id in location_ids:
            if location_id in account._locations:
                account.set_holiday(location_id, temperature, days)


def _cancel_holiday(accounts, location_ids):
    """Cancel holiday mode from a worker thread."""
    for account in accounts:
        for location_id in location_ids:
            if location_id in account._locations:
                account.cancel_holiday(location_id)
