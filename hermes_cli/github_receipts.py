"""Validation helpers for evidence-backed Kanban GitHub completion receipts.

The receipt is intentionally metadata-only.  It carries identifiers and the
result of an independent GitHub read-back, never credentials, command output,
prompts, or repository contents.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlparse


RECEIPT_SCHEMA = "aos.github_action_receipt.v1"
VALID_ACTIONS = {"push", "pr_create", "pr_update", "pr_merge", "comment"}
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubReceiptError(ValueError):
    """A bounded, machine-readable receipt validation failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _required_text(value: Any, field: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str):
        raise GitHubReceiptError(f"missing_{field}")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise GitHubReceiptError(f"invalid_{field}")
    return normalized


def _nullable_text(
    receipt: Mapping[str, Any], field: str, *, maximum: int = 500
) -> Optional[str]:
    """Validate one explicit-null attribution field.

    Presence is mandatory so producers cannot silently omit unknown context;
    ``null`` remains the honest value when the broker has no joinable fact.
    """
    if field not in receipt:
        raise GitHubReceiptError(f"missing_{field}")
    value = receipt.get(field)
    if value is None:
        return None
    return _required_text(value, field, maximum=maximum)


def _parse_utc(value: Any, field: str) -> datetime:
    text = _required_text(value, field, maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitHubReceiptError(f"invalid_{field}") from exc
    if parsed.tzinfo is None:
        raise GitHubReceiptError(f"invalid_{field}")
    return parsed.astimezone(timezone.utc)


def _normalize_repository(value: Any, field: str = "repository") -> str:
    repository = _required_text(value, field, maximum=200).removesuffix(".git")
    if not _REPOSITORY_RE.fullmatch(repository):
        raise GitHubReceiptError(f"invalid_{field}")
    return repository.casefold()


def repository_from_remote(remote: str) -> Optional[str]:
    """Return ``owner/repo`` for a GitHub remote, otherwise ``None``."""
    value = (remote or "").strip()
    if not value:
        return None
    if value.startswith("git@github.com:"):
        path = value.split(":", 1)[1]
    else:
        parsed = urlparse(value)
        if parsed.hostname != "github.com":
            return None
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").strip("/")
    return path.casefold() if _REPOSITORY_RE.fullmatch(path) else None


def git_expectations(workspace: Optional[str]) -> dict[str, Optional[str]]:
    """Read repository, branch, and exact HEAD from a local Git workspace."""
    if not workspace:
        return {"repository": None, "branch": None, "commit_sha": None}
    path = Path(workspace).expanduser()
    if not path.is_dir():
        return {"repository": None, "branch": None, "commit_sha": None}

    def run(*args: str) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(path), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() or None

    remote = run("config", "--get", "remote.origin.url")
    return {
        "repository": repository_from_remote(remote or ""),
        "branch": run("branch", "--show-current"),
        "commit_sha": run("rev-parse", "HEAD"),
    }


def validate_github_receipt(
    receipt: Any,
    *,
    task_id: str,
    expected_repository: Optional[str],
    expected_branch: Optional[str],
    expected_commit_sha: Optional[str],
    not_before_epoch: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Validate one exact, independently read-back GitHub effect receipt."""
    if not isinstance(receipt, Mapping):
        raise GitHubReceiptError("missing_receipt")
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise GitHubReceiptError("invalid_schema")

    receipt_id = _required_text(receipt.get("receipt_id"), "receipt_id", maximum=160)
    request_id = _required_text(receipt.get("request_id"), "request_id", maximum=160)
    action_id = _required_text(receipt.get("action_id"), "action_id", maximum=160)
    surface = _nullable_text(receipt, "surface", maximum=80)
    profile = _nullable_text(receipt, "profile", maximum=160)
    session_id = _nullable_text(receipt, "session_id", maximum=240)
    provider_slot = _nullable_text(receipt, "provider_slot", maximum=80)
    model = _nullable_text(receipt, "model", maximum=240)
    if _required_text(receipt.get("task_id"), "task_id", maximum=160) != task_id:
        raise GitHubReceiptError("wrong_task")
    if receipt.get("status") != "verified":
        raise GitHubReceiptError("not_verified")
    action = _required_text(receipt.get("action"), "action", maximum=32)
    if action not in VALID_ACTIONS:
        raise GitHubReceiptError("invalid_action")

    repository = _normalize_repository(receipt.get("repository"))
    branch = _required_text(receipt.get("branch"), "branch", maximum=240)
    commit_sha = _required_text(receipt.get("commit_sha"), "commit_sha", maximum=40).lower()
    if not _SHA_RE.fullmatch(commit_sha):
        raise GitHubReceiptError("invalid_commit_sha")

    created_at = _parse_utc(receipt.get("created_at"), "created_at")
    verified_at = _parse_utc(receipt.get("verified_at"), "verified_at")
    if verified_at < created_at:
        raise GitHubReceiptError("invalid_timestamp_order")
    current = now or datetime.now(timezone.utc)
    if verified_at > current.replace(microsecond=0) and (verified_at - current).total_seconds() > 300:
        raise GitHubReceiptError("future_receipt")
    if not_before_epoch is not None and verified_at.timestamp() < int(not_before_epoch):
        raise GitHubReceiptError("stale_receipt")

    if expected_repository is None:
        raise GitHubReceiptError("repository_unknown")
    if repository != expected_repository.casefold().removesuffix(".git"):
        raise GitHubReceiptError("wrong_repository")
    if expected_branch and branch != expected_branch:
        raise GitHubReceiptError("wrong_branch")
    if expected_commit_sha is None or commit_sha != expected_commit_sha.lower():
        raise GitHubReceiptError("wrong_head")

    readback = receipt.get("readback")
    if not isinstance(readback, Mapping) or readback.get("status") != "verified":
        raise GitHubReceiptError("missing_readback")
    if _normalize_repository(readback.get("repository"), "readback_repository") != repository:
        raise GitHubReceiptError("readback_repository_mismatch")
    if _required_text(readback.get("branch"), "readback_branch", maximum=240) != branch:
        raise GitHubReceiptError("readback_branch_mismatch")
    readback_sha = _required_text(
        readback.get("commit_sha"), "readback_commit_sha", maximum=40
    ).lower()
    if readback_sha != commit_sha:
        raise GitHubReceiptError("readback_head_mismatch")
    readback_verified_at = _parse_utc(
        readback.get("verified_at"), "readback_verified_at"
    )
    if not created_at <= readback_verified_at <= verified_at:
        raise GitHubReceiptError("invalid_readback_timestamp_order")

    pr_url = receipt.get("pr_url")
    if action.startswith("pr_"):
        parsed_pr = urlparse(_required_text(pr_url, "pr_url", maximum=500))
        expected_path = f"/{repository}/pull/"
        if parsed_pr.scheme != "https" or parsed_pr.hostname != "github.com" or not parsed_pr.path.casefold().startswith(expected_path):
            raise GitHubReceiptError("invalid_pr_url")
    elif pr_url is not None and not isinstance(pr_url, str):
        raise GitHubReceiptError("invalid_pr_url")

    effect_url = receipt.get("effect_url")
    if effect_url is not None:
        parsed_effect = urlparse(
            _required_text(effect_url, "effect_url", maximum=500)
        )
        expected_prefix = f"/{repository}/"
        if (
            parsed_effect.scheme != "https"
            or parsed_effect.hostname != "github.com"
            or not parsed_effect.path.casefold().startswith(expected_prefix)
        ):
            raise GitHubReceiptError("invalid_effect_url")
    if action in {"pr_create", "pr_update", "pr_merge", "comment"} and not effect_url:
        raise GitHubReceiptError("missing_effect_url")

    return {
        "schema": RECEIPT_SCHEMA,
        "receipt_id": receipt_id,
        "request_id": request_id,
        "action_id": action_id,
        "surface": surface,
        "profile": profile,
        "session_id": session_id,
        "task_id": task_id,
        "provider_slot": provider_slot,
        "model": model,
        "status": "verified",
        "action": action,
        "repository": repository,
        "branch": branch,
        "commit_sha": commit_sha,
        "pr_url": pr_url,
        "effect_url": effect_url,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "verified_at": verified_at.isoformat().replace("+00:00", "Z"),
        "readback": dict(readback),
    }
