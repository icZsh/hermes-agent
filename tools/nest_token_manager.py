#!/usr/bin/env python3
"""
Nest SDM API Token Manager
- Reads credentials from ~/.hermes/.env
- Automatically refreshes access token when expired
- Updates .env on successful refresh
- Exits with clear error if refresh token is also invalid
"""

import urllib.request
import urllib.parse
import json
import os
import sys

from hermes_constants import get_default_hermes_root

ENV_PATH = os.getenv("NEST_ENV_PATH") or str(get_default_hermes_root() / ".env")
TOKEN_FILE = os.getenv("NEST_TOKEN_CACHE", "/tmp/nest_tokens.json")

def load_env():
    """Load Nest credentials from .env file."""
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
    """Atomically update .env file with new credentials."""
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
    """Use refresh token to get a new access token."""
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
    """Cache tokens to a local file as backup."""
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
    """Test API connectivity with current access token."""
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

    # Always refresh to get a fresh access token
    print("Refreshing access token...")
    try:
        tokens = refresh_access_token(client_id, client_secret, refresh_token)
    except Exception as e:
        print(f"FATAL: {e}")
        print("The refresh token is invalid or expired. Manual re-authorization required.")
        print("Steps:")
        print("  1. Open: https://accounts.google.com/o/oauth2/v2/auth?client_id={}&redirect_uri=http://localhost:8000/nest/callback&response_type=code&scope=https://www.googleapis.com/auth/sdm.service&access_type=offline&prompt=consent".format(client_id))
        print("  2. Get the code from the redirect URL")
        print("  3. Manually exchange it using the token exchange endpoint")
        sys.exit(1)

    new_access = tokens["access_token"]
    new_refresh = tokens.get("refresh_token", refresh_token)  # Google may issue a new refresh token

    # Save to .env
    creds["NEST_ACCESS_TOKEN"] = new_access
    if new_refresh != refresh_token:
        creds["NEST_REFRESH_TOKEN"] = new_refresh
        print("New refresh token issued and saved.")
    save_env(creds)
    save_token_cache(tokens)
    print("Tokens updated in ~/.hermes/.env")

    # Test API
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
