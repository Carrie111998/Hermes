"""Explicit browser-safe transformations for DevFlow projection rows."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from urllib.parse import urlparse


_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_EMBEDDED_LOCAL_PATH = re.compile(
    r"(?:(?<![A-Za-z])[A-Za-z]:[\\/]|/(?:home|users|tmp|var/tmp)/|[/\\]\.hermes[/\\])",
    re.IGNORECASE,
)
_SENSITIVE_TEXT = re.compile(
    r"(?:\b(?:credential|password|prompt|provider|secret|token)\b|"
    r"(?:ghp|github_pat|sk-proj|xox[baprs])_[A-Za-z0-9_-]+|"
    r"\b(?:anthropic|openai|google|mistral|cohere)/[A-Za-z0-9._/-]+)",
    re.IGNORECASE,
)
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")
_SAFE_REFERENCE = re.compile(r"^[A-Za-z0-9._:/#@+-]{1,500}$")
_SAFE_PR_HOSTS = frozenset({"github.com", "www.github.com"})
_SAFE_PR_PATH = re.compile(r"^/[^/]+/[^/]+/pull/[1-9][0-9]*$")


def _copy_present(row: Mapping[str, object], *keys: str) -> dict[str, object]:
    return {key: row[key] for key in keys if row.get(key) is not None}


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_short_text(value: object, *, limit: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if (
        not text
        or len(text) > limit
        or _is_local_path(text)
        or _EMBEDDED_LOCAL_PATH.search(text)
        or _SENSITIVE_TEXT.search(text)
    ):
        return None
    return text


def _safe_reference(value: object) -> str | None:
    text = _safe_short_text(value)
    if text is None or _SAFE_REFERENCE.fullmatch(text) is None:
        return None
    return text


def _is_local_path(value: str) -> bool:
    text = value.strip()
    return bool(
        _WINDOWS_ABSOLUTE_PATH.match(text)
        or text.startswith(("/", "\\\\", "file:", "~\\", "~/"))
    )


def _actor_class(actor: object) -> str:
    value = str(actor or "").lower()
    if any(token in value for token in ("admin", "operator", "human", "user")):
        return "operator"
    if "triage" in value:
        return "triage"
    if any(token in value for token in ("executor", "builder", "worker")):
        return "executor"
    return "system"


def public_request_summary(row: Mapping[str, object]) -> dict[str, object]:
    summary = _copy_present(
        row,
        "request_id",
        "idempotency_key",
        "state",
        "source_agent",
        "source_kind",
        "target_repo",
        "target_subsystem",
        "kind",
        "severity",
        "created_at",
        "updated_at",
        "lease_attempt_count",
    )
    terminal_reason = _safe_short_text(row.get("terminal_reason"), limit=300)
    if terminal_reason is not None:
        summary["terminal_reason"] = terminal_reason
    envelope = _json_object(row.get("envelope_json"))
    for key in ("title", "priority"):
        value = _safe_short_text(envelope.get(key), limit=300)
        if value is not None:
            summary[key] = value
    return summary


def public_transition_summary(row: Mapping[str, object]) -> dict[str, object]:
    summary = _copy_present(
        row,
        "request_id",
        "from_state",
        "to_state",
        "policy_version",
        "created_at",
    )
    if row.get("id") is not None:
        summary["transition_id"] = row["id"]
    summary["actor_class"] = _actor_class(row.get("actor"))
    evidence_ref = _safe_reference(row.get("evidence_ref"))
    if evidence_ref is not None:
        summary["evidence_ref"] = evidence_ref
    return summary


def public_evidence_summary(row: Mapping[str, object]) -> dict[str, object]:
    summary = _copy_present(row, "request_id", "created_at")
    if row.get("id") is not None:
        summary["evidence_id"] = row["id"]
    evidence = _json_object(row.get("evidence_json"))
    for key in ("kind", "summary"):
        value = _safe_short_text(evidence.get(key))
        if value is not None:
            summary[key] = value
    ref = _safe_reference(evidence.get("ref"))
    if ref is not None:
        summary["ref"] = ref
    return summary


def public_decision_summary(row: Mapping[str, object]) -> dict[str, object]:
    summary = _copy_present(row, "request_id", "decision", "created_at")
    if row.get("id") is not None:
        summary["decision_id"] = row["id"]
    summary["actor_class"] = "operator"
    evidence_ref = _safe_reference(row.get("evidence_ref"))
    if evidence_ref is not None:
        summary["evidence_ref"] = evidence_ref
    return summary


def public_lease_summary(row: Mapping[str, object]) -> dict[str, object]:
    summary = _copy_present(
        row,
        "request_id",
        "acquired_at",
        "expires_at",
        "heartbeat_at",
        "attempt_count",
    )
    branch = _safe_short_text(row.get("branch"), limit=255)
    if branch is not None and _SAFE_BRANCH.fullmatch(branch) is not None:
        summary["branch"] = branch
    return summary


def public_artifact_summary(row: Mapping[str, object]) -> dict[str, object] | None:
    kind = str(row.get("kind") or "").lower()
    ref = _safe_short_text(row.get("ref"))
    if ref is None or kind in {"worktree", "script", "script_path", "local_path"}:
        return None

    summary = _copy_present(row, "request_id", "created_at")
    if row.get("id") is not None:
        summary["artifact_id"] = row["id"]
    summary["kind"] = kind

    if kind == "branch" and _SAFE_BRANCH.fullmatch(ref) is not None:
        summary["branch"] = ref
    elif kind == "pr":
        parsed = urlparse(ref)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _SAFE_PR_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or _SAFE_PR_PATH.fullmatch(parsed.path) is None
        ):
            return None
        summary["pr_url"] = ref
    elif kind == "pr_number" and ref.isdigit():
        summary["pr_number"] = int(ref)
    elif kind in {"validation", "autonomy_gate"}:
        safe_ref = _safe_reference(ref)
        if safe_ref is None:
            return None
        summary["ref"] = safe_ref
    else:
        return None
    return summary
