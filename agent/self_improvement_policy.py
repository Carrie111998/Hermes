"""Canonical self-improvement policy for post-turn writes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

ALLOW = "ALLOW"
DENY_ENV_DISABLED = "DENY_ENV_DISABLED"
DENY_READ_ONLY_SESSION = "DENY_READ_ONLY_SESSION"
DENY_UNKNOWN_OPERATION = "DENY_UNKNOWN_OPERATION"

SELF_IMPROVEMENT_OPERATIONS = frozenset({
    "skill_write", "skill_create", "skill_edit", "skill_patch", "skill_delete",
    "skill_write_file", "skill_remove_file", "memory_write", "memory_delete",
    "suggestions_write", "background_review_spawn",
})
BACKGROUND_REVIEW_ORIGIN = "background_review"


def _normalize_bool(value: Any) -> Optional[bool]:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value) if value in (0, 1) else None
    if isinstance(value, str):
        value = value.strip().lower()
        if not value:
            return False
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
        return None
    return False


def normalize_env_disabled(value: Any) -> bool:
    if isinstance(value, str) and not value.strip():
        return True
    normalized = _normalize_bool(value)
    return normalized is None or normalized


def normalize_read_only_session(value: Any) -> bool:
    normalized = _normalize_bool(value)
    return normalized is None or normalized


@dataclass(frozen=True)
class Decision:
    result: str
    reason: str

    @property
    def allow(self) -> bool:
        return self.result == ALLOW


def evaluate(
    *,
    environment_disabled: Any,
    session_read_only: Any,
    operation_kind: str,
    origin: Optional[str] = None,
    target_path: Optional[str] = None,
    explicit_opt_in: Any = None,
) -> Decision:
    env_disabled = normalize_env_disabled(environment_disabled)
    read_only = normalize_read_only_session(session_read_only)
    suffix = f" target={target_path!r}" if target_path else ""
    if read_only and origin == BACKGROUND_REVIEW_ORIGIN:
        return Decision(DENY_READ_ONLY_SESSION, f"read-only session refuses background-review {operation_kind!r}{suffix}")
    if env_disabled:
        return Decision(DENY_ENV_DISABLED, f"self-improvement disabled; refusing {operation_kind!r}{suffix}")
    if operation_kind not in SELF_IMPROVEMENT_OPERATIONS or explicit_opt_in is False:
        return Decision(DENY_UNKNOWN_OPERATION, f"unrecognized or unauthorized self-improvement operation {operation_kind!r}{suffix}")
    return Decision(ALLOW, f"self-improvement policy allows {operation_kind!r}")


__all__ = [
    "ALLOW", "DENY_ENV_DISABLED", "DENY_READ_ONLY_SESSION", "DENY_UNKNOWN_OPERATION",
    "SELF_IMPROVEMENT_OPERATIONS", "BACKGROUND_REVIEW_ORIGIN", "Decision", "evaluate",
    "normalize_env_disabled", "normalize_read_only_session",
]
