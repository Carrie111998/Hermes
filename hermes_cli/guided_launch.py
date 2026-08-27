"""Replay-safe one-shot contracts for task-bound Dashboard chat launches.

The launch token is opaque and process-local.  It carries no authority on its
own until the authenticated Dashboard WebSocket presents every immutable
selector that was returned when the token was minted.  Consumption is atomic:
all failures burn the token, and a successful claim can seed exactly one TUI
startup turn.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Any, Dict, Tuple

MAX_TTL_SECONDS = 300
MAX_BRIEF_BYTES = 32_768
_REQUIRED_TEXT_FIELDS = (
    "conversation_id",
    "session_id",
    "board",
    "task_id",
    "lease_id",
    "approval_surface",
)

_lock = threading.Lock()
_launches: Dict[str, Tuple[int, Dict[str, Any]]] = {}


class GuidedLaunchInvalid(Exception):
    """Launch is malformed, unapproved, expired, tampered, or replayed."""


def _clean_required(name: str, value: str, *, limit: int = 1024) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise GuidedLaunchInvalid(f"{name} required")
    if len(clean.encode("utf-8")) > limit:
        raise GuidedLaunchInvalid(f"{name} too long")
    return clean


def _brief_digest(brief: str) -> str:
    return hashlib.sha256(brief.encode("utf-8")).hexdigest()


def mint_guided_launch(
    *,
    profile: str,
    conversation_id: str,
    session_id: str,
    board: str,
    task_id: str,
    brief: str,
    lease_id: str,
    approval_surface: str,
    approval_decision: str,
    approval_expires_at: int,
    lease_expires_at: int,
    expires_at: int,
) -> tuple[str, Dict[str, Any]]:
    """Mint an approved, bounded, default-profile launch.

    Denied/pending/timed-out approvals and stale leases never enter the store.
    The effective expiry is capped at five minutes and cannot outlive either
    the approval or the guided lease.
    """

    now = int(time.time())
    clean_profile = str(profile or "").strip()
    if clean_profile != "default":
        raise GuidedLaunchInvalid("guided launch profile must be default")

    decision = str(approval_decision or "").strip().lower()
    if decision != "approved":
        raise GuidedLaunchInvalid(f"approval {decision or 'missing'}")

    try:
        approval_deadline = int(approval_expires_at)
        lease_deadline = int(lease_expires_at)
        requested_deadline = int(expires_at)
    except (TypeError, ValueError) as exc:
        raise GuidedLaunchInvalid("expiry values must be integer timestamps") from exc

    if approval_deadline <= now:
        raise GuidedLaunchInvalid("approval timeout")
    if lease_deadline <= now:
        raise GuidedLaunchInvalid("lease expired")
    if requested_deadline <= now:
        raise GuidedLaunchInvalid("launch expired")

    clean: Dict[str, Any] = {"profile": clean_profile}
    supplied = {
        "conversation_id": conversation_id,
        "session_id": session_id,
        "board": board,
        "task_id": task_id,
        "lease_id": lease_id,
        "approval_surface": approval_surface,
    }
    for name in _REQUIRED_TEXT_FIELDS:
        clean[name] = _clean_required(name, supplied[name])

    clean_brief = str(brief or "").strip()
    if not clean_brief:
        raise GuidedLaunchInvalid("brief required")
    if len(clean_brief.encode("utf-8")) > MAX_BRIEF_BYTES:
        raise GuidedLaunchInvalid("brief too long")

    effective_expiry = min(
        requested_deadline,
        approval_deadline,
        lease_deadline,
        now + MAX_TTL_SECONDS,
    )
    clean.update(
        {
            "brief": clean_brief,
            "brief_sha256": _brief_digest(clean_brief),
            "approval_decision": "approved",
            "approval_expires_at": approval_deadline,
            "lease_expires_at": lease_deadline,
            "expires_at": effective_expiry,
            "minted_at": now,
        }
    )

    token = secrets.token_urlsafe(32)
    with _lock:
        _launches[token] = (effective_expiry, clean)
        _gc_expired_locked(now)
    return token, dict(clean)


def consume_guided_launch(
    token: str,
    *,
    profile: str,
    conversation_id: str,
    session_id: str,
    board: str,
    task_id: str,
    lease_id: str,
    brief_sha256: str,
) -> Dict[str, Any]:
    """Atomically consume and verify every presented immutable selector."""

    now = int(time.time())
    with _lock:
        entry = _launches.pop(str(token or ""), None)
    if entry is None:
        raise GuidedLaunchInvalid("unknown or replayed guided launch")

    expires_at, claim = entry
    if expires_at <= now or int(claim["lease_expires_at"]) <= now:
        raise GuidedLaunchInvalid("guided launch expired")
    if int(claim["approval_expires_at"]) <= now:
        raise GuidedLaunchInvalid("guided launch approval timeout")

    presented = {
        "profile": str(profile or ""),
        "conversation_id": str(conversation_id or ""),
        "session_id": str(session_id or ""),
        "board": str(board or ""),
        "task_id": str(task_id or ""),
        "lease_id": str(lease_id or ""),
        "brief_sha256": str(brief_sha256 or ""),
    }
    for name, value in presented.items():
        expected = str(claim[name])
        if not hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8")):
            raise GuidedLaunchInvalid(f"guided launch binding mismatch: {name}")

    return dict(claim)


def guided_launch_prompt(claim: Dict[str, Any]) -> str:
    """Render the immutable context as the one visible model-facing turn."""

    return "\n".join(
        [
            "Guided task launch. This is exactly one turn bound to the approved surface below.",
            f"Board: {claim['board']}",
            f"Task: {claim['task_id']}",
            f"Conversation: {claim['conversation_id']}",
            f"Session: {claim['session_id']}",
            f"Lease: {claim['lease_id']}",
            f"Approval surface: {claim['approval_surface']}",
            "Immutable brief:",
            str(claim["brief"]),
            "Preserve the approval boundary. Do not submit, publish, send, spend, or take any other external action.",
        ]
    )


def _gc_expired_locked(now: int | None = None) -> None:
    current = int(time.time()) if now is None else int(now)
    for token in [key for key, (expiry, _) in _launches.items() if expiry <= current]:
        _launches.pop(token, None)


def _reset_for_tests() -> None:
    with _lock:
        _launches.clear()


def _active_count_for_tests() -> int:
    with _lock:
        return len(_launches)
