"""Session and response ownership helpers for API-server multi-user scope."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping


SCOPE_KEYS = ("tenant_id", "workspace_id", "project_id", "user_id")


def has_principal_scope(scope: Mapping[str, Any] | None) -> bool:
    return bool(scope) and any(str(scope.get(key) or "") for key in SCOPE_KEYS)


def missing_scope_keys(scope: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not has_principal_scope(scope):
        return ()
    return tuple(key for key in SCOPE_KEYS if not str(scope.get(key) or ""))


def scope_fields(scope: Mapping[str, Any] | None) -> dict[str, str]:
    if not has_principal_scope(scope):
        return {}
    return {key: str(scope.get(key) or "") for key in SCOPE_KEYS}


def scope_matches_record(
    scope: Mapping[str, Any] | None,
    record: Mapping[str, Any] | None,
) -> bool:
    if not has_principal_scope(scope):
        return True
    if not record:
        return False
    return all(str(record.get(key) or "") == str(scope.get(key) or "") for key in SCOPE_KEYS)


def scoped_cache_key(name: str | None, scope: Mapping[str, Any] | None) -> str | None:
    if not name:
        return name
    if not has_principal_scope(scope):
        return name
    return f"{scope_fingerprint(scope)}:{name}"


def scope_fingerprint(scope: Mapping[str, Any] | None) -> str:
    if not has_principal_scope(scope):
        return "legacy"
    seed = "\n".join(str(scope.get(key) or "") for key in SCOPE_KEYS)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
