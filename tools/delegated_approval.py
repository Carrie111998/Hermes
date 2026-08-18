"""In-process exact-once approvals for active delegated children.

Capabilities in this module are never serialized. Public ids are lookup hints;
authority is proved with live Python object identity at resolution time. Raw
commands stay in the blocking entry only and are never placed on an event or
routine audit payload.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import atexit
import hashlib
import secrets
import threading
import time
from typing import Any

_DEFAULT_EXPIRY_SECONDS = 90.0
_MAX_EXPIRY_SECONDS = 120.0
_EVENT_TEXT_LIMIT = 600
_CHOICES = frozenset({"once", "deny", "escalate_to_user"})
_ELIGIBLE_PATTERN_KEYS = frozenset({
    "script execution via -e/-c flag",
    "(python[23]?|perl|ruby|node)\\s+-[ec]\\s+",
})


def is_specialist_local_reversible(
    authority: "DelegatedApprovalAuthority",
    command: str,
    env_type: str,
    *,
    pattern_keys: list[str],
    tirith_findings: list[dict],
    has_host_access: bool = False,
) -> bool:
    """Admit only one narrow structured-scanner false-positive class.

    The dangerous-command scanner does not encode production/external/security
    consequence.  We therefore do not infer that consequence from command text.
    The trusted profile enables the feature, but exact-command attestation happens
    only after the dynamic command exists. Tirith must have no findings and every
    structured dangerous-pattern key must be the single inline-interpreter class
    (canonical key or its exact compatibility alias). Everything else stays with
    the user.
    """
    if env_type != "local" or has_host_access or not command.strip():
        return False
    if len(command.encode("utf-8", errors="surrogatepass")) > 8192:
        return False
    if tirith_findings or not pattern_keys:
        return False
    if not set(pattern_keys).issubset(_ELIGIBLE_PATTERN_KEYS):
        return False
    return True


@dataclass(frozen=True, slots=True)
class DelegatedApprovalAuthority:
    owner_agent: Any
    child_agent: Any
    subagent_id: str
    child_session_id: str
    parent_session_id: str
    owner_approval_session_key: str
    owner_session_id: str | None
    owner_transport: Any
    owner_session_record: Any
    delegation_id: str
    parent_lane_enabled: bool
    parent_task_id: str = ""
    delegated_goal: str = ""


@dataclass(slots=True)
class _PendingApproval:
    approval_id: str
    authority: DelegatedApprovalAuthority
    raw_command: str
    command_digest: str
    tool_call_id: str
    description: str
    pattern_keys: tuple[str, ...]
    created_monotonic: float
    expires_monotonic: float
    event: threading.Event = field(default_factory=threading.Event)
    choice: str | None = None
    terminal_reason: str | None = None


_lock = threading.RLock()
_pending: dict[str, _PendingApproval] = {}


def _digest(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8", errors="surrogatepass")).hexdigest()


def _redact_bounded(text: str) -> str:
    try:
        from agent.redact import redact_sensitive_text
        text = redact_sensitive_text(text, force=True)
    except Exception:
        return "[redaction unavailable]"
    text = " ".join(str(text).split())
    if len(text) > _EVENT_TEXT_LIMIT:
        return text[: _EVENT_TEXT_LIMIT - 1] + "…"
    return text


def _publish_parent_event(event: dict) -> None:
    from tools.process_registry import process_registry
    process_registry.completion_queue.put(event)


def _audit(entry: _PendingApproval, decision: str, decided_by: str, reason: str) -> None:
    """Emit redacted lifecycle evidence through the existing approval hooks."""
    try:
        from tools.approval import _fire_approval_hook
        hook = "pre_approval_request" if decision == "requested" else "post_approval_response"
        payload = {
            "approval_id": entry.approval_id,
            "command": _redact_bounded(entry.raw_command),
            "command_digest": entry.command_digest,
            "description": _redact_bounded(entry.description),
            "pattern_keys": list(entry.pattern_keys),
            "surface": "delegated_parent",
            "parent_session_id": entry.authority.parent_session_id,
            "child_session_id": entry.authority.child_session_id,
            "subagent_id": entry.authority.subagent_id,
            "delegation_id": entry.authority.delegation_id,
            "choice": decision,
            "decided_by": decided_by,
            "reason": reason,
        }
        _fire_approval_hook(hook, **payload)
    except Exception:
        return


def _active_authority_matches(authority: DelegatedApprovalAuthority) -> bool:
    if not authority.parent_lane_enabled:
        return False
    try:
        from tools.delegate_tool import _active_subagents, _active_subagents_lock
        with _active_subagents_lock:
            record = _active_subagents.get(authority.subagent_id)
            if (
                record is None
                or record.get("agent") is not authority.child_agent
                or record.get("owner_agent") is not authority.owner_agent
                or record.get("approval_authority") is not authority
            ):
                return False
            if authority.owner_session_id:
                if (
                    record.get("owner_session_id") != authority.owner_session_id
                    or record.get("owner_transport") is not authority.owner_transport
                    or record.get("owner_session_record") is not authority.owner_session_record
                ):
                    return False
    except Exception:
        return False
    if authority.owner_session_id:
        try:
            from tui_gateway.server import _current_session_steer_authority
            transport, session_record = _current_session_steer_authority(
                authority.owner_session_id
            )
        except Exception:
            return False
        if (
            transport is not authority.owner_transport
            or session_record is not authority.owner_session_record
        ):
            return False
    return True


def await_parent_decision(
    *,
    command: str,
    description: str,
    pattern_keys: list[str],
    tool_call_id: str,
    timeout: float = _DEFAULT_EXPIRY_SECONDS,
) -> dict:
    from agent.delegation_context import get_delegated_approval_authority

    authority = get_delegated_approval_authority()
    if not isinstance(authority, DelegatedApprovalAuthority):
        return {"resolved": False, "choice": None, "ineligible": True}
    if not tool_call_id or not _active_authority_matches(authority):
        return {"resolved": False, "choice": None, "ineligible": True}

    now = time.monotonic()
    bounded_timeout = min(max(float(timeout), 0.0), _MAX_EXPIRY_SECONDS)
    entry = _PendingApproval(
        approval_id=secrets.token_urlsafe(24),
        authority=authority,
        raw_command=command,
        command_digest=_digest(command),
        tool_call_id=tool_call_id,
        description=description,
        pattern_keys=tuple(pattern_keys),
        created_monotonic=now,
        expires_monotonic=now + bounded_timeout,
    )
    with _lock:
        _pending[entry.approval_id] = entry
    event = {
        "type": "delegated_approval_request",
        "approval_id": entry.approval_id,
        "delegation_id": authority.delegation_id,
        "subagent_id": authority.subagent_id,
        "child_session_id": authority.child_session_id,
        "parent_session_id": authority.parent_session_id,
        "session_key": authority.owner_approval_session_key,
        "origin_ui_session_id": authority.owner_session_id or "",
        "command": _redact_bounded(command),
        "description": _redact_bounded(description),
        "command_digest": entry.command_digest,
        "tool_call_id": entry.tool_call_id,
        "pattern_keys": list(entry.pattern_keys),
        "choices": ["once", "deny", "escalate_to_user"],
        "expires_in_seconds": bounded_timeout,
        "untrusted_data": True,
        "system_authored": True,
        "parent_task_id": _redact_bounded(authority.parent_task_id),
        "delegated_goal": _redact_bounded(authority.delegated_goal),
    }
    _audit(entry, "requested", "system", "eligible_local_reversible")
    try:
        _publish_parent_event(event)
    except Exception:
        with _lock:
            _pending.pop(entry.approval_id, None)
        _audit(entry, "revoked", "system", "parent_event_publish_failed")
        return {"resolved": False, "choice": None, "notify_failed": True}

    deadline = now + bounded_timeout
    revoked = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or entry.event.wait(timeout=min(0.1, remaining)):
            break
        if not _active_authority_matches(authority):
            with _lock:
                if _pending.pop(entry.approval_id, None) is entry:
                    entry.choice = "deny"
                    entry.terminal_reason = "authority_replaced"
                    revoked = True
            if revoked:
                entry.event.set()
                _audit(entry, "revoked", "system", "authority_replaced")
            break
    with _lock:
        _pending.pop(entry.approval_id, None)
        choice = entry.choice
    if choice is None:
        entry.terminal_reason = "expired"
        _audit(entry, "expired", "timeout", "monotonic_expiry")
        return {"resolved": False, "choice": None, "expired": True}
    return {"resolved": True, "choice": choice}


def resolve_parent_decision(parent_agent: Any, approval_id: str, choice: str) -> dict:
    """Consume one exact request, returning the same generic refusal on failure."""
    refusal = {"resolved": False, "status": "unavailable"}
    if choice not in _CHOICES or not isinstance(approval_id, str):
        return refusal
    with _lock:
        entry = _pending.get(approval_id)
        if entry is None:
            return refusal
        now = time.monotonic()
        if (
            parent_agent is not entry.authority.owner_agent
            or now >= entry.expires_monotonic
            or entry.choice is not None
            or entry.command_digest != _digest(entry.raw_command)
            or not entry.tool_call_id
            or not _active_authority_matches(entry.authority)
        ):
            return refusal
        _pending.pop(approval_id, None)
        entry.choice = choice
        entry.terminal_reason = "parent_decision"
    entry.event.set()
    _audit(entry, choice, "parent_agent", "exact_capability_consumed")
    return {"resolved": True, "choice": choice}


def revoke_for_child(child_agent: Any, reason: str = "child_completed") -> int:
    with _lock:
        targets = [e for e in _pending.values() if e.authority.child_agent is child_agent]
        for entry in targets:
            _pending.pop(entry.approval_id, None)
            entry.choice = "deny"
            entry.terminal_reason = reason
    for entry in targets:
        entry.event.set()
        _audit(entry, "revoked", "system", reason)
    return len(targets)


def revoke_for_parent_session(parent_session_id: str, reason: str = "parent_reset") -> int:
    with _lock:
        targets = [
            e for e in _pending.values()
            if (
                e.authority.parent_session_id == parent_session_id
                or e.authority.owner_approval_session_key == parent_session_id
            )
        ]
        for entry in targets:
            _pending.pop(entry.approval_id, None)
            entry.choice = "deny"
            entry.terminal_reason = reason
    for entry in targets:
        entry.event.set()
        _audit(entry, "revoked", "system", reason)
    return len(targets)


def revoke_all(reason: str = "process_exit") -> int:
    with _lock:
        targets = list(_pending.values())
        _pending.clear()
        for entry in targets:
            entry.choice = "deny"
            entry.terminal_reason = reason
    for entry in targets:
        entry.event.set()
        _audit(entry, "revoked", "system", reason)
    return len(targets)


atexit.register(revoke_all)


def pending_requests() -> list[dict]:
    """Test/diagnostic snapshot; deliberately excludes raw command and authority."""
    with _lock:
        return [
            {
                "approval_id": e.approval_id,
                "subagent_id": e.authority.subagent_id,
                "command_digest": e.command_digest,
                "tool_call_id": e.tool_call_id,
            }
            for e in _pending.values()
        ]
