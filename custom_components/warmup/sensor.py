"""Sensors attached to each Warmup thermostat device."""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .entity import ensure_location_device, thermostat_device_info


async def async_setup_entry(hass, entry, async_add_entities):
    """Add sensors for every thermostat, including ones found on a later poll."""
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
            fresh.extend(_sensors_for(coordinator, serial, device))
        if fresh:
            async_add_entities(fresh)

    _new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_new_entities))


def _sensors_for(coordinator, serial, device):
    """Build the sensors that belong on one thermostat."""
    specs = [
        (
            "air",
            "Air temperature",
            lambda item: item.air_temperature,
            UnitOfTemperature.CELSIUS,
            SensorDeviceClass.TEMPERATURE,
            SensorStateClass.MEASUREMENT,
        ),
        (
            "floor",
            "Floor temperature",
            lambda item: item.floor_temperature,
            UnitOfTemperature.CELSIUS,
            SensorDeviceClass.TEMPERATURE,
            SensorStateClass.MEASUREMENT,
        ),
        (
            "energy",
            "Energy today",
            lambda item: item.energy,
            UnitOfEnergy.KILO_WATT_HOUR,
            SensorDeviceClass.ENERGY,
            SensorStateClass.TOTAL,
        ),
        (
            "cost",
            "Energy cost today",
            lambda item: item.cost,
            None,
            SensorDeviceClass.MONETARY,
            SensorStateClass.TOTAL,
        ),
    ]
    if device.floor_temperature_2 is not None:
        specs.append(
            (
                "floor_2",
                "Floor temperature 2",
                lambda item: item.floor_temperature_2,
                UnitOfTemperature.CELSIUS,
                SensorDeviceClass.TEMPERATURE,
                SensorStateClass.MEASUREMENT,
            )
        )
    if device.energy_total is not None:
        specs.append(
            (
                "energy_total",
                "Energy total",
                lambda item: item.energy_total,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorDeviceClass.ENERGY,
                SensorStateClass.TOTAL_INCREASING,
            )
        )
    return [
        WarmupSensor(coordinator, serial, key, name, reader, unit, device_class, state_class)
        for key, name, reader, unit, device_class, state_class in specs
    ]


class WarmupSensor(CoordinatorEntity, SensorEntity):
    """One reading from a Warmup thermostat."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, serial, key, name, reader, unit, device_class, state_class):
        """Create a sensor on the same device as the climate entity."""
        super().__init__(coordinator)
        self._serial = serial
        self._reader = reader
        device = coordinator.data[serial]
        self._attr_unique_id = "warmup_%s_%s" % (serial, key)
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_device_info = thermostat_device_info(device)

    @property
    def device(self):
        """Return the live thermostat."""
        return (self.coordinator.data or {}).get(self._serial)

    @property
    def available(self):
        """Follow the shared poll. A missing probe stays unavailable instead of showing a blank value."""
        device = self.device
        if not super().available or device is None:
            return False
        return self._reader(device) is not None

    @property
    def native_value(self):
        """Return the latest reading."""
        device = self.device
        if device is None:
            return None
        return self._reader(device)
