"""Behavior tests for the Warmup API client and climate state rules."""

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "custom_components" / "warmup" / "warmup4ie" / "warmup4ie.py"

spec = importlib.util.spec_from_file_location("warmup4ie_mod", MODULE_PATH)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


def _response(payload, status_code=200):
    response = mock.Mock()
    response.status_code = status_code
    response.text = str(payload)
    response.json.return_value = payload
    return response


def test_login_failure_does_not_query_with_a_missing_token():
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return _response(
            {"status": {"result": "error"}, "response": {"errorCode": 101}}
        )

    with mock.patch.object(api.requests, "post", side_effect=post):
        client = api.Warmup4IE("user@example.com", "wrong")

    assert client.setup_finished is False
    assert len(calls) == 1
    assert calls[0]["request"]["method"] == "userLogin"


def test_room_off_targets_only_that_room():
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append((url, json))
        if json and json.get("request", {}).get("method") == "userLogin":
            return _response(
                {"status": {"result": "success"}, "response": {"token": "TOK"}}
            )
        return _response({"status": "success", "data": {"user": {"allLocations": []}}})

    with mock.patch.object(api.requests, "post", side_effect=post):
        client = api.Warmup4IE("user@example.com", "secret")
        calls.clear()
        assert client.set_room_off(10, 20) is True
    url, body = calls[-1]
    assert url == api.Warmup4IE.URL
    assert "deviceOff" in body["query"]
    assert body["variables"] == {"lid": 10, "rid": 20}


def test_location_off_omits_the_room_id():
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        if json and json.get("request", {}).get("method") == "userLogin":
            return _response(
                {"status": {"result": "success"}, "response": {"token": "TOK"}}
            )
        return _response({"status": "success", "data": {"user": {"allLocations": []}}})

    with mock.patch.object(api.requests, "post", side_effect=post):
        client = api.Warmup4IE("user@example.com", "secret")
        calls.clear()
        assert client.set_location_to_off(10) is True
    body = calls[-1]
    assert "deviceOff" in body["query"]
    assert body["variables"] == {"lid": 10}
    assert "locMode" not in str(body)


def test_fixed_temperature_is_kept_when_the_server_accepts_it():
    def post(url, headers=None, json=None, timeout=None):
        if json and json.get("request", {}).get("method") == "userLogin":
            return _response(
                {"status": {"result": "success"}, "response": {"token": "TOK"}}
            )
        if json and "deviceFixed" in json.get("query", ""):
            return _response({"data": {"deviceFixed": True}})
        return _response({"status": "success", "data": {"user": {"allLocations": []}}})

    with mock.patch.object(api.requests, "post", side_effect=post):
        client = api.Warmup4IE("user@example.com", "secret")
        device = api.Warmup4IEDevice(10, "Home", 20, "Bath", 30, "SN1", client)
        device.set_new_temperature(21.5)
    assert device.target_temperature == 21.5


def test_override_uses_duration_minutes():
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        if json and json.get("request", {}).get("method") == "userLogin":
            return _response(
                {"status": {"result": "success"}, "response": {"token": "TOK"}}
            )
        return _response({"data": {"deviceOverride": True}})

    with mock.patch.object(api.requests, "post", side_effect=post):
        client = api.Warmup4IE("user@example.com", "secret")
        calls.clear()
        assert client.set_override(20, 18, 90, location_id=10) is True
    body = calls[-1]
    assert "deviceOverride" in body["query"]
    assert body["variables"]["minutes"] == 90
    assert body["variables"]["temperature"] == 180
    assert body["variables"]["rid"] == 20


def test_update_reads_schedule_relay_energy_and_ignores_missing_probe():
    device = api.Warmup4IEDevice(1, "Home", 2, "Kitchen", 3, "SN", None)
    device.update_room(
        {
            "targetTemp": 210,
            "currentTemp": 200,
            "awayTemp": 160,
            "comfortTemp": 220,
            "cost": "0.40",
            "energy": "1.50",
            "total": 2500,
            "fixedTemp": 180,
            "overrideTemp": 0,
            "overrideDur": 0,
            "roomMode": "program",
            "sleepTemp": 180,
            "runMode": "schedule",
        },
        {
            "minTemp": "50",
            "maxTemp": "300",
            "airTemp": "215",
            "floor1Temp": "900",
            "floor2Temp": None,
            "heatingTargetInt": 0,
            "parameters": {"outputStatus": 0},
        },
    )
    assert device.run_mode == "prog"
    assert device.output_status == 0
    assert device.air_temperature == 21.5
    assert device.floor_temperature is None
    assert device.energy == 1.5
    assert device.cost == 0.4
    assert device.energy_total == 2500
    assert device.get_cost() == 0.4


def test_request_errors_are_reported_without_raising():
    def post(url, headers=None, json=None, timeout=None):
        if json and json.get("request", {}).get("method") == "userLogin":
            return _response(
                {"status": {"result": "success"}, "response": {"token": "TOK"}}
            )
        raise requests.Timeout("timed out")

    with mock.patch.object(api.requests, "post", side_effect=post):
        client = api.Warmup4IE("user@example.com", "secret")
        assert client.update_all_devices() is None


def test_expired_token_is_refreshed_once():
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        if json and json.get("request", {}).get("method") == "userLogin":
            return _response(
                {
                    "status": {"result": "success"},
                    "response": {"token": "TOK-%s" % len(calls)},
                }
            )
        if json and "deviceOff" in json.get("query", ""):
            if headers.get("warmup-authorization") == "TOK-1":
                return _response(
                    {"errors": [{"message": "unauthorized token"}]}, status_code=200
                )
            return _response({"data": {"deviceOff": True}})
        return _response({"status": "success", "data": {"user": {"allLocations": []}}})

    with mock.patch.object(api.requests, "post", side_effect=post):
        client = api.Warmup4IE("user@example.com", "secret")
        assert client.set_room_off(1, 2) is True
        assert client._warmup_access_token != "TOK-1"


def test_integer_run_mode_still_maps_when_the_api_omits_the_name():
    device = api.Warmup4IEDevice(1, "Home", 2, "Kitchen", 3, "SN", None)
    device.update_room(
        {
            "targetTemp": 200,
            "currentTemp": 190,
            "awayTemp": 160,
            "comfortTemp": 220,
            "cost": "0",
            "energy": "0",
            "fixedTemp": 200,
            "overrideTemp": 0,
            "overrideDur": 0,
            "sleepTemp": 180,
            "runModeInt": 3,
            "roomModeInt": 3,
        },
        {
            "minTemp": 50,
            "maxTemp": 300,
            "airTemp": 190,
            "floor1Temp": 210,
            "floor2Temp": 0,
            "heatingTargetInt": 1,
        },
    )
    assert device.run_mode == "fixed"
    assert device.room_mode == "fixed"
    assert device.floor_temperature == 21.0
    assert device.floor_temperature_2 == 0
    assert device.heating_target == "air"


def test_hvac_action_uses_the_relay_before_the_temperature_guess():
    assert api.infer_hvac_action(False, 1, 10, 20) == "off"
    assert api.infer_hvac_action(True, 0, 10, 20) == "idle"
    assert api.infer_hvac_action(True, 1, 22, 20) == "heating"
    assert api.infer_hvac_action(True, None, 19, 20) == "heating"
    assert api.infer_hvac_action(True, None, 20, 20) == "idle"
    assert api.infer_hvac_action(True, None, 21, 20) == "idle"


def test_displayed_target_ignores_a_stale_poll_and_a_zero_from_off():
    now = datetime(2026, 10, 20, 12, 0, 0)
    displayed, pending = api.resolve_displayed_target(17, 18, now, now + timedelta(seconds=15), 17)
    assert displayed == 18
    assert pending == 18

    displayed, pending = api.resolve_displayed_target(18, 18, now, now + timedelta(seconds=15), 17)
    assert displayed == 18
    assert pending is None

    displayed, pending = api.resolve_displayed_target(0, None, now, None, 21)
    assert displayed == 21
    assert pending is None


def test_until_clock_time_becomes_a_duration():
    now = datetime(2026, 10, 20, 12, 0, 0)
    assert api.minutes_until("14:00", now) == 120
    assert api.minutes_until("11:00", now) == 23 * 60


def test_location_filter_matches_names_without_case():
    assert api.location_selected("Home", None) is True
    assert api.location_selected("Home", "home") is True
    assert api.location_selected("Home", "Office") is False


def test_fahrenheit_service_values_are_converted_to_celsius():
    assert api.to_celsius(70, "°F") == 21.11111111111111
    assert api.to_celsius(21.5, "°C") == 21.5


def _room(room_id, name, serial):
    return {
        "id": room_id,
        "roomName": name,
        "targetTemp": 200,
        "currentTemp": 190,
        "awayTemp": 160,
        "comfortTemp": 220,
        "cost": "0.1",
        "energy": "1",
        "fixedTemp": 200,
        "overrideTemp": 0,
        "overrideDur": 0,
        "sleepTemp": 180,
        "runMode": "fixed",
        "thermostat4ies": [
            {
                "id": room_id,
                "deviceSN": serial,
                "minTemp": 50,
                "maxTemp": 300,
                "airTemp": 190,
                "floor1Temp": 210,
                "floor2Temp": None,
                "heatingTargetInt": 0,
                "appFw": "1.2.3",
            }
        ],
    }


def test_a_bad_room_does_not_drop_the_rest_of_the_account():
    payload = {
        "status": "success",
        "data": {
            "user": {
                "allLocations": [
                    {
                        "id": 10,
                        "name": "Home",
                        "rooms": [
                            _room(1, "Kitchen", "SN-KITCHEN"),
                            {"roomName": "Broken"},
                        ],
                    }
                ]
            }
        },
    }

    def post(url, headers=None, json=None, timeout=None):
        if json and json.get("request", {}).get("method") == "userLogin":
            return _response(
                {"status": {"result": "success"}, "response": {"token": "TOK"}}
            )
        return _response(payload)

    with mock.patch.object(api.requests, "post", side_effect=post):
        client = api.Warmup4IE("user@example.com", "secret")

    assert client.setup_finished is True
    assert client.last_error is None
    devices = list(client.get_all_devices().values())
    assert len(devices) == 1
    assert devices[0].get_serial_number() == "SN-KITCHEN"
    assert devices[0].firmware == "1.2.3"
    assert api.device_identity(devices[0]) == "SN-KITCHEN"


def test_a_dropped_connection_is_retried_once():
    graphql_calls = {"n": 0}

    def post(url, headers=None, json=None, timeout=None):
        if json and json.get("request", {}).get("method") == "userLogin":
            return _response(
                {"status": {"result": "success"}, "response": {"token": "TOK"}}
            )
        graphql_calls["n"] += 1
        if graphql_calls["n"] == 1:
            raise requests.Timeout("timed out")
        return _response(
            {
                "status": "success",
                "data": {
                    "user": {
                        "allLocations": [
                            {"id": 10, "name": "Home", "rooms": [_room(1, "Bath", "SN1")]}
                        ]
                    }
                },
            }
        )

    with mock.patch.object(api.requests, "post", side_effect=post):
        client = api.Warmup4IE("user@example.com", "secret")

    assert graphql_calls["n"] == 2
    assert client.setup_finished is True
    assert client.last_error is None


def test_an_empty_follow_up_poll_keeps_the_previous_devices():
    responses = {"n": 0}

    def post(url, headers=None, json=None, timeout=None):
        if json and json.get("request", {}).get("method") == "userLogin":
            return _response(
                {"status": {"result": "success"}, "response": {"token": "TOK"}}
            )
        responses["n"] += 1
        rooms = [_room(1, "Bath", "SN1")] if responses["n"] == 1 else []
        return _response(
            {
                "status": "success",
                "data": {"user": {"allLocations": [{"id": 10, "name": "Home", "rooms": rooms}]}},
            }
        )

    with mock.patch.object(api.requests, "post", side_effect=post):
        client = api.Warmup4IE("user@example.com", "secret")
        assert len(client.get_all_devices()) == 1
        # The next payload has a location but no rooms, which is a real removal.
        client.update_all_devices()
        assert client.get_all_devices() == {}
        client._all_devices["stale"] = object()
        # A payload with no locations at all is treated as a glitch.
        responses["n"] = 99

        def empty_locations(url, headers=None, json=None, timeout=None):
            if json and json.get("request", {}).get("method") == "userLogin":
                return _response(
                    {"status": {"result": "success"}, "response": {"token": "TOK"}}
                )
            return _response(
                {"status": "success", "data": {"user": {"allLocations": []}}}
            )

        api.requests.post.side_effect = empty_locations
        assert "stale" in client.update_all_devices()


def test_invalid_password_is_an_auth_error():
    def post(url, headers=None, json=None, timeout=None):
        return _response(
            {"status": {"result": "error"}, "response": {"errorCode": 101}}
        )

    with mock.patch.object(api.requests, "post", side_effect=post):
        client = api.Warmup4IE("user@example.com", "wrong")

    assert client.setup_finished is False
    assert isinstance(client.last_error, api.WarmupAuthError)
    assert client.last_error.code == 101
