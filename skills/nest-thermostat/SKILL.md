---
name: nest-thermostat
description: Control Google Nest thermostats via SDM API with a clean Python client and OAuth flow.
version: 1.0.0
license: MIT
metadata:
  hermes:
    tags: [smart-home, nest, thermostat, google-sdm]
prerequisites:
  commands: [python3.11]
  setup:
    - Google Cloud project with SDM API enabled
    - Device Access Console project ($5 one-time fee)
    - OAuth 2.0 Web credentials
---

# Nest Thermostat Control

Control Google Nest thermostats via the Smart Device Management (SDM) API.

## Setup

### 1. Google Cloud + Device Access (one-time)

1. Enable SDM API at console.cloud.google.com
2. Create OAuth 2.0 Web credentials (redirect URI: `http://localhost:8000/nest/callback`)
3. Create Device Access project at console.nest.google.com/device-access ($5 fee)
4. Link OAuth client ID to Device Access project

### 2. Install dependencies

```bash
pip install httpx
```

### 3. Configure

```bash
mkdir -p ~/.config/nest
touch ~/.config/nest/.env
```

Add to `~/.config/nest/.env`:

```
NEST_CLIENT_ID=your-client-id.apps.googleusercontent.com
NEST_CLIENT_SECRET=your-client-secret
NEST_SDM_PROJECT_ID=your-device-access-project-id
```

### 4. Authorize (one-time, browser required)

```bash
python3 ~/.hermes/skills/nest-thermostat/nest_google_sdm.py oauth
```

Prints a URL to open in your browser. Callback is handled automatically on the same port. Tokens are saved to `~/.config/nest/.env`.

---

## Usage

```bash
# List devices
python3 ~/.hermes/skills/nest-thermostat/nest_google_sdm.py devices

# View thermostat status
python3 ~/.hermes/skills/nest-thermostat/nest_google_sdm.py status
python3 ~/.hermes/skills/nest-thermostat/nest_google_sdm.py status 0    # specific thermostat

# Set temperature (auto-detects mode)
python3 ~/.hermes/skills/nest-thermostat/nest_google_sdm.py set-temp 72
python3 ~/.hermes/skills/nest-thermostat/nest_google_sdm.py set-temp 68 1

# Change mode
python3 ~/.hermes/skills/nest-thermostat/nest_google_sdm.py set-mode HEAT
python3 ~/.hermes/skills/nest-thermostat/nest_google_sdm.py set-mode COOL 0

# Eco mode
python3 ~/.hermes/skills/nest-thermostat/nest_google_sdm.py set-eco on
python3 ~/.hermes/skills/nest-thermostat/nest_google_sdm.py set-eco off 0
```

---

## Critical Pitfalls

1. **Auth code is single-use**: When the OAuth callback provides a `code=`, immediately exchange + save in one atomic operation. Never call the token endpoint twice with the same code.
2. **Never print tokens**: Do not `grep`, echo, log, or paste token values. Let the CLI/tool read `.env` internally.
3. **Refresh token fails with `invalid_grant`**: Usually means the token was revoked. Re-run `oauth`.
4. **Hermes profile changes $HOME**: If running under a Hermes profile, `$HOME` may be redirected. Use absolute paths for credentials.

---

## Cron Usage

Prefer the registered Hermes Nest tools (`nest_get_status`, `nest_set_mode`, `nest_set_temperature`) or the CLI. Do **not** print `NEST_ACCESS_TOKEN`, `NEST_REFRESH_TOKEN`, or client secrets in terminal output/logs/context.

Safe scheduled-control pattern:

```text
1. Check status with `nest_get_status` or `python3 .../nest_google_sdm.py status`.
2. If needed, set mode explicitly with `nest_set_mode` / `set-mode`.
3. Set a single temperature only when mode is HEAT or COOL; use set-range for HEATCOOL.
```

Common API commands:

- **HEAT mode**: POST `.../devices/{id}:executeCommand`
  Body: `{"command": "sdm.devices.commands.ThermostatMode.SetMode", "params": {"mode": "HEAT"}}`

- **Set temperature**: POST `.../devices/{id}:executeCommand`
  Body: `{"command": "sdm.devices.commands.ThermostatTemperatureSetpoint.SetHeat", "params": {"heatCelsius": 23.89}}`

- **OFF**: `{"command": "sdm.devices.commands.ThermostatMode.SetMode", "params": {"mode": "OFF"}}`

---

## Architecture

```
nest_google_sdm.py  # Single-file CLI with OAuth flow and auto token refresh
```

Key features:
- Single self-contained file
- `httpx.AsyncClient` reused per-process
- Parallel device info fetching with `asyncio.gather`
- Lazy token initialization (works with `refresh_token` only on first run)
- Graceful null-safety for missing temperature/humidity
- No FastAPI/uvicorn dependency (plain `http.server` + threading)
