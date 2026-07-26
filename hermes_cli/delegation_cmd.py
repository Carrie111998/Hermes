"""Manage named credential bundles under ``delegation.profiles``."""
from __future__ import annotations

import re
import sys
from typing import Any

_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PROFILE_FIELDS = ("model", "provider", "base_url", "api_key", "api_mode")


def _load_raw_config() -> dict[str, Any]:
    from hermes_cli.config import read_raw_config

    return read_raw_config()


def _profiles(config: dict[str, Any]) -> dict[str, Any]:
    delegation = config.get("delegation")
    if not isinstance(delegation, dict):
        return {}
    profiles = delegation.get("profiles")
    return profiles if isinstance(profiles, dict) else {}


def _save_raw_config(config: dict[str, Any]) -> None:
    from hermes_cli.config import save_config

    save_config(config, strip_defaults=False)


def _validate_profile_name(name: str) -> str:
    name = str(name or "").strip()
    if not _PROFILE_NAME_RE.fullmatch(name):
        print(
            "Invalid delegation profile name. Use 1-64 letters, numbers, '_' or '-', "
            "starting with a letter or number.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return name


def cmd_profiles_list(_args) -> None:
    profiles = _profiles(_load_raw_config())
    if not profiles:
        print("No delegation profiles configured.")
        print("Add one with: hermes delegation profiles add NAME --model MODEL --provider PROVIDER")
        return

    print(f"Delegation profiles ({len(profiles)}):")
    for name in sorted(profiles):
        entry = profiles[name]
        if not isinstance(entry, dict):
            print(f"  {name}: invalid configuration")
            continue
        details = []
        for key in ("model", "provider", "base_url", "api_mode"):
            value = entry.get(key)
            if value:
                details.append(f"{key.replace('_', ' ')}: {value}")
        if entry.get("api_key"):
            details.append("api key: configured")
        print(f"  {name}: " + (", ".join(details) if details else "inherits delegation defaults"))


def cmd_profiles_add(args) -> None:
    name = _validate_profile_name(getattr(args, "profile_name", ""))
    entry = {
        field: value.strip() if isinstance(value, str) else value
        for field in _PROFILE_FIELDS
        if (value := getattr(args, field, None)) is not None
        and (not isinstance(value, str) or value.strip())
    }
    if not entry:
        print(
            "At least one of --model, --provider, --base-url, --api-key, or --api-mode is required.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    config = _load_raw_config()
    delegation = config.setdefault("delegation", {})
    if not isinstance(delegation, dict):
        delegation = {}
        config["delegation"] = delegation
    profiles = delegation.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        profiles = {}
        delegation["profiles"] = profiles
    replaced = name in profiles
    profiles[name] = entry
    _save_raw_config(config)
    verb = "Updated" if replaced else "Added"
    print(f"{verb} delegation profile '{name}'.")


def cmd_profiles_remove(args) -> None:
    name = _validate_profile_name(getattr(args, "profile_name", ""))
    config = _load_raw_config()
    delegation = config.get("delegation")
    profiles = _profiles(config)
    if name not in profiles:
        print(f"Delegation profile not found: {name}", file=sys.stderr)
        raise SystemExit(1)

    del profiles[name]
    if not profiles and isinstance(delegation, dict):
        delegation.pop("profiles", None)
    _save_raw_config(config)
    print(f"Removed delegation profile '{name}'.")


def cmd_delegation(args) -> None:
    """Dispatch ``hermes delegation profiles`` commands."""
    action = getattr(args, "delegation_action", None)
    profile_action = getattr(args, "profiles_action", None)
    if action != "profiles":
        print("Usage: hermes delegation profiles [list|add|remove]")
        raise SystemExit(2)
    if profile_action in {None, "", "list", "ls"}:
        cmd_profiles_list(args)
    elif profile_action == "add":
        cmd_profiles_add(args)
    elif profile_action in {"remove", "rm"}:
        cmd_profiles_remove(args)
    else:
        print(f"Unknown delegation profiles command: {profile_action}", file=sys.stderr)
        raise SystemExit(2)
