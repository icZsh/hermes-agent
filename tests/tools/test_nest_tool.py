import importlib.util
import json
import stat
from pathlib import Path

import pytest

from toolsets import resolve_toolset
from tools.nest_tool import (
    _check_nest_available,
    _extract_status,
    _fahrenheit_to_celsius,
    _celsius_to_fahrenheit,
    _get_thermostat_device,
    _handle_get_status,
    _handle_set_mode,
    _handle_set_temperature,
    _resolve_project_id,
    _save_shared_env,
    _set_temperature,
)
from tools.registry import registry
from tools import nest_token_manager


SAMPLE_DEVICE = {
    "name": "enterprises/proj/devices/device-1",
    "traits": {
        "sdm.devices.traits.Info": {"customName": "勺儿"},
        "sdm.devices.traits.Temperature": {"ambientTemperatureCelsius": 23.0},
        "sdm.devices.traits.Humidity": {"ambientHumidityPercent": 44},
        "sdm.devices.traits.ThermostatMode": {"mode": "HEAT"},
        "sdm.devices.traits.ThermostatHvac": {"status": "HEATING"},
        "sdm.devices.traits.ThermostatTemperatureSetpoint": {"heatCelsius": 23.9},
        "sdm.devices.traits.Connectivity": {"status": "ONLINE"},
    },
}


class TestHelpers:
    def test_resolve_project_id_prefers_nest_project_id(self):
        cfg = {"NEST_PROJECT_ID": "root-project", "NEST_SDM_PROJECT_ID": "fallback-project"}
        assert _resolve_project_id(cfg) == "root-project"

    def test_resolve_project_id_falls_back_to_sdm_project_id(self):
        cfg = {"NEST_SDM_PROJECT_ID": "fallback-project"}
        assert _resolve_project_id(cfg) == "fallback-project"

    def test_resolve_project_id_raises_when_missing(self):
        with pytest.raises(ValueError):
            _resolve_project_id({})

    def test_fahrenheit_to_celsius(self):
        assert _fahrenheit_to_celsius(75) == 23.9

    def test_celsius_to_fahrenheit(self):
        assert _celsius_to_fahrenheit(23.0) == 73.4
        assert _celsius_to_fahrenheit(None) is None

    def test_extract_status(self):
        result = _extract_status(SAMPLE_DEVICE, 0)
        assert result["device_index"] == 0
        assert result["device_name"] == "enterprises/proj/devices/device-1"
        assert result["custom_name"] == "勺儿"
        assert result["mode"] == "HEAT"
        assert result["ambient_temp_f"] == 73.4
        assert result["heat_setpoint_f"] == 75.0
        assert result["humidity_percent"] == 44

    def test_get_thermostat_device_filters_and_indexes(self):
        devices = [
            {"name": "enterprises/x/devices/sensor-1", "traits": {}},
            SAMPLE_DEVICE,
        ]
        result = _get_thermostat_device(devices, 0)
        assert result["name"] == SAMPLE_DEVICE["name"]

    def test_get_thermostat_device_invalid_index(self):
        with pytest.raises(ValueError):
            _get_thermostat_device([SAMPLE_DEVICE], 3)


class TestAvailability:
    def test_check_nest_available_true(self, monkeypatch):
        monkeypatch.setattr(
            "tools.nest_tool._load_config",
            lambda: {
                "NEST_PROJECT_ID": "proj",
                "NEST_CLIENT_ID": "client",
                "NEST_CLIENT_SECRET": "secret",
                "NEST_REFRESH_TOKEN": "refresh",
            },
        )
        assert _check_nest_available() is True

    def test_check_nest_available_false(self, monkeypatch):
        monkeypatch.setattr("tools.nest_tool._load_config", lambda: {"NEST_CLIENT_ID": "client"})
        assert _check_nest_available() is False


class TestHandlers:
    def test_handle_get_status_success(self, monkeypatch):
        monkeypatch.setattr("tools.nest_tool._get_status", lambda idx=0: {"device_index": idx, "mode": "HEAT"})
        result = json.loads(_handle_get_status({"device_index": 2}))
        assert result["result"]["device_index"] == 2
        assert result["result"]["mode"] == "HEAT"

    def test_handle_set_mode_missing_mode(self):
        result = json.loads(_handle_set_mode({}))
        assert "error" in result
        assert "mode" in result["error"]

    def test_handle_set_mode_success(self, monkeypatch):
        monkeypatch.setattr(
            "tools.nest_tool._set_mode",
            lambda mode, idx=0: {"ok": True, "mode": mode.upper(), "device_index": idx},
        )
        result = json.loads(_handle_set_mode({"mode": "cool", "device_index": 1}))
        assert result["result"]["mode"] == "COOL"
        assert result["result"]["device_index"] == 1

    def test_handle_set_temperature_missing_temperature(self):
        result = json.loads(_handle_set_temperature({}))
        assert "error" in result
        assert "temperature_f" in result["error"]

    def test_handle_set_temperature_success(self, monkeypatch):
        monkeypatch.setattr(
            "tools.nest_tool._set_temperature",
            lambda temp_f, idx=0: {"ok": True, "temperature_f": temp_f, "device_index": idx},
        )
        result = json.loads(_handle_set_temperature({"temperature_f": 72, "device_index": 1}))
        assert result["result"]["temperature_f"] == 72.0
        assert result["result"]["device_index"] == 1


class TestBehavior:
    def test_set_temperature_uses_cool_command_when_mode_is_cool(self, monkeypatch):
        monkeypatch.setattr(
            "tools.nest_tool._load_config",
            lambda: {
                "NEST_PROJECT_ID": "proj",
                "NEST_CLIENT_ID": "client",
                "NEST_CLIENT_SECRET": "secret",
                "NEST_REFRESH_TOKEN": "refresh",
                "NEST_ACCESS_TOKEN": "access",
            },
        )
        monkeypatch.setattr("tools.nest_tool._list_devices", lambda cfg: [{"name": SAMPLE_DEVICE["name"], "traits": {"sdm.devices.traits.ThermostatMode": {}}}])

        calls = []

        def fake_request(cfg, method, path, body=None):
            calls.append((method, path, body))
            if method == "GET":
                return {
                    "name": SAMPLE_DEVICE["name"],
                    "traits": {
                        "sdm.devices.traits.ThermostatMode": {"mode": "COOL"},
                    },
                }
            return {}

        monkeypatch.setattr("tools.nest_tool._request", fake_request)
        result = _set_temperature(70, 0)
        assert result["command"] == "sdm.devices.commands.ThermostatTemperatureSetpoint.SetCool"
        assert result["mode_used"] == "COOL"
        assert calls[-1][2]["params"] == {"coolCelsius": 21.1}

    @pytest.mark.parametrize(
        ("mode_payload", "match"),
        [
            ({"mode": "HEATCOOL"}, "HEATCOOL"),
            ({"mode": "OFF"}, "OFF"),
            ({"mode": "UNKNOWN"}, "UNKNOWN"),
            ({}, "UNKNOWN"),
        ],
    )
    def test_set_temperature_refuses_non_heat_cool_single_setpoint(self, monkeypatch, mode_payload, match):
        monkeypatch.setattr(
            "tools.nest_tool._load_config",
            lambda: {
                "NEST_PROJECT_ID": "proj",
                "NEST_CLIENT_ID": "client",
                "NEST_CLIENT_SECRET": "secret",
                "NEST_REFRESH_TOKEN": "refresh",
                "NEST_ACCESS_TOKEN": "access",
            },
        )
        monkeypatch.setattr("tools.nest_tool._list_devices", lambda cfg: [{"name": SAMPLE_DEVICE["name"], "traits": {"sdm.devices.traits.ThermostatMode": {}}}])

        calls = []

        def fake_request(cfg, method, path, body=None):
            calls.append((method, path, body))
            if method == "GET":
                return {
                    "name": SAMPLE_DEVICE["name"],
                    "traits": {
                        "sdm.devices.traits.ThermostatMode": mode_payload,
                    },
                }
            return {}

        monkeypatch.setattr("tools.nest_tool._request", fake_request)

        with pytest.raises(ValueError, match=match):
            _set_temperature(70, 0)

        assert len(calls) == 1

    def test_save_shared_env_is_owner_only(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        monkeypatch.setattr("tools.nest_tool._shared_env_path", lambda: env_path)

        _save_shared_env({"NEST_ACCESS_TOKEN": "new-access", "NEST_REFRESH_TOKEN": "new-refresh"})

        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
        assert "NEST_ACCESS_TOKEN=new-access" in env_path.read_text()

    def test_token_cache_is_owner_only(self, tmp_path, monkeypatch):
        token_file = tmp_path / "nest_token_cache.json"
        monkeypatch.setattr(nest_token_manager, "TOKEN_FILE", str(token_file))

        nest_token_manager.save_token_cache({"access_token": "sentinel"})

        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    def test_vendored_token_cache_is_owner_only(self, tmp_path, monkeypatch):
        module_path = Path(__file__).parents[2] / "skills" / "nest-thermostat" / "scripts" / "nest_token_manager.py"
        spec = importlib.util.spec_from_file_location("vendored_nest_token_manager", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        token_file = tmp_path / "vendored_nest_token_cache.json"
        monkeypatch.setattr(module, "TOKEN_FILE", str(token_file))

        module.save_token_cache({"access_token": "sentinel"})

        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


class TestRegistration:
    def test_tools_registered(self):
        names = registry.get_all_tool_names()
        assert "nest_get_status" in names
        assert "nest_set_mode" in names
        assert "nest_set_temperature" in names

    def test_tools_in_nest_toolset(self):
        toolset_map = registry.get_tool_to_toolset_map()
        for tool in ("nest_get_status", "nest_set_mode", "nest_set_temperature"):
            assert toolset_map[tool] == "nest"

    def test_check_fn_gates_availability(self, monkeypatch):
        monkeypatch.setattr("tools.nest_tool._load_config", lambda: {})
        defs = registry.get_definitions({"nest_get_status", "nest_set_mode", "nest_set_temperature"})
        assert defs == []

    def test_check_fn_includes_when_credentials_present(self, monkeypatch):
        monkeypatch.setattr(
            "tools.nest_tool._load_config",
            lambda: {
                "NEST_PROJECT_ID": "proj",
                "NEST_CLIENT_ID": "client",
                "NEST_CLIENT_SECRET": "secret",
                "NEST_REFRESH_TOKEN": "refresh",
            },
        )
        defs = registry.get_definitions({"nest_get_status", "nest_set_mode", "nest_set_temperature"})
        assert len(defs) == 3

    def test_nest_toolset_resolves(self):
        tools = resolve_toolset("nest")
        assert set(tools) == {"nest_get_status", "nest_set_mode", "nest_set_temperature"}
