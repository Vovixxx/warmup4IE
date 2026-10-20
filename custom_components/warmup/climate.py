"""Climate entities for Warmup thermostats."""

from datetime import datetime, timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateEntity
from homeassistant.components.climate.const import (
    HVACAction,
    HVACMode,
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_HOME,
    ClimateEntityFeature,
)
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_PASSWORD,
    CONF_USERNAME,
    PRECISION_HALVES,
    UnitOfTemperature,
)
from homeassistant.exceptions import InvalidStateError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BOOST_MINUTES,
    CONF_LOCATION,
    CONST_MODE_AWAY,
    CONST_MODE_FIXED,
    CONST_MODE_FROST,
    CONST_MODE_OFF,
    CONST_MODE_PROGRAM,
    DOMAIN,
)
from .entity import ensure_location_device, thermostat_device_info
from .warmup4ie.warmup4ie import infer_hvac_action, resolve_displayed_target

_LOGGER = logging.getLogger(__name__)

PENDING_TARGET_WINDOW = timedelta(seconds=20)

HVAC_MAP = {
    CONST_MODE_PROGRAM: HVACMode.AUTO,
    CONST_MODE_FIXED: HVACMode.HEAT,
    "override": HVACMode.HEAT,
    CONST_MODE_AWAY: HVACMode.AUTO,
    CONST_MODE_FROST: HVACMode.HEAT,
    CONST_MODE_OFF: HVACMode.OFF,
}
HVAC_ACTION_MAP = {
    "off": HVACAction.OFF,
    "heating": HVACAction.HEATING,
    "idle": HVACAction.IDLE,
}

SUPPORT_FLAGS = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.PRESET_MODE
    | ClimateEntityFeature.TURN_ON
    | ClimateEntityFeature.TURN_OFF
)
SUPPORT_HVAC = [HVACMode.HEAT, HVACMode.AUTO, HVACMode.OFF]
SUPPORT_PRESET = [PRESET_AWAY, PRESET_HOME, PRESET_BOOST]

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_LOCATION): cv.string,
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Import the legacy YAML climate block into a config entry."""
    username = str(config[CONF_USERNAME]).strip().lower()
    for entry in hass.config_entries.async_entries(DOMAIN):
        existing = str(entry.data.get(CONF_USERNAME, "")).strip().lower()
        if entry.unique_id == username or existing == username:
            return
    _LOGGER.warning(
        "Warmup YAML configuration is imported into Settings > Devices & services. "
        "Remove the climate warmup block after the entry exists"
    )
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={
                CONF_USERNAME: config[CONF_USERNAME],
                CONF_PASSWORD: config[CONF_PASSWORD],
                CONF_LOCATION: config.get(CONF_LOCATION),
            },
        )
    )


async def async_setup_entry(hass, entry, async_add_entities):
    """Add one climate entity per thermostat, and add new ones that appear later."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    coordinator = runtime["coordinator"]
    known = set()

    def _new_entities():
        fresh = []
        for serial, device in (coordinator.data or {}).items():
            if serial in known:
                continue
            known.add(serial)
            ensure_location_device(hass, entry, device)
            fresh.append(WarmupThermostat(coordinator, serial))
        if fresh:
            runtime["entities"].extend(fresh)
            async_add_entities(fresh)

    _new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_new_entities))


class WarmupThermostat(CoordinatorEntity, ClimateEntity):
    """One Warmup room."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = SUPPORT_FLAGS
    _attr_hvac_modes = SUPPORT_HVAC
    _attr_preset_modes = SUPPORT_PRESET
    _attr_target_temperature_step = PRECISION_HALVES

    def __init__(self, coordinator, serial):
        """Attach this entity to a thermostat already returned by the coordinator."""
        super().__init__(coordinator)
        self._serial = serial
        device = coordinator.data[serial]
        self._attr_unique_id = "warmup_%s" % serial
        self._attr_device_info = thermostat_device_info(device)
        self._pending_target = None
        self._pending_deadline = None
        self._target_temperature = device.target_temperature
        self._current_temperature = device.current_temperature
        self._current_operation_mode = device.run_mode
        self._min_temp = device.min_temp
        self._max_temp = device.max_temp
        self._on = device.run_mode != CONST_MODE_OFF
        self._away = device.run_mode in (CONST_MODE_AWAY, CONST_MODE_FROST)

    @property
    def device(self):
        """Return the live thermostat, or None when this poll did not include it."""
        return (self.coordinator.data or {}).get(self._serial)

    @property
    def available(self):
        """Unavailable when the shared poll failed or this thermostat disappeared."""
        return super().available and self.device is not None

    def remember_target(self, temperature, mode):
        """Show a command immediately, before the next poll confirms it."""
        self._pending_target = float(temperature)
        self._pending_deadline = datetime.now() + PENDING_TARGET_WINDOW
        self._target_temperature = float(temperature)
        self._current_operation_mode = mode
        self._on = True
        self._away = False
        self.async_write_ha_state()

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self._current_temperature

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._target_temperature

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        return self._min_temp

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        return self._max_temp

    @property
    def hvac_mode(self):
        """Return heat, auto, or off."""
        return HVAC_MAP.get(self._current_operation_mode, HVACMode.AUTO)

    @property
    def hvac_action(self):
        """Return heating only when the relay is on, or the room is below target."""
        action = infer_hvac_action(
            self._on,
            None if self.device is None else self.device.output_status,
            self._current_temperature,
            self._target_temperature,
        )
        return HVAC_ACTION_MAP[action]

    @property
    def preset_mode(self):
        """Return home, away, or boost."""
        if self._current_operation_mode == "override":
            return PRESET_BOOST
        if self._away:
            return PRESET_AWAY
        return PRESET_HOME

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the extra Warmup readings on the climate entity."""
        device = self.device
        if device is None:
            return {}
        return {
            "floor_temperature": device.floor_temperature,
            "air_temperature": device.air_temperature,
            "away_temperature": device.away_temperature,
            "comfort_temperature": device.comfort_temperature,
            "cost": device.cost,
            "energy": device.energy,
            "energy_total": device.energy_total,
            "output_status": device.output_status,
            "floor_temperature_2": device.floor_temperature_2,
            "fixed_temperature": device.fixed_temperature,
            "override_temperature": device.override_temperature,
            "override_duration_mins": device.override_duration_mins,
            "sleep_temperature": device.sleep_temperature,
            "run_mode": device.run_mode,
            "room_mode": device.room_mode,
            "heating_target": device.heating_target,
        }

    async def async_set_preset_mode(self, preset_mode):
        """Set home, away, or boost for this room only."""
        device = self.device
        if device is None:
            return
        if preset_mode == PRESET_HOME:
            self._away = False
            self._current_operation_mode = CONST_MODE_PROGRAM
            self.async_write_ha_state()
            await self.hass.async_add_executor_job(device.set_temperature_to_auto)
        elif preset_mode == PRESET_AWAY:
            self._away = True
            self._current_operation_mode = CONST_MODE_FROST
            self.async_write_ha_state()
            await self.hass.async_add_executor_job(device.set_room_to_frost)
        elif preset_mode == PRESET_BOOST:
            temperature = device.comfort_temperature or self._target_temperature
            if not temperature:
                raise InvalidStateError
            self.remember_target(temperature, "override")
            await self.hass.async_add_executor_job(
                device.set_override, temperature, BOOST_MINUTES
            )
        else:
            raise InvalidStateError
        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs):
        """Set a fixed target for this room."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        device = self.device
        if temperature is None or device is None:
            return
        self.remember_target(temperature, CONST_MODE_FIXED)
        await self.hass.async_add_executor_job(device.set_new_temperature, temperature)
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode):
        """Change this room. Off does not turn off the other rooms."""
        device = self.device
        if device is None:
            return
        if hvac_mode == HVACMode.OFF:
            self._on = False
            self._current_operation_mode = CONST_MODE_OFF
            self.async_write_ha_state()
            await self.hass.async_add_executor_job(device.set_room_off)
        elif hvac_mode == HVACMode.AUTO:
            self._on = True
            self._current_operation_mode = CONST_MODE_PROGRAM
            self.async_write_ha_state()
            await self.hass.async_add_executor_job(device.set_temperature_to_auto)
        elif hvac_mode == HVACMode.HEAT:
            self._on = True
            self._current_operation_mode = CONST_MODE_FIXED
            self.async_write_ha_state()
            await self.hass.async_add_executor_job(device.set_temperature_to_manual)
        else:
            raise InvalidStateError
        await self.coordinator.async_request_refresh()

    def _handle_coordinator_update(self):
        """Apply a poll without letting a stale target overwrite a recent command."""
        device = self.device
        if device is None:
            self.async_write_ha_state()
            return
        device_mode = device.get_run_mode()
        displayed, pending = resolve_displayed_target(
            device.target_temperature,
            self._pending_target,
            datetime.now(),
            self._pending_deadline,
            self._target_temperature,
        )
        self._pending_target = pending
        if pending is None:
            self._pending_deadline = None
            self._current_operation_mode = device_mode
        if displayed is not None:
            self._target_temperature = displayed
        self._current_temperature = device.current_temperature
        self._min_temp = device.min_temp
        self._max_temp = device.max_temp
        self._away = self._current_operation_mode in (CONST_MODE_AWAY, CONST_MODE_FROST)
        self._on = self._current_operation_mode != CONST_MODE_OFF
        self.async_write_ha_state()
