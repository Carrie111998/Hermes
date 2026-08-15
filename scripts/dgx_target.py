#!/usr/bin/env python3
"""Resolve the configured DGX SSH target without applying Hermes defaults."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_constants import get_hermes_home  # noqa: E402
from utils import fast_safe_load  # noqa: E402


CONFIG_ERROR = 78
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]*$")
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _error(reason: str) -> int:
    print(f"CONFIG_ERROR:{reason}", file=sys.stderr)
    return CONFIG_ERROR


def _valid_host(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    host = value.strip()
    if host != value or not 1 <= len(host) <= 253:
        return False
    labels = host.split(".")
    return all(1 <= len(label) <= 63 and _HOST_LABEL_RE.fullmatch(label) for label in labels)


def _valid_user(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and 1 <= len(value) <= 32
        and _USER_RE.fullmatch(value) is not None
    )


def resolve_target() -> int:
    config_path = get_hermes_home() / "config.yaml"
    try:
        with config_path.open(encoding="utf-8") as stream:
            config = fast_safe_load(stream)
    except FileNotFoundError:
        return _error("config_missing")
    except (OSError, UnicodeError):
        return _error("config_unreadable")
    except Exception:
        return _error("config_malformed")

    if not isinstance(config, dict):
        return _error("config_not_mapping")
    target = config.get("dgx_ssh")
    if not isinstance(target, dict):
        return _error("target_not_mapping")

    host = target.get("host")
    user = target.get("user")
    if host is None or user is None:
        return _error("target_incomplete")
    if not _valid_host(host):
        return _error("host_invalid")
    if not _valid_user(user):
        return _error("user_invalid")

    print(f"{user}@{host}")
    return 0


if __name__ == "__main__":
    raise SystemExit(resolve_target())
