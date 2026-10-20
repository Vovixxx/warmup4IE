"""
platform that offers a connection to a warmup4ie device.

this platform is inspired by the following code:
https://github.com/alyc100/SmartThingsPublic/tree/master/devicetypes/alyc100/\
warmup-4ie.src

to setup this component, you need to register to warmup first.
see
https://my.warmup.com/login

Then add to your
configuration.yaml

climate:
  - platform: warmup
    username: YOUR_E_MAIL_ADDRESS
    password: YOUR_PASSWORD

Reads use the Warmup GraphQL query. Writes that must affect one room
(off, frost, fixed temperature, override) use the per-room GraphQL
mutations. Location-wide off, frost, schedule and holiday stay available
as explicit account methods.
"""

import logging
import re
from datetime import timedelta

import requests

_LOGGER = logging.getLogger(__name__)

RUN_MODE = {
    0: "off",
    1: "prog",
    2: "override",
    3: "fixed",
    4: "frost",
    5: "away",
    6: "flip",
    7: "grad",
    8: "relay",
}
RUN_MODE_FROM_API = {
    "off": "off",
    "not_set": "off",
    "schedule": "prog",
    "program": "prog",
    "prog": "prog",
    "override": "override",
    "fixed": "fixed",
    "anti_frost": "frost",
    "frost": "frost",
    "holiday": "away",
    "away": "away",
    "fil_pilote": "flip",
    "gradual": "grad",
    "relay": "relay",
    "previous": "prog",
}
ROOM_MODE = {1: "prog", 3: "fixed"}
ROOM_MODE_FROM_API = {
    "program": "prog",
    "prog": "prog",
    "fixed": "fixed",
}
HEATING_TARGET = {0: "floor", 1: "air"}
HEATING_TARGET_FROM_API = {"floor": "floor", "air": "air"}

# An unwired probe is reported as 90.0°C (900 tenths), which is not a reading.
NO_PROBE_TENTHS = 900
REQUEST_TIMEOUT = 20
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")
_TOKEN_ERROR = re.compile(r"token|unauthor|forbidden|\bauth", re.IGNORECASE)

GQL_DEVICES = """
query Devices {
  user {
    allLocations: locations {
      id
      name
      rooms {
        id
        roomName
        runMode
        runModeInt
        roomMode
        roomModeInt
        targetTemp
        currentTemp
        awayTemp
        comfortTemp
        cost
        energy
        total
        fixedTemp
        overrideTemp
        overrideDur
        sleepTemp
        thermostat4ies {
          id
          deviceSN
          minTemp
          maxTemp
          airTemp
          floor1Temp
          floor2Temp
          heatingTarget
          heatingTargetInt
          parameters { outputStatus }
        }
      }
    }
  }
}
""".strip()

GQL_DEVICE_OFF_ROOM = (
    "mutation DeviceOff($lid: Int!, $rid: Int!) "
    "{ deviceOff(lid: $lid, rid: $rid) }"
)
GQL_DEVICE_OFF_LOCATION = (
    "mutation DeviceOff($lid: Int!) { deviceOff(lid: $lid) }"
)
GQL_DEVICE_FROST_ROOM = (
    "mutation DeviceFrost($lid: Int!, $rid: Int!) "
    "{ deviceFrost(lid: $lid, rid: $rid) }"
)
GQL_DEVICE_FROST_LOCATION = (
    "mutation DeviceFrost($lid: Int!) { deviceFrost(lid: $lid) }"
)
GQL_DEVICE_PROGRAM_ROOM = (
    "mutation DeviceProgram($lid: Int!, $rid: Int!) "
    "{ deviceProgram(lid: $lid, rid: $rid) }"
)
GQL_DEVICE_PROGRAM_LOCATION = (
    "mutation DeviceProgram($lid: Int!) { deviceProgram(lid: $lid) }"
)
GQL_DEVICE_FIXED = (
    "mutation DeviceFixed($lid: Int!, $rid: Int!, $temperature: Int!) "
    "{ deviceFixed(lid: $lid, rid: $rid, temperature: $temperature) }"
)
GQL_DEVICE_FIXED_MODE = (
    "mutation DeviceFixed($lid: Int!, $rid: Int!) "
    "{ deviceFixed(lid: $lid, rid: $rid) }"
)
GQL_DEVICE_OVERRIDE = (
    "mutation DeviceOverride($lid: Int!, $rid: Int!, $temperature: Int!, $minutes: Int!) "
    "{ deviceOverride(lid: $lid, rid: $rid, temperature: $temperature, minutes: $minutes) }"
)
GQL_DEVICE_HOLIDAY = (
    "mutation DeviceHoliday($lid: Int!, $temperature: Int!, $days: Int!) "
    "{ deviceHoliday(lid: $lid, temperature: $temperature, days: $days) }"
)
GQL_CANCEL_HOLIDAY = (
    "mutation CancelHoliday($lid: Int!) { cancelHoliday(lid: $lid) }"
)


def normalize_run_mode(run_mode=None, run_mode_int=None):
    """Return the internal run mode name used by the climate entity."""
    if isinstance(run_mode, str) and run_mode.strip():
        key = run_mode.strip()
        return RUN_MODE_FROM_API.get(key, key)
    if run_mode_int is None or run_mode_int == "":
        return None
    try:
        return RUN_MODE.get(int(run_mode_int))
    except (TypeError, ValueError):
        return None


def normalize_room_mode(room_mode=None, room_mode_int=None):
    """Return prog or fixed for a room mode payload."""
    if isinstance(room_mode, str) and room_mode.strip():
        return ROOM_MODE_FROM_API.get(room_mode.strip(), room_mode.strip())
    if room_mode_int is None or room_mode_int == "":
        return None
    try:
        return ROOM_MODE.get(int(room_mode_int))
    except (TypeError, ValueError):
        return None


def infer_hvac_action(is_on, output_status, current, target):
    """Return off, heating, or idle.

    ``output_status`` is the relay flag from the thermostat. When Warmup
    omits it, heating is inferred only while the room is below the target.
    A room sitting on its target is idle; treating equality as heating is
    what made the climate card stay on ``heating`` all day.
    """
    if not is_on:
        return "off"
    if output_status is not None:
        return "heating" if int(output_status) else "idle"
    if current is not None and target is not None and current < target:
        return "heating"
    return "idle"


def resolve_displayed_target(device_target, pending_target, now, deadline, last_target):
    """Choose the target to show after a local change or an off reading.

    A poll that still has the previous target is ignored until the pending
    change expires or the device reports the new value. A target of 0, which
    Warmup sends for a thermostat that is off, keeps the last real setpoint.
    """
    if pending_target is not None and deadline is not None and now < deadline:
        if (
            device_target is not None
            and abs(float(device_target) - float(pending_target)) < 0.05
        ):
            return float(pending_target), None
        return float(pending_target), pending_target
    if device_target:
        return float(device_target), None
    if last_target:
        return float(last_target), None
    return device_target, None


def minutes_until(until, now):
    """Convert an HH:MM clock time into minutes from ``now``, rolling to tomorrow."""
    hour_text, minute_text = str(until).split(":", 1)
    target = now.replace(
        hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0
    )
    if target <= now:
        target = target + timedelta(days=1)
    minutes = int((target - now).total_seconds() // 60)
    return min(1440, max(1, minutes))


def location_selected(device_location, wanted):
    """Return True when ``wanted`` is empty or matches the device location."""
    if wanted is None or str(wanted).strip() == "":
        return True
    if device_location is None:
        return False
    return device_location.strip().casefold() == str(wanted).strip().casefold()


def to_celsius(value, unit):
    """Convert a Home Assistant temperature into Celsius for the Warmup API."""
    temperature = float(value)
    normalized = str(unit or "°C").strip().upper().replace("°", "")
    if normalized in {"C", "CELSIUS"}:
        return temperature
    if normalized in {"F", "FAHRENHEIT"}:
        return (temperature - 32.0) * 5.0 / 9.0
    if normalized in {"K", "KELVIN"}:
        return temperature - 273.15
    return temperature


def _tenths(value, probe=False):
    """Convert a Warmup tenth-of-a-degree field into Celsius."""
    if value is None or value == "":
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if probe and raw == NO_PROBE_TENTHS:
        return None
    return raw / 10


def _parse_number(value):
    """Return a float from a Warmup energy or cost field."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER.search(str(value).replace(",", "."))
    if not match:
        return None
    return float(match.group())


def _warmup_temperature(value):
    """Return the integer tenth-of-a-degree value the API expects."""
    return int(round(float(value) * 10))


class Warmup4IE:
    TOKEN_URL = "https://api.warmup.com/apps/app/v1"
    URL = "https://apil.warmup.com/graphql"
    APP_TOKEN = 'M=;He<Xtg"$}4N%5k{$:PD+WA"]D<;#PriteY|VTuA>_iyhs+vA"4lic{6-LqNM:'
    HEADER = {
        "user-agent": "WARMUP_APP",
        "accept-encoding": "br, gzip, deflate",
        "accept": "*/*",
        "Connection": "keep-alive",
        "content-type": "application/json",
        "app-token": APP_TOKEN,
        "app-version": "1.8.1",
        "accept-language": "en-gb",
    }

    def __init__(self, user, password):
        """Initialize the climate device."""
        self._user = user
        self._password = password
        self._warmup_access_token = None
        self.setup_finished = False
        self._locations = {}
        self._rooms = {}
        self._all_devices = {}
        if self.connect():
            self.update_all_devices()
            self.setup_finished = True

    def connect(self):
        """Retrieve an access token from the Warmup login API."""
        body = {
            "request": {
                "email": self._user,
                "password": self._password,
                "method": "userLogin",
                "appId": "WARMUP-APP-V001",
            }
        }
        payload = self._post(self.TOKEN_URL, body, self.HEADER)
        if not payload:
            return False
        status = (payload.get("status") or {}).get("result")
        if status != "success":
            code = (payload.get("response") or {}).get("errorCode")
            if code == 101:
                _LOGGER.error(
                    "Warmup login failed: invalid email or password (errorCode 101)"
                )
            else:
                _LOGGER.error("Warmup login failed, errorCode %s", code)
            self._warmup_access_token = None
            return False
        token = (payload.get("response") or {}).get("token")
        if not token:
            _LOGGER.error("Warmup login response did not include a token")
            self._warmup_access_token = None
            return False
        self._warmup_access_token = token
        return True

    def _post(self, url, body, headers):
        """POST JSON and return the decoded body, or None when the call fails."""
        try:
            response = requests.post(
                url=url, headers=headers, json=body, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as err:
            _LOGGER.warning("Warmup request failed: %s", err)
            return None
        if response.status_code == 401:
            return {"errors": [{"message": "unauthorized"}]}
        try:
            return response.json()
        except ValueError:
            _LOGGER.warning(
                "Warmup returned a non-JSON response (%s)", response.status_code
            )
            return None

    def _graphql(self, query, variables, allow_refresh=True):
        """Run one GraphQL operation, refreshing the access token once if it expired."""
        if self._warmup_access_token is None and not self.connect():
            _LOGGER.error("Access token is not set. Try calling connect first")
            return None
        headers = self.HEADER.copy()
        headers["warmup-authorization"] = str(self._warmup_access_token)
        headers["x-request-type"] = "GraphQL"
        payload = self._post(
            self.URL, {"query": query, "variables": variables}, headers
        )
        if payload is None:
            return None
        errors = payload.get("errors") or []
        if errors:
            message = "; ".join(error.get("message", "") for error in errors)
            if allow_refresh and _TOKEN_ERROR.search(message):
                _LOGGER.info("Warmup rejected the access token; signing in again")
                self._warmup_access_token = None
                if self.connect():
                    return self._graphql(query, variables, allow_refresh=False)
            _LOGGER.error("Warmup GraphQL error: %s", message)
            return None
        status = payload.get("status")
        if isinstance(status, str) and status != "success":
            _LOGGER.error("Warmup GraphQL status %s", status)
            return None
        if isinstance(status, dict) and status.get("result") not in (None, "success"):
            _LOGGER.error("Warmup GraphQL status %s", status)
            return None
        return payload.get("data", payload)

    def _mutation(self, query, variables):
        """Return True when a mutation is accepted."""
        data = self._graphql(query, variables)
        if not data:
            return False
        return all(value is not False for value in data.values())

    def get_locations(self):
        """Return the locations found by the last device update."""
        if self._warmup_access_token is None and not self.connect():
            _LOGGER.error("Access token is not set. Try calling connect first")
            return None
        return [
            {"id": location_id, "name": name}
            for location_id, name in self._locations.items()
        ]

    def get_all_devices(self):
        return self._all_devices

    def get_location_id(self, location_name):
        for location_id, name in self._locations.items():
            if name == location_name:
                return location_id
        return None

    def get_room_id(self, room_name):
        for room_id, name in self._rooms.items():
            if name == room_name:
                return room_id
        return None

    def get_device_by_name(self, room_name, location=None):
        location_id = None
        room_id = self.get_room_id(room_name)
        if location is not None:
            location_id = self.get_location_id(location)

        for (device_location_id, device_room_id, _thermostat_id), device in self._all_devices.items():
            if room_id == device_room_id:
                if location_id is None or location_id == device_location_id:
                    return device
        return None

    def update_all_devices(self):
        """Update all devices that are registered with this account."""
        data = self._graphql(GQL_DEVICES, {})
        if data is None:
            return None
        user = data.get("user") or {}
        locations = user.get("allLocations") or []

        for location in locations:
            self._locations[location["id"]] = location["name"]
            for room in location.get("rooms") or []:
                self._rooms[room["id"]] = room["roomName"]
                for thermostat in room.get("thermostat4ies") or []:
                    key = (location["id"], room["id"], thermostat.get("id"))
                    device_to_update = self._all_devices.get(key)
                    if device_to_update is None:
                        _LOGGER.info(
                            "Found thermostat with SN %s, name %s in location %s ",
                            thermostat.get("deviceSN"),
                            room.get("roomName"),
                            location.get("name"),
                        )
                        device_to_update = Warmup4IEDevice(
                            location["id"],
                            location["name"],
                            room["id"],
                            room["roomName"],
                            thermostat.get("id"),
                            thermostat.get("deviceSN"),
                            self,
                        )
                        self._all_devices[key] = device_to_update
                    device_to_update.update_room(room, thermostat)
        return self._all_devices

    def set_override(self, room_id, temperature, minutes, location_id):
        """Hold one room at ``temperature`` °C for ``minutes``."""
        variables = {
            "lid": int(location_id),
            "rid": int(room_id),
            "temperature": _warmup_temperature(temperature),
            "minutes": int(min(1440, max(0, minutes))),
        }
        if not self._mutation(GQL_DEVICE_OVERRIDE, variables):
            _LOGGER.error(
                "Setting override for room %s to %s°C for %s minutes failed",
                room_id,
                temperature,
                minutes,
            )
            return False
        _LOGGER.info(
            "Set room %s to %s°C for %s minutes", room_id, temperature, minutes
        )
        return True

    def set_room_off(self, location_id, room_id):
        """Turn off one room. Other rooms at the location stay as they are."""
        if not self._mutation(
            GQL_DEVICE_OFF_ROOM, {"lid": int(location_id), "rid": int(room_id)}
        ):
            _LOGGER.error("Turning off room %s failed", room_id)
            return False
        _LOGGER.info("Turned off room %s", room_id)
        return True

    def set_location_to_off(self, location_id):
        """Turn off every room at a location."""
        if not self._mutation(GQL_DEVICE_OFF_LOCATION, {"lid": int(location_id)}):
            _LOGGER.error("Turning off location %s failed", location_id)
            return False
        _LOGGER.info("Turned off location %s", location_id)
        return True

    def set_room_to_frost(self, location_id, room_id):
        """Put one room into frost protection."""
        if not self._mutation(
            GQL_DEVICE_FROST_ROOM, {"lid": int(location_id), "rid": int(room_id)}
        ):
            _LOGGER.error("Setting room %s to frost failed", room_id)
            return False
        _LOGGER.info("Set room %s to frost", room_id)
        return True

    def set_location_to_frost(self, location_id):
        """Put every room at a location into frost protection."""
        if not self._mutation(GQL_DEVICE_FROST_LOCATION, {"lid": int(location_id)}):
            _LOGGER.error("Setting location %s to frost failed", location_id)
            return False
        _LOGGER.info("Set location %s to frost", location_id)
        return True

    def set_temperature_to_auto(self, room_id, location_id):
        """Resume the programmed schedule for one room."""
        if not self._mutation(
            GQL_DEVICE_PROGRAM_ROOM, {"lid": int(location_id), "rid": int(room_id)}
        ):
            _LOGGER.error("Setting room %s to schedule failed", room_id)
            return False
        _LOGGER.info("Set room %s to schedule", room_id)
        return True

    def set_location_to_auto(self, location_id):
        """Resume the programmed schedule for every room at a location."""
        if not self._mutation(GQL_DEVICE_PROGRAM_LOCATION, {"lid": int(location_id)}):
            _LOGGER.error("Setting location %s to schedule failed", location_id)
            return False
        _LOGGER.info("Set location %s to schedule", location_id)
        return True

    def set_temperature_to_manual(self, room_id, location_id):
        """Put one room into fixed mode without changing its fixed temperature."""
        if not self._mutation(
            GQL_DEVICE_FIXED_MODE, {"lid": int(location_id), "rid": int(room_id)}
        ):
            _LOGGER.error("Setting room %s to fixed mode failed", room_id)
            return False
        _LOGGER.info("Set room %s to fixed mode", room_id)
        return True

    def set_new_temperature(self, room_id, new_temperature, location_id):
        """Set one room to a fixed temperature. Return that temperature on success."""
        if not self._mutation(
            GQL_DEVICE_FIXED,
            {
                "lid": int(location_id),
                "rid": int(room_id),
                "temperature": _warmup_temperature(new_temperature),
            },
        ):
            _LOGGER.error(
                "Setting room %s to %s°C failed", room_id, new_temperature
            )
            return None
        _LOGGER.info("Set room %s to %s°C", room_id, new_temperature)
        return new_temperature

    def set_holiday(self, location_id, temperature, days):
        """Put a whole location into holiday mode for ``days`` days."""
        if not self._mutation(
            GQL_DEVICE_HOLIDAY,
            {
                "lid": int(location_id),
                "temperature": _warmup_temperature(temperature),
                "days": int(days),
            },
        ):
            _LOGGER.error("Setting holiday on location %s failed", location_id)
            return False
        _LOGGER.info(
            "Set location %s to holiday at %s°C for %s days",
            location_id,
            temperature,
            days,
        )
        return True

    def cancel_holiday(self, location_id):
        """Cancel holiday mode for a location."""
        if not self._mutation(GQL_CANCEL_HOLIDAY, {"lid": int(location_id)}):
            _LOGGER.error("Cancelling holiday on location %s failed", location_id)
            return False
        _LOGGER.info("Cancelled holiday on location %s", location_id)
        return True


class Warmup4IEDevice:
    """Representation of a warmup4ie device."""

    # pylint: disable-msg=too-many-arguments
    def __init__(
        self,
        location_id,
        location_name,
        room_id,
        room_name,
        device_id,
        serial_number,
        warmup4ie_account,
    ):
        """Initialize the climate device."""
        self._location_name = location_name
        self._loc_id = location_id
        self._room_id = room_id
        self._room_name = room_name
        self._device_id = device_id
        self._serial_number = serial_number
        self._warmup4ie_account = warmup4ie_account

        self.target_temperature = 0
        self.current_temperature = 0
        self.min_temp = 5
        self.max_temp = 30
        self.floor_temperature = None
        self.floor_temperature_2 = None
        self.air_temperature = None
        self.away_temperature = 0
        self.comfort_temperature = 0
        self.cost = None
        self.energy = None
        self.energy_total = None
        self.fixed_temperature = 0
        self.override_temperature = 0
        self.override_duration_mins = 0
        self.sleep_temperature = 0
        self.run_mode = None
        self.room_mode = None
        self.heating_target = None
        self.output_status = None

    def get_run_mode(self):
        """return current mode, e.g. 'off', 'fixed', 'prog'."""
        return self.run_mode

    def update_room(self, room, thermostat):
        """Update room/device data for the given room name."""
        self.target_temperature = _tenths(room.get("targetTemp")) or 0
        self.current_temperature = _tenths(room.get("currentTemp")) or 0
        minimum = _tenths(thermostat.get("minTemp"))
        maximum = _tenths(thermostat.get("maxTemp"))
        self.min_temp = 5 if minimum is None else minimum
        self.max_temp = 30 if maximum is None else maximum
        self.floor_temperature = _tenths(thermostat.get("floor1Temp"), probe=True)
        self.floor_temperature_2 = _tenths(thermostat.get("floor2Temp"), probe=True)
        self.air_temperature = _tenths(thermostat.get("airTemp"), probe=True)
        self.away_temperature = _tenths(room.get("awayTemp")) or 0
        self.comfort_temperature = _tenths(room.get("comfortTemp")) or 0
        self.cost = _parse_number(room.get("cost"))
        self.energy = _parse_number(room.get("energy"))
        total = room.get("total")
        self.energy_total = int(total) if total not in (None, "") else None
        self.fixed_temperature = _tenths(room.get("fixedTemp")) or 0
        self.override_temperature = _tenths(room.get("overrideTemp")) or 0
        duration = room.get("overrideDur")
        self.override_duration_mins = int(duration or 0)
        self.sleep_temperature = _tenths(room.get("sleepTemp")) or 0
        self.run_mode = normalize_run_mode(room.get("runMode"), room.get("runModeInt"))
        self.room_mode = normalize_room_mode(room.get("roomMode"), room.get("roomModeInt"))
        heating = thermostat.get("heatingTarget")
        if isinstance(heating, str) and heating.strip():
            self.heating_target = HEATING_TARGET_FROM_API.get(
                heating.strip(), heating.strip()
            )
        else:
            target_int = thermostat.get("heatingTargetInt")
            try:
                self.heating_target = HEATING_TARGET.get(int(target_int))
            except (TypeError, ValueError):
                self.heating_target = None
        parameters = thermostat.get("parameters") or {}
        if "outputStatus" in parameters and parameters.get("outputStatus") is not None:
            self.output_status = int(parameters["outputStatus"])
        else:
            self.output_status = None

    def get_target_temperature(self):
        """return target temperature"""
        return self.target_temperature

    def get_current_temperature(self):
        """return current temperature"""
        return self.current_temperature

    def get_floor_temperature(self):
        """return floor temperature"""
        return self.floor_temperature

    def get_floor_temperature_2(self):
        """return floor second temperature"""
        return self.floor_temperature_2

    def get_air_temperature(self):
        """return air temperature"""
        return self.air_temperature

    def get_away_temperature(self):
        """return away temperature"""
        return self.away_temperature

    def get_comfort_temperature(self):
        """return comfort temperature"""
        return self.comfort_temperature

    def get_fixed_temperature(self):
        """return fixed temperature"""
        return self.fixed_temperature

    def get_override_temperature(self):
        """return override temperature"""
        return self.override_temperature

    def get_sleep_temperature(self):
        """return sleep temperature"""
        return self.sleep_temperature

    def get_energy(self):
        """return today's energy usage"""
        return self.energy

    def get_cost(self):
        """return today's energy cost"""
        return self.cost

    def get_override_duration_mins(self):
        """return the number of minutes the current override is set for"""
        return self.override_duration_mins

    def get_target_temperature_low(self):
        """return minimum temperature"""
        return self.min_temp

    def get_target_temperature_high(self):
        """return maximum temperature"""
        return self.max_temp

    def get_room_mode(self):
        return self.room_mode

    def get_heating_target(self):
        return self.heating_target

    def get_location_name(self):
        return self._location_name

    def get_location_id(self):
        return self._loc_id

    def get_room_name(self):
        return self._room_name

    def get_room_id(self):
        return self._room_id

    def get_serial_number(self):
        return self._serial_number

    def get_device_id(self):
        return self._device_id

    def set_new_temperature(self, new_temperature):
        """set new target temperature"""
        returned_temp = self._warmup4ie_account.set_new_temperature(
            self._room_id, new_temperature, self._loc_id
        )
        if returned_temp is None:
            _LOGGER.info(
                "Server declined to set new target temperature to %.1f°C",
                new_temperature,
            )
            return
        self.target_temperature = returned_temp
        _LOGGER.info(
            "Successfully set new target temperature to %.1f°C",
            self.target_temperature,
        )

    def set_temperature_to_auto(self):
        """set device to automatic mode"""
        return self._warmup4ie_account.set_temperature_to_auto(
            self._room_id, self._loc_id
        )

    def set_temperature_to_manual(self):
        """set device to manual mode"""
        return self._warmup4ie_account.set_temperature_to_manual(
            self._room_id, self._loc_id
        )

    def set_room_to_frost(self):
        """set this room to frost protection without changing the others"""
        return self._warmup4ie_account.set_room_to_frost(self._loc_id, self._room_id)

    def set_location_to_frost(self):
        """set every room at this location to frost protection"""
        return self._warmup4ie_account.set_location_to_frost(self._loc_id)

    def set_room_off(self):
        """turn off this room without turning off the rest of the location"""
        return self._warmup4ie_account.set_room_off(self._loc_id, self._room_id)

    def set_location_to_off(self):
        """turn off every room at this location"""
        return self._warmup4ie_account.set_location_to_off(self._loc_id)

    def set_override(self, temperature, minutes):
        """Hold this room at ``temperature`` °C for ``minutes``."""
        ok = self._warmup4ie_account.set_override(
            self._room_id, temperature, minutes, self._loc_id
        )
        if ok:
            self.override_temperature = temperature
            self.override_duration_mins = minutes
            self.run_mode = "override"
        return ok
