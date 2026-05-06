#!/usr/bin/env python3
"""
Google Nest SDM API — Single-file client + OAuth server + CLI.

Key improvements over upstream nest-sdm-control:
  • Single file, no duplicate _update_env_file
  • httpx.AsyncClient reused (not rebuilt per request)
  • Parallel device info fetching with asyncio.gather
  • Lazy token init — works with refresh_token only on first run
  • Graceful null handling for missing temperature/humidity
  • No FastAPI dependency (plain http.server + threading)
  • Config stored in ~/.config/nest/.env (not cwd)

Usage:
  # OAuth authorization (one-time, browser required)
  python3 nest_google_sdm.py oauth

  # CLI
  python3 nest_google_sdm.py devices
  python3 nest_google_sdm.py status [idx]
  python3 nest_google_sdm.py set-temp <F> [idx]
  python3 nest_google_sdm.py set-mode HEAT|COOL|HEATCOOL|OFF [idx]
  python3 nest_google_sdm.py set-range <heat_F> <cool_F> [idx]
  python3 nest_google_sdm.py set-eco on|off [idx]
"""

from __future__ import annotations

import os
import re
import sys
import asyncio
import logging
import secrets
import threading
import http.server
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from typing import Optional

try:
    import httpx
except ImportError:
    sys.exit("httpx required: pip install httpx")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://smartdevicemanagement.googleapis.com/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://nestservices.google.com/partnerconnections"
SDM_SCOPE = "https://www.googleapis.com/auth/sdm.service"

# Store config in ~/.config/nest/ so it's out of the working directory
DEFAULT_ENV_PATH = Path.home() / ".config" / "nest" / ".env"
ENV_PATH = Path(os.getenv("NEST_ENV_PATH", str(DEFAULT_ENV_PATH)))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("nest")


def _env() -> dict[str, str]:
    return {k: os.getenv(k, "").strip() for k in (
        "NEST_CLIENT_ID", "NEST_CLIENT_SECRET", "NEST_SDM_PROJECT_ID",
        "NEST_ACCESS_TOKEN", "NEST_REFRESH_TOKEN",
    )}


def _write_owner_only(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path, path)
        path.chmod(0o600)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _redact_secret_values(text: str, cfg: dict[str, str]) -> str:
    redacted = text
    for key in ("NEST_ACCESS_TOKEN", "NEST_REFRESH_TOKEN", "NEST_CLIENT_SECRET"):
        value = cfg.get(key)
        if value and len(value) >= 6:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _update_env(updates: dict[str, str]) -> None:
    content = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    for key, value in updates.items():
        if re.search(rf"^{re.escape(key)}=", content, re.MULTILINE):
            content = re.sub(rf"^{re.escape(key)}=.*", f"{key}={value}", content, flags=re.MULTILINE)
        else:
            content = content.rstrip() + f"\n{key}={value}\n"
    _write_owner_only(ENV_PATH, content)
    os.environ.update(updates)


def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
    except ImportError:
        for line in ENV_PATH.read_text().splitlines():
            m = re.match(r"^([^=]+)=(.*)$", line)
            if m:
                os.environ.setdefault(m.group(1).strip(), m.group(2).strip())


# ---------------------------------------------------------------------------
# Temperature helpers
# ---------------------------------------------------------------------------

def _c_to_f(c: Optional[float]) -> Optional[float]:
    return round(c * 9 / 5 + 32, 1) if c is not None else None


def _f_to_c(f: float) -> float:
    return round((f - 32) * 5 / 9, 1)


# ---------------------------------------------------------------------------
# OAuth Server (synchronous, runs in background thread)
# ---------------------------------------------------------------------------

_pending_state: Optional[str] = None
_callback_received: threading.Event = threading.Event()
_server_shutdown: threading.Event = threading.Event()


class _OAuthHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress request logs

    def do_GET(self):
        global _pending_state, _callback_received

        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/nest/callback":
            code = qs.get("code", [""])[0]
            state = qs.get("state", [""])[0]
            error = qs.get("error", [""])[0]

            if error:
                self._send_html(400, f"<h2 style='color:#ff6b6b'>OAuth Error: {error}</h2>")
                _callback_received.set()
                return

            if not code or state != _pending_state:
                self._send_html(400, "<h2 style='color:#ff6b6b'>Invalid or expired state. Restart oauth.</h2>")
                _callback_received.set()
                return

            cfg = _env()
            import httpx
            with httpx.Client(timeout=30) as client:
                resp = client.post(TOKEN_URL, data={
                    "grant_type": "authorization_code",
                    "client_id": cfg["NEST_CLIENT_ID"],
                    "client_secret": cfg["NEST_CLIENT_SECRET"],
                    "redirect_uri": f"http://localhost:{self.server.server_address[1]}/nest/callback",
                    "code": code,
                })

            if resp.status_code != 200:
                self._send_html(400, f"<pre>Token exchange failed: {_redact_secret_values(resp.text, cfg)}</pre>")
                _callback_received.set()
                return

            tokens = resp.json()
            _update_env({
                "NEST_ACCESS_TOKEN": tokens["access_token"],
                "NEST_REFRESH_TOKEN": tokens.get("refresh_token", cfg["NEST_REFRESH_TOKEN"]),
            })

            self._send_html(200, f"""
                <html style="font-family:monospace;padding:40px;background:#1a1a1a;color:#4ade80">
                <h2>OAuth Complete</h2>
                <p>Tokens saved to:<br><code>{ENV_PATH}</code></p>
                <p style="color:#888">Close this tab.</p>
                </html>""")
            _callback_received.set()
            _server_shutdown.set()

        elif parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_response(302)
            cfg = _env()
            redirect_uri = f"http://localhost:{self.server.server_address[1]}/nest/callback"
            params = urlencode({
                "redirect_uri": redirect_uri,
                "access_type": "offline",
                "prompt": "consent",
                "client_id": cfg["NEST_CLIENT_ID"],
                "response_type": "code",
                "scope": SDM_SCOPE,
                "state": _pending_state,
            })
            self.send_header("Location", f"{AUTH_URL}/{cfg['NEST_SDM_PROJECT_ID']}/auth?{params}")
            self.end_headers()

    def _send_html(self, code: int, body: str) -> None:
        body_bytes = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)


def _run_oauth_server_blocking(port: int) -> None:
    server = http.server.HTTPServer(("127.0.0.1", port), _OAuthHandler)
    server.timeout = 1
    logger.info(f"OAuth server listening on port {port}")
    while not _server_shutdown.is_set():
        server.handle_request()


def _start_oauth_in_background(port: int) -> threading.Thread:
    t = threading.Thread(target=_run_oauth_server_blocking, args=(port,), daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Nest Client
# ---------------------------------------------------------------------------

class NestClient:
    """Google Nest SDM API client with automatic token refresh."""

    def __init__(self) -> None:
        cfg = _env()
        self.project_id = cfg["NEST_SDM_PROJECT_ID"]
        self.client_id = cfg["NEST_CLIENT_ID"]
        self.client_secret = cfg["NEST_CLIENT_SECRET"]
        self._access_token = cfg["NEST_ACCESS_TOKEN"]
        self._refresh_token = cfg["NEST_REFRESH_TOKEN"]
        self._http = httpx.AsyncClient(timeout=30)

        if not self.project_id:
            raise ValueError("NEST_SDM_PROJECT_ID not set. Check ~/.config/nest/.env")
        if not self._refresh_token:
            raise ValueError(
                "NEST_REFRESH_TOKEN not set. Run: python3 nest_google_sdm.py oauth"
            )

    async def close(self) -> None:
        await self._http.aclose()

    @property
    def _base(self) -> str:
        return f"/enterprises/{self.project_id}"

    # ---------------------------------------------------------------------------
    # Token management
    # ---------------------------------------------------------------------------

    async def _refresh_tokens(self) -> None:
        logger.info("Refreshing Nest access token...")
        resp = await self._http.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self._refresh_token,
        })
        if resp.status_code != 200:
            cfg = _env()
            raise RuntimeError(f"Token refresh failed ({resp.status_code}): {_redact_secret_values(resp.text, cfg)}")

        tokens = resp.json()
        self._access_token = tokens["access_token"]
        new_refresh = tokens.get("refresh_token", self._refresh_token)
        self._refresh_token = new_refresh
        _update_env({
            "NEST_ACCESS_TOKEN": self._access_token,
            "NEST_REFRESH_TOKEN": new_refresh,
        })
        logger.info(f"Token refreshed (expires in {tokens.get('expires_in', '?')}s)")

    # ---------------------------------------------------------------------------
    # Core request
    # ---------------------------------------------------------------------------

    async def _request(self, method: str, path: str, json_body: dict | None = None) -> dict:
        url = f"{BASE_URL}{path}"
        headers = {"Authorization": f"Bearer {self._access_token}"}

        resp = await self._http.request(method, url, headers=headers, json=json_body)

        if resp.status_code == 401:
            await self._refresh_tokens()
            headers["Authorization"] = f"Bearer {self._access_token}"
            resp = await self._http.request(method, url, headers=headers, json=json_body)

        if resp.status_code >= 400:
            cfg = _env()
            raise RuntimeError(f"SDM API {method} {path} failed ({resp.status_code}): {_redact_secret_values(resp.text, cfg)}")

        return resp.json() if resp.content else {}

    # ---------------------------------------------------------------------------
    # Device discovery
    # ---------------------------------------------------------------------------

    async def list_devices(self) -> list[dict]:
        return (await self._request("GET", f"{self._base}/devices")).get("devices", [])

    async def _thermostat_names(self) -> list[str]:
        devices = await self.list_devices()
        return [
            d["name"] for d in devices
            if "sdm.devices.traits.ThermostatMode" in d.get("traits", {})
        ]

    async def _device_statuses(self) -> list[dict]:
        """Parallel fetch full state for all thermostats."""
        names = await self._thermostat_names()
        if not names:
            return []

        results = await asyncio.gather(*[
            self._request("GET", f"/{n}") for n in names
        ])

        statuses = []
        for i, traits in enumerate(results):
            t = traits.get("traits", {})

            def get_trait(key: str, sub: str | None = None):
                value = t.get(key, {})
                return value.get(sub) if sub else value

            custom_name = get_trait("sdm.devices.traits.Info", "customName") or f"Thermostat {i}"
            ambient = get_trait("sdm.devices.traits.Temperature")
            humidity_trait = get_trait("sdm.devices.traits.Humidity")
            mode_trait = get_trait("sdm.devices.traits.ThermostatMode")
            hvac_trait = get_trait("sdm.devices.traits.ThermostatHvac")
            eco_trait = get_trait("sdm.devices.traits.ThermostatEco")
            setpoint = get_trait("sdm.devices.traits.ThermostatTemperatureSetpoint")
            connectivity = get_trait("sdm.devices.traits.Connectivity", "status") or "UNKNOWN"

            statuses.append({
                "index": i,
                "name": names[i],
                "custom_name": custom_name,
                "current_temp_f": _c_to_f(ambient.get("ambientTemperatureCelsius")) if ambient else None,
                "humidity": humidity_trait.get("ambientHumidityPercent") if humidity_trait else None,
                "mode": mode_trait.get("mode", "UNKNOWN") if mode_trait else "UNKNOWN",
                "hvac_status": hvac_trait.get("status", "") if hvac_trait else "",
                "heat_setpoint_f": _c_to_f(setpoint.get("heatCelsius")) if setpoint else None,
                "cool_setpoint_f": _c_to_f(setpoint.get("coolCelsius")) if setpoint else None,
                "eco_mode": eco_trait.get("mode", "") if eco_trait else "",
                "connectivity": connectivity,
            })
        return statuses

    async def get_status(self, idx: int | None = None) -> list[dict] | dict:
        statuses = await self._device_statuses()
        return statuses[idx] if idx is not None else statuses

    # ---------------------------------------------------------------------------
    # Control
    # ---------------------------------------------------------------------------

    async def _exec(self, device_idx: int, command: str, params: dict) -> dict:
        names = await self._thermostat_names()
        if device_idx >= len(names):
            raise ValueError(f"Thermostat index {device_idx} out of range (have {len(names)})")
        return await self._request(
            "POST",
            f"/{names[device_idx]}:executeCommand",
            json_body={"command": command, "params": params},
        )

    async def set_mode(self, idx: int, mode: str) -> dict:
        result = await self._exec(idx, "sdm.devices.commands.ThermostatMode.SetMode", {"mode": mode.upper()})
        logger.info(f"Thermostat {idx} mode -> {mode.upper()}")
        return result

    async def set_temp(self, idx: int, temp_f: float) -> dict:
        statuses = await self.get_status(idx)
        s = statuses if isinstance(statuses, dict) else statuses[0]
        mode = s["mode"]

        if mode == "HEAT":
            result = await self._exec(idx, "sdm.devices.commands.ThermostatTemperatureSetpoint.SetHeat", {
                "heatCelsius": _f_to_c(temp_f),
            })
        elif mode == "COOL":
            result = await self._exec(idx, "sdm.devices.commands.ThermostatTemperatureSetpoint.SetCool", {
                "coolCelsius": _f_to_c(temp_f),
            })
        elif mode == "HEATCOOL":
            logger.warning("Thermostat in HEATCOOL mode. Set mode first or use set-range.")
            return {"status": "skipped", "reason": "HEATCOOL mode — set HEAT or COOL first"}
        else:
            logger.warning(f"Thermostat in {mode} mode. Set mode to HEAT or COOL first.")
            return {"status": "skipped", "reason": f"mode is {mode}"}

        logger.info(f"Thermostat {idx} temp -> {temp_f}F")
        return result

    async def set_range(self, idx: int, heat_f: float, cool_f: float) -> dict:
        result = await self._exec(idx, "sdm.devices.commands.ThermostatTemperatureSetpoint.SetRange", {
            "heatCelsius": _f_to_c(heat_f),
            "coolCelsius": _f_to_c(cool_f),
        })
        logger.info(f"Thermostat {idx} range -> {heat_f}F–{cool_f}F")
        return result

    async def set_eco(self, idx: int, enabled: bool) -> dict:
        mode = "MANUAL_ECO" if enabled else "OFF"
        result = await self._exec(idx, "sdm.devices.commands.ThermostatEco.SetMode", {"mode": mode})
        logger.info(f"Thermostat {idx} eco -> {mode}")
        return result


# ---------------------------------------------------------------------------
# OAuth entry point
# ---------------------------------------------------------------------------

def _cmd_oauth() -> None:
    global _pending_state, _server_shutdown, _callback_received

    _load_env()
    cfg = _env()

    if not cfg["NEST_CLIENT_ID"]:
        sys.exit("NEST_CLIENT_ID not set. Edit ~/.config/nest/.env first.")
    if not cfg["NEST_SDM_PROJECT_ID"]:
        sys.exit("NEST_SDM_PROJECT_ID not set. Edit ~/.config/nest/.env first.")

    port = int(os.getenv("NEST_OAUTH_PORT", "8000"))
    _pending_state = secrets.token_hex(16)

    print("\n  Open this URL in your browser:\n")
    print(f"  http://localhost:{port}/nest/oauth/start\n")
    print("  Waiting for OAuth callback...\n")

    server_thread = _start_oauth_in_background(port)

    # Wait for callback (or keyboard interrupt)
    try:
        _callback_received.wait()
    except KeyboardInterrupt:
        pass
    finally:
        _server_shutdown.set()
        server_thread.join(timeout=3)

    print("\n  Done. You can now use the CLI commands.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def cli_main() -> None:
    _load_env()

    if len(sys.argv) < 2 or sys.argv[1] in ("help", "--help"):
        print("Usage: python3 nest_google_sdm.py <command> [args]\n")
        print("Commands:")
        print("  oauth                  Start OAuth server (one-time, opens browser)")
        print("  devices                List all Nest devices")
        print("  status [idx]           Show thermostat status")
        print("  set-temp <F> [idx]     Set target temperature (Fahrenheit)")
        print("  set-mode <M> [idx]     Set mode: HEAT | COOL | HEATCOOL | OFF")
        print("  set-range <H> <C> [idx] Set heat/cool range (HEATCOOL mode)")
        print("  set-eco <on|off> [idx]  Toggle eco mode")
        print("\nConfig: ~/.config/nest/.env")
        return

    cmd = sys.argv[1].lower()

    if cmd == "oauth":
        _cmd_oauth()
        return

    client = NestClient()
    try:
        if cmd == "devices":
            devices = await client.list_devices()
            for i, d in enumerate(devices):
                traits = d.get("traits", {})
                name = traits.get("sdm.devices.traits.Info", {}).get("customName", "unnamed")
                print(f"[{i}] {name} ({d.get('type', 'unknown')})")
                print(f"    {d['name']}")

        elif cmd == "status":
            idx = int(sys.argv[2]) if len(sys.argv) > 2 else None
            statuses = await client.get_status(idx)
            if idx is not None:
                statuses = [statuses]
            for s in statuses:
                temp_str = f"{s['current_temp_f']}F"
                if s["current_temp_f"] is not None and s.get("current_temp_c"):
                    temp_str += f" ({s['current_temp_c']}C)"
                print(f"\n{'='*44}")
                print(f"  {s['custom_name']} [{s['connectivity']}]")
                print(f"{'='*44}")
                print(f"  Temperature: {temp_str}")
                print(f"  Humidity:    {s['humidity']}%")
                print(f"  Mode:        {s['mode']}")
                print(f"  HVAC:        {s['hvac_status']}")
                if s["heat_setpoint_f"]:
                    print(f"  Heat set to: {s['heat_setpoint_f']}F")
                if s["cool_setpoint_f"]:
                    print(f"  Cool set to: {s['cool_setpoint_f']}F")
                print(f"  Eco:         {s['eco_mode']}")

        elif cmd == "set-temp":
            if len(sys.argv) < 3:
                sys.exit("Usage: set-temp <F> [device_index]")
            temp = float(sys.argv[2])
            idx = int(sys.argv[3]) if len(sys.argv) > 3 else 0
            result = await client.set_temp(idx, temp)
            if result.get("status") == "skipped":
                print(f"  Skipped: {result['reason']}")

        elif cmd == "set-mode":
            if len(sys.argv) < 3:
                sys.exit("Usage: set-mode <HEAT|COOL|HEATCOOL|OFF> [device_index]")
            mode = sys.argv[2].upper()
            idx = int(sys.argv[3]) if len(sys.argv) > 3 else 0
            await client.set_mode(idx, mode)

        elif cmd == "set-range":
            if len(sys.argv) < 4:
                sys.exit("Usage: set-range <heat_F> <cool_F> [device_index]")
            heat_f, cool_f = float(sys.argv[2]), float(sys.argv[3])
            idx = int(sys.argv[4]) if len(sys.argv) > 4 else 0
            await client.set_range(idx, heat_f, cool_f)

        elif cmd == "set-eco":
            if len(sys.argv) < 3:
                sys.exit("Usage: set-eco <on|off> [device_index]")
            enabled = sys.argv[2].lower() in ("on", "true", "1", "yes")
            idx = int(sys.argv[3]) if len(sys.argv) > 3 else 0
            await client.set_eco(idx, enabled)

        else:
            print(f"Unknown command: {cmd}")
            sys.exit(1)

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(cli_main())
