"""Google Nest thermostat control via the Smart Device Management (SDM) API.

Registers three LLM-callable tools:
- ``nest_get_status`` -- read thermostat status (mode, hvac state, temp, humidity)
- ``nest_set_mode`` -- set HVAC mode (HEAT, COOL, HEATCOOL, OFF)
- ``nest_set_temperature`` -- set a single Fahrenheit target on the active heat/cool side

Credentials are loaded from environment variables and Hermes .env files. To avoid
profile-isolation friction for Isaac's thermostat setup, this tool reads both the
current profile's ``.env`` and the shared Hermes root ``.env`` and writes token
refresh updates back to the shared root ``.env``.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_default_hermes_root, get_hermes_home
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
BASE_URL = "https://smartdevicemanagement.googleapis.com/v1"
_ALLOWED_MODES = frozenset({"HEAT", "COOL", "HEATCOOL", "OFF"})


def _shared_env_path() -> Path:
    return get_default_hermes_root() / ".env"


def _current_env_path() -> Path:
    return get_hermes_home() / ".env"


def _read_env_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def _load_config() -> Dict[str, str]:
    """Load config from env vars, current profile .env, and shared root .env.

    Precedence: process env > current profile .env > shared root .env.
    This preserves explicit overrides while still allowing shared Nest creds.
    """
    cfg = _read_env_file(_shared_env_path())
    current_path = _current_env_path()
    if current_path != _shared_env_path():
        cfg.update(_read_env_file(current_path))
    for key in (
        "NEST_PROJECT_ID",
        "NEST_SDM_PROJECT_ID",
        "NEST_CLIENT_ID",
        "NEST_CLIENT_SECRET",
        "NEST_REFRESH_TOKEN",
        "NEST_ACCESS_TOKEN",
    ):
        value = os.getenv(key, "").strip()
        if value:
            cfg[key] = value
    return cfg


def _write_owner_only(path: Path, content: str) -> None:
    """Atomically write a secret-bearing file with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _save_shared_env(updates: Dict[str, str]) -> None:
    path = _shared_env_path()
    existing_lines = path.read_text().splitlines() if path.exists() else []
    seen: set[str] = set()
    new_lines: list[str] = []

    for line in existing_lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            new_lines.append(line)
            continue
        key = raw.split("=", 1)[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")

    _write_owner_only(path, "\n".join(new_lines) + "\n")


def _resolve_project_id(cfg: Dict[str, str]) -> str:
    project_id = (cfg.get("NEST_PROJECT_ID") or cfg.get("NEST_SDM_PROJECT_ID") or "").strip()
    if not project_id:
        raise ValueError("Missing NEST_PROJECT_ID / NEST_SDM_PROJECT_ID in Hermes .env")
    return project_id


def _check_nest_available() -> bool:
    cfg = _load_config()
    try:
        project_ok = bool(_resolve_project_id(cfg))
    except ValueError:
        project_ok = False
    return bool(
        project_ok
        and cfg.get("NEST_CLIENT_ID")
        and cfg.get("NEST_CLIENT_SECRET")
        and cfg.get("NEST_REFRESH_TOKEN")
    )


def _fahrenheit_to_celsius(temp_f: float) -> float:
    return round((float(temp_f) - 32.0) * 5.0 / 9.0, 1)


def _celsius_to_fahrenheit(temp_c: Optional[float]) -> Optional[float]:
    if temp_c is None:
        return None
    return round(float(temp_c) * 9.0 / 5.0 + 32.0, 1)


def _redact_secret_values(text: str, cfg: Dict[str, str]) -> str:
    redacted = text
    for key in ("NEST_ACCESS_TOKEN", "NEST_REFRESH_TOKEN", "NEST_CLIENT_SECRET"):
        value = cfg.get(key)
        if value and len(value) >= 6:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _refresh_access_token(cfg: Dict[str, str]) -> Dict[str, str]:
    data = urllib.parse.urlencode(
        {
            "client_id": cfg["NEST_CLIENT_ID"],
            "client_secret": cfg["NEST_CLIENT_SECRET"],
            "refresh_token": cfg["NEST_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request(TOKEN_URL, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = _redact_secret_values(exc.read().decode(errors="replace"), cfg)
        raise RuntimeError(f"Nest token refresh failed: {body}") from exc

    cfg["NEST_ACCESS_TOKEN"] = payload["access_token"]
    if payload.get("refresh_token"):
        cfg["NEST_REFRESH_TOKEN"] = payload["refresh_token"]
    _save_shared_env(
        {
            "NEST_ACCESS_TOKEN": cfg["NEST_ACCESS_TOKEN"],
            "NEST_REFRESH_TOKEN": cfg["NEST_REFRESH_TOKEN"],
        }
    )
    return cfg


def _request(cfg: Dict[str, str], method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{BASE_URL}{path}"
    payload = None if body is None else json.dumps(body).encode()

    def _make_request(access_token: str):
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}

    if not cfg.get("NEST_ACCESS_TOKEN"):
        cfg = _refresh_access_token(cfg)

    try:
        return _make_request(cfg["NEST_ACCESS_TOKEN"])
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            body_text = _redact_secret_values(exc.read().decode(errors="replace"), cfg)
            raise RuntimeError(f"Nest API {method} {path} failed ({exc.code}): {body_text}") from exc

    cfg = _refresh_access_token(cfg)
    return _make_request(cfg["NEST_ACCESS_TOKEN"])


def _list_devices(cfg: Dict[str, str]) -> list[Dict[str, Any]]:
    project_id = _resolve_project_id(cfg)
    result = _request(cfg, "GET", f"/enterprises/{project_id}/devices")
    return result.get("devices", [])


def _get_thermostat_device(devices: list[Dict[str, Any]], device_index: int = 0) -> Dict[str, Any]:
    thermostats = [
        device for device in devices if "sdm.devices.traits.ThermostatMode" in device.get("traits", {})
    ]
    if not thermostats:
        raise ValueError("No Nest thermostat devices found")
    if device_index < 0 or device_index >= len(thermostats):
        raise ValueError(f"Invalid device_index {device_index}; found {len(thermostats)} thermostat(s)")
    return thermostats[device_index]


def _extract_status(device: Dict[str, Any], device_index: int) -> Dict[str, Any]:
    traits = device.get("traits", {})
    info = traits.get("sdm.devices.traits.Info", {})
    temp = traits.get("sdm.devices.traits.Temperature", {})
    humidity = traits.get("sdm.devices.traits.Humidity", {})
    mode = traits.get("sdm.devices.traits.ThermostatMode", {})
    hvac = traits.get("sdm.devices.traits.ThermostatHvac", {})
    setpoint = traits.get("sdm.devices.traits.ThermostatTemperatureSetpoint", {})
    connectivity = traits.get("sdm.devices.traits.Connectivity", {})

    return {
        "device_index": device_index,
        "device_name": device.get("name"),
        "custom_name": info.get("customName") or "",
        "mode": mode.get("mode"),
        "hvac_state": hvac.get("status"),
        "connectivity": connectivity.get("status"),
        "ambient_temp_f": _celsius_to_fahrenheit(temp.get("ambientTemperatureCelsius")),
        "humidity_percent": humidity.get("ambientHumidityPercent"),
        "heat_setpoint_f": _celsius_to_fahrenheit(setpoint.get("heatCelsius")),
        "cool_setpoint_f": _celsius_to_fahrenheit(setpoint.get("coolCelsius")),
    }


def _get_status(device_index: int = 0) -> Dict[str, Any]:
    cfg = _load_config()
    device = _get_thermostat_device(_list_devices(cfg), device_index)
    full = _request(cfg, "GET", f"/{device['name']}")
    return _extract_status(full, device_index)


def _execute_command(device_name: str, command: str, params: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _load_config()
    return _request(
        cfg,
        "POST",
        f"/{device_name}:executeCommand",
        body={"command": command, "params": params},
    )


def _set_mode(mode: str, device_index: int = 0) -> Dict[str, Any]:
    normalized = mode.upper().strip()
    if normalized not in _ALLOWED_MODES:
        raise ValueError(f"Invalid mode '{mode}'. Allowed: {', '.join(sorted(_ALLOWED_MODES))}")

    cfg = _load_config()
    device = _get_thermostat_device(_list_devices(cfg), device_index)
    result = _request(
        cfg,
        "POST",
        f"/{device['name']}:executeCommand",
        body={
            "command": "sdm.devices.commands.ThermostatMode.SetMode",
            "params": {"mode": normalized},
        },
    )
    return {
        "ok": True,
        "device_index": device_index,
        "device_name": device["name"],
        "mode": normalized,
        "result": result,
    }


def _set_temperature(temperature_f: float, device_index: int = 0) -> Dict[str, Any]:
    cfg = _load_config()
    device = _get_thermostat_device(_list_devices(cfg), device_index)
    full = _request(cfg, "GET", f"/{device['name']}")
    current_mode = full.get("traits", {}).get("sdm.devices.traits.ThermostatMode", {}).get("mode") or "UNKNOWN"
    temp_c = _fahrenheit_to_celsius(float(temperature_f))

    if current_mode == "COOL":
        command = "sdm.devices.commands.ThermostatTemperatureSetpoint.SetCool"
        params = {"coolCelsius": temp_c}
    elif current_mode == "HEAT":
        command = "sdm.devices.commands.ThermostatTemperatureSetpoint.SetHeat"
        params = {"heatCelsius": temp_c}
    elif current_mode == "HEATCOOL":
        raise ValueError("Thermostat is in HEATCOOL mode; set a heat/cool range or switch to HEAT/COOL before setting a single temperature")
    else:
        raise ValueError(f"Thermostat mode is {current_mode}; switch to HEAT or COOL before setting a single temperature")

    result = _request(cfg, "POST", f"/{device['name']}:executeCommand", body={"command": command, "params": params})
    return {
        "ok": True,
        "device_index": device_index,
        "device_name": device["name"],
        "temperature_f": float(temperature_f),
        "temperature_c": temp_c,
        "mode_used": current_mode,
        "command": command,
        "result": result,
    }


def _handle_get_status(args: dict, **kw) -> str:
    device_index = int(args.get("device_index", 0))
    try:
        return json.dumps({"result": _get_status(device_index)}, ensure_ascii=False)
    except Exception as exc:
        logger.error("nest_get_status error: %s", exc)
        return tool_error(f"Failed to get Nest thermostat status: {exc}")



def _handle_set_mode(args: dict, **kw) -> str:
    mode = str(args.get("mode", "")).strip()
    if not mode:
        return tool_error("Missing required parameter: mode")
    device_index = int(args.get("device_index", 0))
    try:
        return json.dumps({"result": _set_mode(mode, device_index)}, ensure_ascii=False)
    except Exception as exc:
        logger.error("nest_set_mode error: %s", exc)
        return tool_error(f"Failed to set Nest thermostat mode: {exc}")



def _handle_set_temperature(args: dict, **kw) -> str:
    if "temperature_f" not in args:
        return tool_error("Missing required parameter: temperature_f")
    device_index = int(args.get("device_index", 0))
    try:
        temperature_f = float(args["temperature_f"])
        return json.dumps({"result": _set_temperature(temperature_f, device_index)}, ensure_ascii=False)
    except Exception as exc:
        logger.error("nest_set_temperature error: %s", exc)
        return tool_error(f"Failed to set Nest thermostat temperature: {exc}")


NEST_GET_STATUS_SCHEMA = {
    "name": "nest_get_status",
    "description": "Get the current Nest thermostat status including mode, HVAC state, ambient temperature, humidity, and setpoints.",
    "parameters": {
        "type": "object",
        "properties": {
            "device_index": {
                "type": "integer",
                "description": "Optional thermostat index when multiple Nest thermostats exist. Defaults to 0.",
            }
        },
        "required": [],
    },
}

NEST_SET_MODE_SCHEMA = {
    "name": "nest_set_mode",
    "description": "Set the Nest thermostat HVAC mode to HEAT, COOL, HEATCOOL, or OFF.",
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "description": "Target HVAC mode: HEAT, COOL, HEATCOOL, or OFF.",
            },
            "device_index": {
                "type": "integer",
                "description": "Optional thermostat index when multiple Nest thermostats exist. Defaults to 0.",
            },
        },
        "required": ["mode"],
    },
}

NEST_SET_TEMPERATURE_SCHEMA = {
    "name": "nest_set_temperature",
    "description": "Set a single Nest thermostat target temperature in Fahrenheit. Only valid when the current mode is HEAT or COOL; HEATCOOL requires a range command and OFF/UNKNOWN is refused.",
    "parameters": {
        "type": "object",
        "properties": {
            "temperature_f": {
                "type": "number",
                "description": "Target temperature in degrees Fahrenheit.",
            },
            "device_index": {
                "type": "integer",
                "description": "Optional thermostat index when multiple Nest thermostats exist. Defaults to 0.",
            },
        },
        "required": ["temperature_f"],
    },
}


registry.register(
    name="nest_get_status",
    toolset="nest",
    schema=NEST_GET_STATUS_SCHEMA,
    handler=_handle_get_status,
    check_fn=_check_nest_available,
    requires_env=["NEST_PROJECT_ID", "NEST_CLIENT_ID", "NEST_CLIENT_SECRET", "NEST_REFRESH_TOKEN"],
    emoji="🌡️",
)

registry.register(
    name="nest_set_mode",
    toolset="nest",
    schema=NEST_SET_MODE_SCHEMA,
    handler=_handle_set_mode,
    check_fn=_check_nest_available,
    requires_env=["NEST_PROJECT_ID", "NEST_CLIENT_ID", "NEST_CLIENT_SECRET", "NEST_REFRESH_TOKEN"],
    emoji="🌡️",
)

registry.register(
    name="nest_set_temperature",
    toolset="nest",
    schema=NEST_SET_TEMPERATURE_SCHEMA,
    handler=_handle_set_temperature,
    check_fn=_check_nest_available,
    requires_env=["NEST_PROJECT_ID", "NEST_CLIENT_ID", "NEST_CLIENT_SECRET", "NEST_REFRESH_TOKEN"],
    emoji="🌡️",
)
