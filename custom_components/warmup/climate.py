"""Platform that offers a connection to a warmup device."""
from datetime import datetime, timedelta
import logging
from typing import Any, Dict

import voluptuous as vol
from .warmup4ie import Warmup4IE, Warmup4IEDevice
from .warmup4ie.warmup4ie import (
    infer_hvac_action,
    location_selected,
    minutes_until,
    resolve_displayed_target,
    to_celsius,
)

from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateEntity
from homeassistant.components.climate.const import (
    HVACAction,
    HVACMode,
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_HOME,
    ClimateEntityFeature,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_PASSWORD,
    CONF_USERNAME,
    PRECISION_HALVES,
    UnitOfTemperature,
    MAJOR_VERSION, 
    MINOR_VERSION,
)

from homeassistant.exceptions import InvalidStateError, PlatformNotReady
from homeassistant.helpers import discovery
import homeassistant.helpers.config_validation as cv
from homeassistant.util import Throttle

""" temperature utility will stop working in HA 2023.4. Removed it for now, as CELCIUS is the only temperature standard this module supports anyway. """
# from homeassistant.util.temperature import convert as convert_temperature

DOMAIN = "warmup"
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_USERNAME): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

_LOGGER = logging.getLogger(__name__)

ATTR_UNTIL = "until"
ATTR_DURATION = "duration"
ATTR_DAYS = "days"
ATTR_MODE = "mode"

CONF_LOCATION = "location"
CONF_TARGET_TEMP = "target_temp"
PENDING_TARGET_WINDOW = timedelta(seconds=20)
BOOST_MINUTES = 60

DEFAULT_NAME = "warmup4ie"
DEFAULT_TARGET_TEMP = 20

CONST_MODE_PROGRAM = "prog"  # Default room mode (treated as Home/Auto)
CONST_MODE_FIXED = "fixed"  # Fixed temperature mode (treated as Home/Heat)
CONST_MODE_AWAY = "away"  # WarmUp Geo Away or Holiday Mode (treated as Away/Auto)
CONST_MODE_FROST = "frost"  # Frost Mode (treated as Away/Heat)
CONST_MODE_OFF = "off"  # Switch off heating in a zone

HVAC_MAP_WARMUP_HEAT = {
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

SUPPORT_FLAGS = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE | ClimateEntityFeature.TURN_ON |ClimateEntityFeature.TURN_OFF
SUPPORT_HVAC_HEAT = [HVACMode.HEAT, HVACMode.AUTO, HVACMode.OFF]
SUPPORT_PRESET = [PRESET_AWAY, PRESET_HOME, PRESET_BOOST]

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_LOCATION): cv.string,
    }
)

MIN_TIME_BETWEEN_UPDATES = timedelta(seconds=60)


def _entities_for(hass, call):
    """Return climate entities selected by entity_id, or every Warmup entity."""
    entities = hass.data[DOMAIN]["entities"]
    raw = call.data.get(ATTR_ENTITY_ID)
    if not raw:
        return list(entities)
    wanted = {raw} if isinstance(raw, str) else set(raw)
    return [entity for entity in entities if entity.entity_id in wanted]


def _location_ids(hass, call):
    """Return location ids touched by a service call."""
    wanted = call.data.get(CONF_LOCATION)
    found = {}
    for entity in _entities_for(hass, call):
        name = entity.device.get_location_name()
        if location_selected(name, wanted):
            found[entity.device.get_location_id()] = name
    return found


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the climate devices."""
    hass.data.setdefault(DOMAIN, {})

    def service_set_override(call):
        """Hold the selected rooms at a temperature for a duration."""
        temperature = call.data.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        temperature = to_celsius(temperature, hass.config.units.temperature_unit)
        duration = call.data.get(ATTR_DURATION)
        until = call.data.get(ATTR_UNTIL)
        if duration is not None:
            minutes = int(duration)
        elif until:
            minutes = minutes_until(str(until), datetime.now())
        else:
            minutes = 60
        minutes = min(1440, max(1, minutes))
        for target_device in _entities_for(hass, call):
            target_device.set_override(temperature, minutes)
            target_device.schedule_update_ha_state(True)

    def service_set_location_mode(call):
        """Change every room at a location. Off here is the whole-home control."""
        mode = call.data.get(ATTR_MODE)
        account = hass.data[DOMAIN]["account"]
        for location_id in _location_ids(hass, call):
            if mode == CONST_MODE_OFF:
                account.set_location_to_off(location_id)
            elif mode == CONST_MODE_FROST:
                account.set_location_to_frost(location_id)
            elif mode in (CONST_MODE_PROGRAM, "auto", "schedule"):
                account.set_location_to_auto(location_id)
            else:
                _LOGGER.error("Unknown Warmup location mode %s", mode)
        for target_device in _entities_for(hass, call):
            target_device.schedule_update_ha_state(True)

    def service_set_holiday(call):
        """Start holiday mode for each selected location."""
        temperature = to_celsius(
            call.data.get(ATTR_TEMPERATURE, 5), hass.config.units.temperature_unit
        )
        days = int(call.data.get(ATTR_DAYS, 7))
        account = hass.data[DOMAIN]["account"]
        for location_id in _location_ids(hass, call):
            account.set_holiday(location_id, temperature, days)
        for target_device in _entities_for(hass, call):
            target_device.schedule_update_ha_state(True)

    def service_cancel_holiday(call):
        """Cancel holiday mode for each selected location."""
        account = hass.data[DOMAIN]["account"]
        for location_id in _location_ids(hass, call):
            account.cancel_holiday(location_id)
        for target_device in _entities_for(hass, call):
            target_device.schedule_update_ha_state(True)

    _LOGGER.info("Setting up platform for Warmup component")
    user = config.get(CONF_USERNAME)
    password = config.get(CONF_PASSWORD)
    wanted_location = config.get(CONF_LOCATION)

    warmup = Warmup4IE(user, password)

    if warmup is None or not warmup.setup_finished:
        raise PlatformNotReady
    warmup_client = WarmupClient(warmup)
    devices = []
    to_add = []
    for device in warmup.get_all_devices().values():
        if not location_selected(device.get_location_name(), wanted_location):
            continue
        devices.append(device)
        to_add.append(WarmupThermostat(hass, device, warmup_client))
    add_entities(to_add)
    hass.data[DOMAIN]["account"] = warmup
    hass.data[DOMAIN]["client"] = warmup_client
    hass.data[DOMAIN]["devices"] = devices
    hass.data[DOMAIN]["entities"] = to_add
    hass.services.register(DOMAIN, "set_override", service_set_override)
    hass.services.register(DOMAIN, "set_location_mode", service_set_location_mode)
    hass.services.register(DOMAIN, "set_holiday", service_set_holiday)
    hass.services.register(DOMAIN, "cancel_holiday", service_cancel_holiday)
    try:
        discovery.load_platform(hass, "sensor", DOMAIN, {}, config)
    except Exception as err:
        _LOGGER.warning("Warmup temperature sensors were not created: %s", err)
    return True


class WarmupClient:
    """Wrapper around the API client to throttle requests."""

    def __init__(self, warmup: Warmup4IE):
        """Initialise wrapper."""
        self._warmup = warmup

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def update(self):
        """Throttles calls to the warmup client update method."""
        _LOGGER.debug("Updating Warmup devices")
        self._warmup.update_all_devices()


class WarmupThermostat(ClimateEntity):
    """Representation of a Warmup device."""

    def __init__(self, hass, device: Warmup4IEDevice, client: WarmupClient):
        """Initialize the climate device."""
        _LOGGER.info("Setting up Warmup Thermostat %s", device.get_room_name())
        self._device = device
        self._client = client

        self.attributes = {}
        self._update_attributes_from_device()

        self._current_temperature = device.current_temperature
        self._target_temperature = device.target_temperature
        self._min_temp = device.min_temp
        self._max_temp = device.max_temp
        self._name = device.get_room_name()
        self._attr_unique_id = "warmup_" + device.get_serial_number()

        self._current_operation_mode = device.run_mode
        self._away = False
        self._on = True
        self._pending_target = None
        self._pending_deadline = None

        # https://developers.home-assistant.io/blog/2024/01/24/climate-climateentityfeatures-expanded
        self._enable_turn_on_off_backwards_compatibility = False

    @property
    def device(self):
        """Return the Warmup device wrapped by this entity."""
        return self._device

    @property
    def name(self):
        """Return the name of the climate device."""
        return self._name

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self._current_temperature

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._target_temperature

    @property
    def hvac_mode(self):
        """Return hvac operation ie. heat, cool mode.

        Need to be one of HVACMode.
        """
        return HVAC_MAP_WARMUP_HEAT.get(self._current_operation_mode, HVACMode.AUTO)

    @property
    def hvac_action(self):
        """Return whether the relay is heating, idle, or off.

        The thermostat's outputStatus flag is the relay itself. When Warmup
        does not send it, a room below its target is treated as heating and a
        room at or above its target is idle.
        """
        action = infer_hvac_action(
            self._on,
            self._device.output_status,
            self._current_temperature,
            self._target_temperature,
        )
        return HVAC_ACTION_MAP[action]

    @property
    def preset_mode(self):
        """Return the current preset mode, e.g., home, away, temp."""
        if self._current_operation_mode == "override":
            return PRESET_BOOST
        if self._away:
            return PRESET_AWAY
        return PRESET_HOME

    @property
    def preset_modes(self):
        """Return a list of available preset modes."""
        return SUPPORT_PRESET

    def set_preset_mode(self, preset_mode):
        """Set new preset mode."""

        if preset_mode == PRESET_HOME:
            """Turn away mode off."""
            self._away = False
            self._device.set_temperature_to_auto()
            self._current_operation_mode = CONST_MODE_PROGRAM

        elif preset_mode == PRESET_AWAY:
            """Turn frost protection on for this room only."""
            self._away = True
            self._device.set_room_to_frost()
            self._current_operation_mode = CONST_MODE_FROST

        elif preset_mode == PRESET_BOOST:
            temperature = self._device.comfort_temperature or self._target_temperature
            if not temperature:
                raise InvalidStateError
            self.set_override(temperature, BOOST_MINUTES)

        else:
            raise InvalidStateError

    def set_temperature(self, **kwargs):
        """Set new target temperatures."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        # Keep the requested target on the entity until Warmup's next poll
        # reports it. A slower response used to overwrite the card with the
        # previous setpoint while the user was still tapping plus.
        self._pending_target = temperature
        self._pending_deadline = datetime.now() + PENDING_TARGET_WINDOW
        self._target_temperature = temperature
        self._current_operation_mode = CONST_MODE_FIXED
        self._on = True
        self._away = False
        self._device.set_new_temperature(temperature)

    def set_hvac_mode(self, hvac_mode):
        """Set new target hvac mode.

        Switch device on if was previously off
        """

        if hvac_mode == HVACMode.OFF:
            self._on = False
            self._device.set_room_off()
            self._current_operation_mode = CONST_MODE_OFF

        elif hvac_mode == HVACMode.AUTO:
            self._on = True
            self._device.set_temperature_to_auto()
            self._current_operation_mode = CONST_MODE_PROGRAM

        elif hvac_mode == HVACMode.HEAT:
            self._on = True
            self._device.set_temperature_to_manual()
            self._current_operation_mode = CONST_MODE_FIXED

        else:
            raise InvalidStateError

    def turn_on(self) -> None:
        """Turn the entity on."""
        self.set_hvac_mode(HVACMode.AUTO)

    def turn_off(self) -> None:
        """Turn the entity off."""
        self.set_hvac_mode(HVACMode.OFF)

    def set_override(self, temperature, minutes):
        """Hold this room at ``temperature`` °C for ``minutes``."""
        self._device.set_override(temperature, minutes)
        self._current_operation_mode = "override"
        self._on = True
        self._away = False
        self._pending_target = temperature
        self._pending_deadline = datetime.now() + PENDING_TARGET_WINDOW

    def update(self):
        """Fetch new state data for this device.

        This is the only method that should fetch new data for Home Assistant.
        """
        try:
            self._client.update()
        except Exception as err:
            _LOGGER.warning("Warmup update failed for %s: %s", self._name, err)
            return

        device_mode = self._device.get_run_mode()
        displayed, pending = resolve_displayed_target(
            self._device.target_temperature,
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
        self._current_temperature = self._device.current_temperature
        self._min_temp = self._device.min_temp
        self._max_temp = self._device.max_temp

        self._update_attributes_from_device()

        # set whether device is in away mode
        if (
            self._current_operation_mode == CONST_MODE_AWAY
            or self._current_operation_mode == CONST_MODE_FROST
        ):
            self._away = True
        else:
            self._away = False

        # set whether device is on/off
        if self._current_operation_mode == CONST_MODE_OFF:
            self._on = False
        else:
            self._on = True

    def _update_attributes_from_device(self):
        _attributes = {
            "floor_temperature": self._device.floor_temperature,
            "air_temperature": self._device.air_temperature,
            "away_temperature": self._device.away_temperature,
            "comfort_temperature": self._device.comfort_temperature,
            "cost": self._device.cost,
            "energy": self._device.energy,
            "energy_total": self._device.energy_total,
            "output_status": self._device.output_status,
            "floor_temperature_2": self._device.floor_temperature_2,
            "fixed_temperature": self._device.fixed_temperature,
            "override_temperature": self._device.override_temperature,
            "override_duration_mins": self._device.override_duration_mins,
            "sleep_temperature": self._device.sleep_temperature,
            "run_mode": self._device.run_mode,
            "room_mode": self._device.room_mode,
            "heating_target": self._device.heating_target,
        }
        self.attributes = _attributes

    @property
    def state_attributes(self) -> Dict[str, Any]:
        """Return the optional state attributes."""
        return {**super(WarmupThermostat, self).state_attributes, **self.attributes}

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        # return convert_temperature(
        #    self._device.min_temp, UnitOfTemperature.CELSIUS, self.hass.config.units.temperature_unit
        # )
        return self._device.min_temp

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        # return convert_temperature(
        #     self._device.max_temp, UnitOfTemperature.CELSIUS, self.hass.config.units.temperature_unit
        # )
        return self._device.max_temp

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return SUPPORT_FLAGS

    @property
    def should_poll(self):
        """Return the polling state."""
        return True

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return UnitOfTemperature.CELSIUS

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        return PRECISION_HALVES

    @property
    def hvac_modes(self):
        """Return the list of available hvac operation modes.

        Need to be a subset of HVAC_MODES.
        """
        return SUPPORT_HVAC_HEAT
