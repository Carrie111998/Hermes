#!/usr/bin/env python3
"""Bridge authentication from Hermes google-workspace Python skill to gws CLI.

Converts the existing google_token.json + google_client_secret.json
into a gws-compatible credentials file so both tools share the same OAuth session.

Usage:
  python bridge_auth.py --check       # Check if gws credentials exist
  python bridge_auth.py --bridge      # Convert Python OAuth token to gws format
  python bridge_auth.py --status      # Show status of both auth systems

The gws CLI accepts a credentials JSON file via GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE.
This script creates that file from the existing Hermes google_token.json.
"""

import argparse
import json
import os
import sys
from pathlib import Path

HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))

# Existing google-workspace skill paths
GOOGLE_TOKEN_PATH = HERMES_HOME / "google_token.json"
GOOGLE_CLIENT_SECRET_PATH = HERMES_HOME / "google_client_secret.json"

# gws CLI paths
GWS_CONFIG_DIR = Path(os.getenv(
    "GOOGLE_WORKSPACE_CLI_CONFIG_DIR",
    HERMES_HOME / "gws-config"
))
GWS_CREDENTIALS_PATH = GWS_CONFIG_DIR / "credentials.json"
GWS_CLIENT_SECRET_PATH = GWS_CONFIG_DIR / "client_secret.json"


def check_gws_auth() -> bool:
    """Check if gws credentials file exists and looks valid."""
    if not GWS_CREDENTIALS_PATH.exists():
        print(f"NOT_CONFIGURED: No gws credentials at {GWS_CREDENTIALS_PATH}")
        return False

    try:
        data = json.loads(GWS_CREDENTIALS_PATH.read_text())
        if data.get("token") or data.get("access_token") or data.get("refresh_token"):
            print(f"CONFIGURED: gws credentials at {GWS_CREDENTIALS_PATH}")
            return True
        print("INVALID: Credentials file exists but missing token fields")
        return False
    except (json.JSONDecodeError, Exception) as e:
        print(f"ERROR: {e}")
        return False


def check_python_auth() -> bool:
    """Check if the Python google-workspace skill has valid auth."""
    if not GOOGLE_TOKEN_PATH.exists():
        return False
    try:
        data = json.loads(GOOGLE_TOKEN_PATH.read_text())
        return bool(data.get("refresh_token") or data.get("token"))
    except Exception:
        return False


def bridge_auth():
    """Convert Python google-workspace OAuth token to gws CLI format."""
    if not GOOGLE_TOKEN_PATH.exists():
        print(f"ERROR: No Python OAuth token at {GOOGLE_TOKEN_PATH}")
        print("Set up the google-workspace skill first, or run gws auth login directly.")
        sys.exit(1)

    # Read existing token
    try:
        token_data = json.loads(GOOGLE_TOKEN_PATH.read_text())
    except (json.JSONDecodeError, Exception) as e:
        print(f"ERROR: Cannot read token: {e}")
        sys.exit(1)

    # Read client secret if available
    client_id = token_data.get("client_id", "")
    client_secret = token_data.get("client_secret", "")

    if not client_id and GOOGLE_CLIENT_SECRET_PATH.exists():
        try:
            cs_data = json.loads(GOOGLE_CLIENT_SECRET_PATH.read_text())
            installed = cs_data.get("installed", cs_data.get("web", {}))
            client_id = installed.get("client_id", "")
            client_secret = installed.get("client_secret", "")
        except Exception:
            pass

    # Build gws-compatible credentials
    # gws expects an OAuth credentials JSON with these fields
    gws_creds = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
        },
        "token": token_data.get("token", ""),
        "refresh_token": token_data.get("refresh_token", ""),
        "token_uri": token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        "scopes": token_data.get("scopes", []),
        "expiry": token_data.get("expiry", ""),
    }

    # Create gws config directory
    GWS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Write gws credentials
    GWS_CREDENTIALS_PATH.write_text(json.dumps(gws_creds, indent=2))
    print(f"OK: gws credentials written to {GWS_CREDENTIALS_PATH}")

    # Also copy client_secret.json for gws if available
    if GOOGLE_CLIENT_SECRET_PATH.exists() and not GWS_CLIENT_SECRET_PATH.exists():
        import shutil
        shutil.copy2(GOOGLE_CLIENT_SECRET_PATH, GWS_CLIENT_SECRET_PATH)
        print(f"OK: Client secret copied to {GWS_CLIENT_SECRET_PATH}")

    print(f"\nSet this environment variable:")
    print(f"  export GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE={GWS_CREDENTIALS_PATH}")


def show_status():
    """Show status of both authentication systems."""
    print("=== Authentication Status ===\n")

    print("Python google-workspace skill:")
    if check_python_auth():
        print(f"  Token: {GOOGLE_TOKEN_PATH}")
        print(f"  Status: AUTHENTICATED")
    else:
        print(f"  Token: {GOOGLE_TOKEN_PATH} (missing)")
        print(f"  Status: NOT_AUTHENTICATED")

    print()

    print("gws CLI:")
    if check_gws_auth():
        print(f"  Config dir: {GWS_CONFIG_DIR}")
        creds_env = os.getenv("GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE", "not set")
        print(f"  CREDENTIALS_FILE env: {creds_env}")
    else:
        print(f"  Config dir: {GWS_CONFIG_DIR} (not configured)")

    print()

    if check_python_auth() and not check_gws_auth():
        print("TIP: Run --bridge to share Python OAuth token with gws CLI")


def main():
    parser = argparse.ArgumentParser(description="Bridge auth between google-workspace and gws CLI")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Check if gws credentials exist")
    group.add_argument("--bridge", action="store_true", help="Convert Python OAuth token to gws format")
    group.add_argument("--status", action="store_true", help="Show status of both auth systems")
    args = parser.parse_args()

    if args.check:
        sys.exit(0 if check_gws_auth() else 1)
    elif args.bridge:
        bridge_auth()
    elif args.status:
        show_status()


if __name__ == "__main__":
    main()
