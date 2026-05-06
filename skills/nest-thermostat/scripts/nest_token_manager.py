#!/usr/bin/env python3
"""
Nest SDM API Token Manager
- Reads credentials from NEST_ENV_PATH or the active Hermes .env
- Automatically refreshes access token when expired
- Updates .env on successful refresh
- Exits with clear error if refresh token is also invalid

Usage:
  python3 nest_token_manager.py              # refresh + test
  python3 nest_token_manager.py --no-test    # refresh only, skip API test
"""

import urllib.request
import urllib.parse
import json
import os
import sys
from pathlib import Path

ENV_PATH = os.getenv("NEST_ENV_PATH") or str(Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))) / ".env")
TOKEN_FILE = os.getenv("NEST_TOKEN_CACHE", "/tmp/nest_tokens.json")

def load_env():
    creds = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    creds[key] = val
    return creds

def save_env(creds):
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            lines = f.readlines()

    new_lines = []
    keys_in_creds = set(creds.keys())
    keys_seen = set()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or (not stripped):
            new_lines.append(line)
        elif "=" in stripped:
            key = stripped.split("=", 1)[0]
            if key in keys_in_creds and key not in keys_seen:
                new_lines.append(f"{key}={creds[key]}\n")
                keys_seen.add(key)
            else:
                new_lines.append(line)

    for key in keys_in_creds:
        if key not in keys_seen:
            new_lines.append(f"{key}={creds[key]}\n")

    content = "".join(new_lines)
    tmp_file = f"{ENV_PATH}.{os.getpid()}.tmp"
    fd = os.open(tmp_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp_file, ENV_PATH)
        os.chmod(ENV_PATH, 0o600)
    finally:
        if os.path.exists(tmp_file):
            os.unlink(tmp_file)

def refresh_access_token(client_id, client_secret, refresh_token):
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }).encode()

    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = json.loads(e.read().decode())
        raise Exception(f"Token refresh failed: {error_body.get('error_description', error_body.get('error'))}")

def save_token_cache(tokens):
    tmp_file = f"{TOKEN_FILE}.{os.getpid()}.tmp"
    fd = os.open(tmp_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(tokens, f)
        os.replace(tmp_file, TOKEN_FILE)
        os.chmod(TOKEN_FILE, 0o600)
    finally:
        if os.path.exists(tmp_file):
            os.unlink(tmp_file)

def test_api(project_id, access_token):
    req = urllib.request.Request(
        f"https://smartdevicemanagement.googleapis.com/v1/enterprises/{project_id}/devices",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

def main():
    creds = load_env()

    required = ["NEST_PROJECT_ID", "NEST_CLIENT_ID", "NEST_CLIENT_SECRET", "NEST_REFRESH_TOKEN"]
    missing = [k for k in required if k not in creds]
    if missing:
        print(f"ERROR: Missing credentials in {ENV_PATH}: {missing}")
        sys.exit(1)

    project_id = creds["NEST_PROJECT_ID"]
    client_id = creds["NEST_CLIENT_ID"]
    client_secret = creds["NEST_CLIENT_SECRET"]
    refresh_token = creds["NEST_REFRESH_TOKEN"]

    print("Refreshing access token...")
    try:
        tokens = refresh_access_token(client_id, client_secret, refresh_token)
    except Exception as e:
        print(f"FATAL: {e}")
        print("The refresh token is invalid or expired. Manual re-authorization required.")
        sys.exit(1)

    new_access = tokens["access_token"]
    new_refresh = tokens.get("refresh_token", refresh_token)

    creds["NEST_ACCESS_TOKEN"] = new_access
    if new_refresh != refresh_token:
        creds["NEST_REFRESH_TOKEN"] = new_refresh
        print("New refresh token issued and saved.")
    save_env(creds)
    save_token_cache(tokens)
    print("Tokens updated in ~/.hermes/.env")

    if "--no-test" in sys.argv:
        print("Skipping API test.")
        return

    print("Testing API connection...")
    try:
        result = test_api(project_id, new_access)
        devices = result.get("devices", [])
        print(f"API OK. Devices found: {len(devices)}")
        if devices:
            for d in devices:
                print(f"  - {d.get('name')} ({d.get('type')})")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("Access token rejected. Credentials may need re-authorization.")
            sys.exit(1)
        raise

if __name__ == "__main__":
    main()
