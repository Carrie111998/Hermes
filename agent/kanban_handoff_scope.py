"""Exact gateway-origin scope for Phase-1 short-task handoff.

The global feature flag is only a kill switch.  A gateway task becomes a
short-task chain only when its authenticated platform/chat/user identity
matches one explicit ``allowed_origins`` entry.  The resulting worker policy
is frozen into the task's durable control binding at creation time so later
allowlist or threshold changes cannot retroactively widen an old task.

Ordinary CLI/local Kanban creation has no authenticated gateway identity and
therefore remains outside this scope.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


TASK_POLICY_SCHEMA = 1
_MATCH_REQUIRED = ("platform", "chat_type", "chat_id", "user_id")
_MATCH_OPTIONAL = (
    "scope_id",
    "thread_id",
    "notifier_profile",
    "session_key",
)
_IDENTITY_FIELDS = (
    "platform",
    "scope_id",
    "chat_type",
    "chat_id",
    "thread_id",
    "user_id",
    "notifier_profile",
    "session_key",
)
_CONTROL_REQUIRED = (
    "platform",
    "chat_type",
    "chat_id",
    "user_id",
    "notifier_profile",
    "session_key",
)


def _clean_identity(identity: Mapping[str, Any] | None) -> dict[str, str]:
    source = identity if isinstance(identity, Mapping) else {}
    values = {name: str(source.get(name) or "").strip() for name in _IDENTITY_FIELDS}
    values["platform"] = values["platform"].lower()
    values["chat_type"] = values["chat_type"].lower()
    return values


def normalize_short_task_allowed_origins(
    config: Mapping[str, Any] | None,
) -> tuple[list[dict[str, str]], str | None]:
    """Return ``(normalized_entries, validation_error)`` for the allowlist.

    A disabled feature or an enabled feature with no configured entries returns
    ``([], None)``.  Any malformed opt-in returns ``([], <safe error>)``.  Every
    successful entry contains all required exact-match fields and only supported
    optional narrowing fields.
    """
    try:
        raw_handoff = (((config or {}).get("kanban") or {}).get(
            "short_task_handoff"
        ) or {})
    except Exception:
        return [], "short-task settings must be a mapping"
    if not isinstance(raw_handoff, Mapping):
        return [], "short-task settings must be a mapping"
    if raw_handoff.get("enabled") is not True:
        return [], None
    raw_entries = raw_handoff.get("allowed_origins")
    if raw_entries is None or raw_entries == []:
        return [], None
    if not isinstance(raw_entries, list):
        return [], "allowed_origins must be a list"

    allowed_fields = set(_MATCH_REQUIRED + _MATCH_OPTIONAL)
    normalized: list[dict[str, str]] = []
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, Mapping):
            return [], f"allowed_origins[{index}] must be a mapping"
        unknown = set(raw_entry) - allowed_fields
        if unknown:
            return [], f"allowed_origins[{index}] contains unsupported fields"
        entry: dict[str, str] = {}
        for name in _MATCH_REQUIRED:
            value = raw_entry.get(name)
            if not isinstance(value, str) or not value.strip():
                return [], f"allowed_origins[{index}].{name} is required"
            entry[name] = value.strip()
        entry["platform"] = entry["platform"].lower()
        entry["chat_type"] = entry["chat_type"].lower()
        for name in _MATCH_OPTIONAL:
            if name not in raw_entry:
                continue
            value = raw_entry.get(name)
            if not isinstance(value, str) or not value.strip():
                return [], f"allowed_origins[{index}].{name} must be non-empty"
            entry[name] = value.strip()
        normalized.append(entry)
    return normalized, None


def normalize_short_task_allowed_workspace_roots(
    config: Mapping[str, Any] | None,
) -> tuple[list[str], str | None]:
    """Return canonical exact pilot roots or a fail-closed config error.

    These are exact workspace directories, not ancestor prefixes. A task may
    enter the managed lane only when its canonical ``dir`` workspace equals
    one entry byte-for-byte. This keeps an allowed chat from accidentally
    targeting another repository, a Hermes home, or any other existing path.
    """
    try:
        raw_handoff = (((config or {}).get("kanban") or {}).get(
            "short_task_handoff"
        ) or {})
    except Exception:
        return [], "short-task settings must be a mapping"
    if not isinstance(raw_handoff, Mapping):
        return [], "short-task settings must be a mapping"
    if raw_handoff.get("enabled") is not True:
        return [], None
    raw_roots = raw_handoff.get("allowed_workspace_roots")
    if not isinstance(raw_roots, list) or not raw_roots:
        return [], "allowed_workspace_roots must be a non-empty list"

    canonical: list[str] = []
    seen: set[str] = set()
    for index, raw_root in enumerate(raw_roots):
        if not isinstance(raw_root, str) or not raw_root.strip():
            return [], f"allowed_workspace_roots[{index}] must be non-empty"
        supplied = Path(raw_root.strip())
        if not supplied.is_absolute():
            return [], f"allowed_workspace_roots[{index}] must be absolute"
        try:
            resolved = supplied.resolve(strict=True)
        except (OSError, RuntimeError):
            return [], f"allowed_workspace_roots[{index}] must already exist"
        if not resolved.is_dir():
            return [], f"allowed_workspace_roots[{index}] must be a directory"
        if resolved == Path(resolved.anchor):
            return [], f"allowed_workspace_roots[{index}] is too broad"
        value = str(resolved)
        if value in seen:
            continue
        seen.add(value)
        canonical.append(value)
    return canonical, None


def match_short_task_allowed_source(
    config: Mapping[str, Any] | None,
    identity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Classify allowlist matching separately from delivery completeness.

    ``platform + chat_type + chat_id + user_id`` identify the intended source.
    Optional allowlist fields narrow it.  A missing optional field is reported
    as an incomplete candidate rather than a mismatch, so a trusted-source
    launch cannot silently degrade into an ordinary task merely because its
    stable session/delivery proof was unavailable.
    """
    allowed, allowlist_error = normalize_short_task_allowed_origins(config)
    if allowlist_error:
        return {
            "matched": False,
            "candidate": False,
            "validation_error": allowlist_error,
            "reason": "invalid_allowlist",
        }
    try:
        handoff_settings = (((config or {}).get("kanban") or {}).get(
            "short_task_handoff"
        ) or {})
    except Exception:
        return {
            "matched": False,
            "candidate": False,
            "validation_error": "short-task settings must be a mapping",
            "reason": "invalid_allowlist",
        }
    if handoff_settings.get("enabled") is not True:
        return {"matched": False, "candidate": False, "reason": "feature_disabled"}
    if not allowed:
        return {"matched": False, "candidate": False, "reason": "allowlist_missing"}

    origin = _clean_identity(identity)
    if any(not origin[name] for name in _MATCH_REQUIRED):
        return {
            "matched": False,
            "candidate": False,
            "reason": "source_identity_incomplete",
        }

    incomplete_candidate: dict[str, str] | None = None
    for entry in allowed:
        if any(origin[name] != entry[name] for name in _MATCH_REQUIRED):
            continue
        optional_mismatch = False
        optional_missing = False
        for name in _MATCH_OPTIONAL:
            if name not in entry:
                continue
            if not origin[name]:
                optional_missing = True
            elif origin[name] != entry[name]:
                optional_mismatch = True
                break
        if optional_mismatch:
            continue
        if optional_missing:
            incomplete_candidate = entry
            continue
        return {
            "matched": True,
            "candidate": True,
            "reason": "origin_allowed",
            "origin": origin,
            "matched_origin": dict(entry),
        }

    if incomplete_candidate is not None:
        return {
            "matched": False,
            "candidate": True,
            "reason": "source_identity_incomplete",
            "origin": origin,
            "matched_origin": dict(incomplete_candidate),
        }
    return {"matched": False, "candidate": False, "reason": "origin_not_allowed"}


def decide_gateway_origin(
    config: Mapping[str, Any] | None,
    identity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Authorize one exact authenticated Gateway origin and freeze its policy."""
    source_match = match_short_task_allowed_source(config, identity)
    if source_match.get("validation_error"):
        return {
            "authorized": False,
            "validation_error": str(source_match["validation_error"]),
            "reason": str(source_match.get("reason") or "invalid_allowlist"),
        }
    if source_match.get("reason") in {"feature_disabled", "allowlist_missing"}:
        return {"authorized": False, "reason": source_match["reason"]}

    # Imported only after allowlist normalization so the dispatcher policy
    # builder can reuse the same helper without a module-import cycle.
    from agent.kanban_auto_handoff import build_dispatcher_policy_snapshot

    worker_policy = build_dispatcher_policy_snapshot(config)
    if worker_policy.get("validation_error"):
        return {
            "authorized": False,
            "validation_error": str(worker_policy["validation_error"]),
            "reason": "invalid_worker_policy",
        }
    if worker_policy.get("enabled") is not True:
        return {"authorized": False, "reason": "feature_disabled"}

    origin = _clean_identity(identity)
    if source_match.get("candidate") and not source_match.get("matched"):
        return {"authorized": False, "reason": "identity_incomplete"}
    if any(not origin[name] for name in _CONTROL_REQUIRED):
        return {"authorized": False, "reason": "identity_incomplete"}
    if source_match.get("matched") is not True:
        return {
            "authorized": False,
            "reason": (
                "identity_incomplete"
                if source_match.get("reason") == "source_identity_incomplete"
                else "origin_not_allowed"
            ),
        }
    matched = dict(source_match["matched_origin"])

    task_policy = {
        "schema": TASK_POLICY_SCHEMA,
        "authorized": True,
        "origin": origin,
        "matched_origin": dict(matched),
        "worker_policy": worker_policy,
    }
    return {
        "authorized": True,
        "reason": "origin_allowed",
        "task_policy": task_policy,
        "task_policy_json": json.dumps(
            task_policy,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def _load_current_dispatcher_config() -> Mapping[str, Any]:
    """Load the process-owner config rather than an assignee profile config."""
    from hermes_constants import (
        get_process_hermes_home,
        reset_hermes_home_override,
        set_hermes_home_override,
    )
    from hermes_cli.config import load_config_current_strict

    policy_home = str(get_process_hermes_home()).strip()
    if not policy_home:
        raise RuntimeError("dispatcher policy home is unavailable")
    token = set_hermes_home_override(policy_home)
    try:
        return load_config_current_strict()
    finally:
        reset_hermes_home_override(token)


def current_gateway_identity() -> dict[str, str] | None:
    """Return the current request-scoped identity, never process-global residue."""
    if os.environ.get("_HERMES_GATEWAY") != "1":
        return None
    try:
        from gateway.session_context import get_session_env

        if get_session_env("HERMES_SESSION_INTERNAL", "") == "1":
            return None

        identity = {
            "platform": get_session_env("HERMES_SESSION_PLATFORM", ""),
            "scope_id": get_session_env("HERMES_SESSION_SCOPE_ID", ""),
            "chat_type": get_session_env("HERMES_SESSION_CHAT_TYPE", ""),
            "chat_id": (
                get_session_env("HERMES_SESSION_CHAT_ID_ALT", "")
                or get_session_env("HERMES_SESSION_CHAT_ID", "")
            ),
            "thread_id": get_session_env("HERMES_SESSION_THREAD_ID", ""),
            "user_id": (
                get_session_env("HERMES_SESSION_USER_ID_ALT", "")
                or get_session_env("HERMES_SESSION_USER_ID", "")
            ),
            "notifier_profile": get_session_env("HERMES_SESSION_PROFILE", ""),
            "session_key": get_session_env("HERMES_SESSION_KEY", ""),
        }
    except Exception:
        return None
    cleaned = _clean_identity(identity)
    return cleaned if all(cleaned[name] for name in _CONTROL_REQUIRED) else None


def decide_current_gateway_origin() -> dict[str, Any]:
    """Resolve the current request against the dispatcher-owned allowlist."""
    return decide_gateway_identity_current_config(current_gateway_identity())


def decide_gateway_identity_current_config(
    identity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Resolve an already-authenticated identity using current owner config."""
    try:
        config = _load_current_dispatcher_config()
    except Exception:
        return {
            "authorized": False,
            "validation_error": "dispatcher policy could not be read",
            "reason": "policy_unavailable",
        }
    return decide_gateway_origin(config, identity)


def match_gateway_identity_current_config(
    identity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Classify one source against the current owner allowlist, fail closed."""
    try:
        config = _load_current_dispatcher_config()
    except Exception:
        return {
            "matched": False,
            "candidate": False,
            "validation_error": "dispatcher policy could not be read",
            "reason": "policy_unavailable",
        }
    return match_short_task_allowed_source(config, identity)


def canonical_task_policy(
    raw_policy: Any,
    *,
    control_identity: Mapping[str, Any] | None = None,
) -> str:
    """Validate and canonicalize a frozen policy before durable storage.

    Empty input is retained as an unauthorised legacy/control-only binding.
    A non-empty malformed policy is rejected instead of being silently trusted.
    """
    if raw_policy is None or raw_policy == "":
        return ""
    if isinstance(raw_policy, str):
        try:
            policy = json.loads(raw_policy)
        except json.JSONDecodeError as exc:
            raise ValueError("short handoff task policy is invalid") from exc
    else:
        policy = raw_policy
    if not isinstance(policy, Mapping):
        raise ValueError("short handoff task policy must be a mapping")
    if policy.get("schema") != TASK_POLICY_SCHEMA or policy.get("authorized") is not True:
        raise ValueError("short handoff task policy is not authorized")

    origin = _clean_identity(policy.get("origin"))
    if any(not origin[name] for name in _CONTROL_REQUIRED):
        raise ValueError("short handoff task policy origin is incomplete")
    if control_identity is not None:
        expected = _clean_identity(control_identity)
        if any(origin[name] != expected[name] for name in _IDENTITY_FIELDS):
            raise ValueError("short handoff task policy origin does not match control binding")

    matched = policy.get("matched_origin")
    if (
        not isinstance(matched, Mapping)
        or any(name not in matched for name in _MATCH_REQUIRED)
        or any(
            origin.get(str(name)) != str(value)
            for name, value in matched.items()
        )
    ):
        raise ValueError("short handoff task policy allowlist proof is invalid")
    if any(str(name) not in set(_MATCH_REQUIRED + _MATCH_OPTIONAL) for name in matched):
        raise ValueError("short handoff task policy allowlist proof is invalid")

    worker = policy.get("worker_policy")
    if not isinstance(worker, Mapping):
        raise ValueError("short handoff worker policy is missing")
    if worker.get("schema") != 2 or worker.get("enabled") is not True:
        raise ValueError("short handoff worker policy is not enabled")
    for name in ("soft_iteration_limit", "max_handoffs", "max_iterations", "failure_limit"):
        value = worker.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"short handoff worker policy {name} is invalid")
    if worker["soft_iteration_limit"] < 2:
        raise ValueError("short handoff worker policy soft limit is invalid")
    if worker["soft_iteration_limit"] >= worker["max_iterations"]:
        raise ValueError("short handoff worker policy limits are inconsistent")
    if worker.get("validation_error"):
        raise ValueError("short handoff worker policy is invalid")
    roots = worker.get("allowed_workspace_roots")
    if not isinstance(roots, list) or not roots:
        raise ValueError("short handoff worker policy workspace roots are missing")
    canonical_roots: list[str] = []
    for index, root in enumerate(roots):
        if not isinstance(root, str) or not root.strip():
            raise ValueError(
                f"short handoff worker policy workspace root {index} is invalid"
            )
        candidate = Path(root.strip())
        if not candidate.is_absolute():
            raise ValueError("short handoff worker policy workspace root is not absolute")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                "short handoff worker policy workspace root is unavailable"
            ) from exc
        if not resolved.is_dir() or str(resolved) != root:
            raise ValueError("short handoff worker policy workspace root is not canonical")
        if resolved == Path(resolved.anchor):
            raise ValueError("short handoff worker policy workspace root is too broad")
        canonical_roots.append(str(resolved))
    if len(canonical_roots) != len(set(canonical_roots)):
        raise ValueError("short handoff worker policy workspace roots are duplicated")

    canonical = {
        "schema": TASK_POLICY_SCHEMA,
        "authorized": True,
        "origin": origin,
        "matched_origin": {str(k): str(v) for k, v in matched.items()},
        "worker_policy": dict(worker, allowed_workspace_roots=canonical_roots),
    }
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def worker_policy_from_task_policy(raw_policy: Any) -> dict[str, Any] | None:
    """Return the frozen worker snapshot, or ``None`` for legacy/invalid rows."""
    try:
        canonical = canonical_task_policy(raw_policy)
        if not canonical:
            return None
        return dict(json.loads(canonical)["worker_policy"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None


__all__ = [
    "TASK_POLICY_SCHEMA",
    "canonical_task_policy",
    "current_gateway_identity",
    "decide_current_gateway_origin",
    "decide_gateway_identity_current_config",
    "decide_gateway_origin",
    "match_gateway_identity_current_config",
    "match_short_task_allowed_source",
    "normalize_short_task_allowed_origins",
    "normalize_short_task_allowed_workspace_roots",
    "worker_policy_from_task_policy",
]
