"""Device registry helpers shared by climate and sensor entities."""

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .warmup4ie.warmup4ie import device_identity


def location_identifier(location_id):
    """Return the device-registry id for a Warmup location."""
    return "location-%s" % location_id


def ensure_location_device(hass, entry, device):
    """Create the location device that thermostats hang from."""
    identifier = location_identifier(device.get_location_id())
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, identifier)},
        manufacturer="Warmup",
        model="MyWarmup",
        name=device.get_location_name() or "Warmup",
        configuration_url="https://my.warmup.com/",
    )
    return identifier


def thermostat_device_info(device):
    """Return the device info for one thermostat."""
    return DeviceInfo(
        identifiers={(DOMAIN, device_identity(device))},
        manufacturer="Warmup",
        model="Thermostat",
        name=device.get_room_name() or "Warmup",
        suggested_area=device.get_room_name(),
        sw_version=device.firmware,
        via_device=(DOMAIN, location_identifier(device.get_location_id())),
        configuration_url="https://my.warmup.com/",
    )
