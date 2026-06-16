"""Principal scope header parsing for API-server backed agent turns."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from gateway.session_acl import missing_scope_keys


PRINCIPAL_HEADERS = {
    "tenant_id": "X-Hermes-Tenant-Id",
    "workspace_id": "X-Hermes-Workspace-Id",
    "project_id": "X-Hermes-Project-Id",
    "user_id": "X-Hermes-User-Id",
    "roles": "X-Hermes-Roles",
    "sandbox_id": "X-Hermes-Sandbox-Id",
}

_CONTROL_CHARS = re.compile(r"[\r\n\x00]")


def _read_header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name, "")
    return str(value).strip() if value is not None else ""


def _validate_header_value(name: str, value: str, max_len: int) -> str | None:
    if _CONTROL_CHARS.search(value):
        return f"{name} contains invalid control characters"
    if len(value) > max_len:
        return f"{name} is too long"
    return None


def parse_principal_scope_headers(
    headers: Mapping[str, str],
    *,
    api_key_configured: bool,
    max_len: int = 256,
) -> tuple[dict[str, Any], str | None]:
    """Parse optional API-server principal scope headers.

    These headers are a P0 bridge from an authenticated gateway/API layer into
    the agent turn. They are accepted only when API-key auth is configured; the
    browser/client is not treated as an authority by itself.
    """

    raw = {
        key: _read_header(headers, header_name)
        for key, header_name in PRINCIPAL_HEADERS.items()
    }
    if not any(raw.values()):
        return {}, None

    if not api_key_configured:
        return {}, "Principal scope headers require API key authentication"

    for key, value in raw.items():
        if not value:
            continue
        error = _validate_header_value(PRINCIPAL_HEADERS[key], value, max_len)
        if error:
            return {}, error

    roles = tuple(role.strip() for role in raw["roles"].split(",") if role.strip())
    scope: dict[str, Any] = {
        "tenant_id": raw["tenant_id"],
        "workspace_id": raw["workspace_id"],
        "project_id": raw["project_id"],
        "user_id": raw["user_id"],
        "sandbox_id": raw["sandbox_id"],
    }
    if raw["roles"] or roles:
        scope["roles"] = roles
    missing = missing_scope_keys(scope)
    if missing:
        return {}, "Principal scope headers missing required fields: " + ", ".join(missing)
    return {key: value for key, value in scope.items() if value not in ("", ())}, None
