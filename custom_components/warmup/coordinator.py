"""One poll of the Warmup account, shared by every entity."""

from datetime import timedelta
import logging

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_LOCATIONS, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN
from .warmup4ie.warmup4ie import (
    WarmupAuthError,
    device_identity,
    location_selected,
)

_LOGGER = logging.getLogger(__name__)


class WarmupCoordinator(DataUpdateCoordinator):
    """Fetch every thermostat once per interval.

    Entities used to poll on their own timer. Those polls overlapped, and a
    single failed request left one room updated and another showing the
    previous heating state. This coordinator is the only reader.
    """

    def __init__(self, hass, account, entry):
        """Create the coordinator for one config entry."""
        seconds = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=seconds),
        )
        self.account = account
        self.entry = entry
        self._use_login_snapshot = True

    def _devices_from_account(self):
        """Index the thermostats selected for this entry."""
        selected = self.entry.data.get(CONF_LOCATIONS) or []
        devices = {}
        for device in self.account.get_all_devices().values():
            if selected and not any(
                location_selected(device.get_location_name(), name) for name in selected
            ):
                continue
            devices[device_identity(device)] = device
        return devices

    async def _async_update_data(self):
        """Return the current thermostats, or raise so Home Assistant marks them unavailable.

        Login already fetched the account. The first refresh reuses that
        snapshot so a second request cannot fail setup after a good login.
        """
        if self._use_login_snapshot:
            self._use_login_snapshot = False
            if self.account.last_error is None:
                return self._devices_from_account()

        await self.hass.async_add_executor_job(self.account.update_all_devices)
        error = self.account.last_error
        if isinstance(error, WarmupAuthError):
            raise ConfigEntryAuthFailed(str(error)) from error
        if error is not None:
            raise UpdateFailed(str(error))
        return self._devices_from_account()
