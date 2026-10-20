"""Sensors for the temperatures and energy values Warmup already reports."""

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfTemperature

from .climate import DOMAIN

_LOGGER = logging.getLogger(__name__)


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Create sensors for devices the climate platform has already loaded."""
    data = hass.data.get(DOMAIN, {})
    devices = data.get("devices") or []
    client = data.get("client")
    if client is None or not devices:
        return
    entities = []
    for device in devices:
        entities.extend(_sensors_for(device, client))
    add_entities(entities)
    _LOGGER.info("Added %s Warmup sensors", len(entities))


def _sensors_for(device, client):
    room = device.get_room_name()
    sensors = [
        WarmupValueSensor(
            device,
            client,
            "air",
            "%s Air temperature" % room,
            device.get_air_temperature,
            UnitOfTemperature.CELSIUS,
            SensorDeviceClass.TEMPERATURE,
            SensorStateClass.MEASUREMENT,
        ),
        WarmupValueSensor(
            device,
            client,
            "floor",
            "%s Floor temperature" % room,
            device.get_floor_temperature,
            UnitOfTemperature.CELSIUS,
            SensorDeviceClass.TEMPERATURE,
            SensorStateClass.MEASUREMENT,
        ),
        WarmupValueSensor(
            device,
            client,
            "energy",
            "%s Energy today" % room,
            device.get_energy,
            UnitOfEnergy.KILO_WATT_HOUR,
            SensorDeviceClass.ENERGY,
            SensorStateClass.TOTAL,
        ),
        WarmupValueSensor(
            device,
            client,
            "cost",
            "%s Energy cost today" % room,
            device.get_cost,
            None,
            SensorDeviceClass.MONETARY,
            SensorStateClass.TOTAL,
        ),
    ]
    if device.get_floor_temperature_2() is not None:
        sensors.insert(
            2,
            WarmupValueSensor(
                device,
                client,
                "floor_2",
                "%s Floor temperature 2" % room,
                device.get_floor_temperature_2,
                UnitOfTemperature.CELSIUS,
                SensorDeviceClass.TEMPERATURE,
                SensorStateClass.MEASUREMENT,
            ),
        )
    if device.energy_total is not None:
        sensors.append(
            WarmupValueSensor(
                device,
                client,
                "energy_total",
                "%s Energy total" % room,
                lambda: device.energy_total,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorDeviceClass.ENERGY,
                SensorStateClass.TOTAL_INCREASING,
            )
        )
    return sensors


class WarmupValueSensor(SensorEntity):
    """One numeric value from a Warmup room."""

    def __init__(self, device, client, key, name, reader, unit, device_class, state_class):
        """Initialize the sensor."""
        self._device = device
        self._client = client
        self._reader = reader
        self._attr_unique_id = "warmup_%s_%s" % (device.get_serial_number(), key)
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class

    @property
    def native_value(self):
        """Return the latest value published by the shared device update."""
        return self._reader()

    def update(self):
        """Refresh the shared Warmup client. Calls are throttled there."""
        try:
            self._client.update()
        except Exception as err:
            _LOGGER.warning("Warmup sensor update failed for %s: %s", self._attr_name, err)
