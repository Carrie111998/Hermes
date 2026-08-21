"""In-process exact-once approvals for active delegated children.

Capabilities are never serialized. Public ids are lookup hints; authority is
proved with live Python object identity. Raw commands and exact tool-call
bindings remain only in frozen process-memory records. Same-process arbitrary
memory corruption is outside this boundary; normal field mutation is closed.
"""
from __future__ import annotations

import atexit
import ast
from dataclasses import dataclass, field
import hashlib
import json
import math
import operator
import re
import secrets
import shlex
import threading
import time
from typing import Any

_DEFAULT_EXPIRY_SECONDS = 90.0
_MAX_EXPIRY_SECONDS = 120.0
_EVENT_TEXT_LIMIT = 600
_IDENTITY_TEXT_LIMIT = 128
_TOOL_CALL_ID_LIMIT = 256
MAX_SERIALIZED_EVENT_BYTES = 4096
MAX_SERIALIZED_MESSAGE_BYTES = 4096
_CHOICES = frozenset({"once", "deny", "escalate_to_user"})
_ELIGIBLE_PATTERN_KEYS = frozenset({
    "script execution via -e/-c flag",
    "(python[23]?|perl|ruby|node)\\s+-[ec]\\s+",
})
_PYTHON_INTERPRETER = re.compile(r"python(?:3(?:\.\d+)?)?", re.ASCII)
_INLINE_INTERPRETERS = frozenset({"perl", "ruby", "node"})
_MAX_AST_NODES = 64
_MAX_SAFE_VALUE_BYTES = 8192
_MAX_SAFE_NUMBER = 1_000_000_000_000


def _strict_utf8_bytes(value: str) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeError:
        return None


def _bounded_safe_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_NUMBER:
            raise ValueError("number outside safe bound")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > _MAX_SAFE_NUMBER:
            raise ValueError("number outside safe bound")
        return value
    if isinstance(value, str):
        data = _strict_utf8_bytes(value)
        if data is None or len(data) > _MAX_SAFE_VALUE_BYTES:
            raise ValueError("string outside safe bound")
        return value
    if isinstance(value, (tuple, list, set)):
        result = type(value)(_bounded_safe_value(item) for item in value)
    elif isinstance(value, dict):
        result = {
            _bounded_safe_value(key): _bounded_safe_value(item)
            for key, item in value.items()
        }
    else:
        raise ValueError("unsupported value")
    if len(repr(result).encode("utf-8", errors="strict")) > _MAX_SAFE_VALUE_BYTES:
        raise ValueError("container outside safe bound")
    return result


_SAFE_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_SAFE_UNARY_OPERATORS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
}
_SAFE_COMPARE_OPERATORS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def _evaluate_safe_expression(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        return _bounded_safe_value(node.value)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = [_evaluate_safe_expression(item) for item in node.elts]
        constructor = {ast.List: list, ast.Tuple: tuple, ast.Set: set}[type(node)]
        return _bounded_safe_value(constructor(values))
    if isinstance(node, ast.Dict):
        if any(key is None for key in node.keys):
            raise ValueError("dictionary unpacking is not safe")
        return _bounded_safe_value({
            _evaluate_safe_expression(key): _evaluate_safe_expression(value)
            for key, value in zip(node.keys, node.values)
        })
    if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_BINARY_OPERATORS:
        left = _evaluate_safe_expression(node.left)
        right = _evaluate_safe_expression(node.right)
        return _bounded_safe_value(_SAFE_BINARY_OPERATORS[type(node.op)](left, right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_UNARY_OPERATORS:
        return _bounded_safe_value(
            _SAFE_UNARY_OPERATORS[type(node.op)](_evaluate_safe_expression(node.operand))
        )
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        values = [_evaluate_safe_expression(value) for value in node.values]
        return _bounded_safe_value(all(values) if isinstance(node.op, ast.And) else any(values))
    if isinstance(node, ast.Compare):
        left = _evaluate_safe_expression(node.left)
        for op_node, comparator in zip(node.ops, node.comparators):
            operation = _SAFE_COMPARE_OPERATORS.get(type(op_node))
            if operation is None:
                raise ValueError("comparison is not safe")
            right = _evaluate_safe_expression(comparator)
            if not operation(left, right):
                return False
            left = right
        return True
    raise ValueError(f"unsupported AST node: {type(node).__name__}")


def _is_closed_python_inline_command(command: str) -> bool:
    command_bytes = _strict_utf8_bytes(command)
    if command_bytes is None or not 1 <= len(command_bytes) <= 8192:
        return False
    try:
        tokens = shlex.split(command, posix=True)
    except (TypeError, ValueError):
        return False
    if len(tokens) != 3 or tokens[1] != "-c" or not _PYTHON_INTERPRETER.fullmatch(tokens[0]):
        return False
    payload = tokens[2]
    payload_bytes = _strict_utf8_bytes(payload)
    if payload_bytes is None or not payload_bytes or len(payload_bytes) > 8192:
        return False
    try:
        tree = ast.parse(payload, mode="exec")
        if sum(1 for _ in ast.walk(tree)) > _MAX_AST_NODES or len(tree.body) != 1:
            return False
        statement = tree.body[0]
        if not isinstance(statement, ast.Expr):
            return False
        expression = statement.value
        if isinstance(expression, ast.Call):
            if (
                not isinstance(expression.func, ast.Name)
                or expression.func.id != "print"
                or expression.keywords
            ):
                return False
            for argument in expression.args:
                _evaluate_safe_expression(argument)
            return True
        _evaluate_safe_expression(expression)
        return True
    except (ArithmeticError, MemoryError, RecursionError, SyntaxError, TypeError, ValueError):
        return False


def requires_delegated_inline_review(command: str) -> bool:
    """Recognize inline-code forms that must not bypass delegated review."""
    data = _strict_utf8_bytes(command)
    if data is None or not 1 <= len(data) <= 8192:
        return False
    try:
        tokens = shlex.split(command, posix=True)
    except (TypeError, ValueError):
        return False
    has_interpreter = any(
        _PYTHON_INTERPRETER.fullmatch(token) or token in _INLINE_INTERPRETERS
        for token in tokens
    )
    return has_interpreter and any(token in {"-c", "-e"} for token in tokens)


def is_specialist_local_reversible(
    authority: "DelegatedApprovalAuthority",
    command: str,
    env_type: str,
    *,
    pattern_keys: list[str],
    tirith_findings: list[dict],
    has_host_access: bool = False,
) -> bool:
    """Admit only a closed Python ``-c`` expression/print subset."""
    if env_type != "local" or has_host_access:
        return False
    if tirith_findings or not pattern_keys:
        return False
    if not set(pattern_keys).issubset(_ELIGIBLE_PATTERN_KEYS):
        return False
    return _is_closed_python_inline_command(command)


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


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    raw_command: str
    command_digest: str
    tool_call_id: str


def _digest(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8", errors="strict")).hexdigest()


def capture_request_identity(command: str, tool_call_id: str) -> RequestIdentity:
    """Capture the current child guard identity; the parent never supplies it."""
    return RequestIdentity(command, _digest(command), tool_call_id)


@dataclass(frozen=True, slots=True)
class _ApprovalBinding:
    authority: DelegatedApprovalAuthority
    raw_command: str
    command_digest: str
    tool_call_id: str
    request_identity: RequestIdentity
    description: str
    pattern_keys: tuple[str, ...]


@dataclass(slots=True)
class _DecisionState:
    event: threading.Event = field(default_factory=threading.Event)
    choice: str | None = None
    terminal_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _PendingApproval:
    approval_id: str
    binding: _ApprovalBinding
    created_monotonic: float
    expires_monotonic: float
    state: _DecisionState = field(default_factory=_DecisionState)

    @property
    def authority(self) -> DelegatedApprovalAuthority:
        return self.binding.authority

    @property
    def raw_command(self) -> str:
        return self.binding.raw_command

    @property
    def command_digest(self) -> str:
        return self.binding.command_digest

    @property
    def tool_call_id(self) -> str:
        return self.binding.tool_call_id

    @property
    def description(self) -> str:
        return self.binding.description

    @property
    def pattern_keys(self) -> tuple[str, ...]:
        return self.binding.pattern_keys

    @property
    def event(self) -> threading.Event:
        return self.state.event

    @property
    def choice(self) -> str | None:
        return self.state.choice

    @property
    def terminal_reason(self) -> str | None:
        return self.state.terminal_reason


_lock = threading.RLock()
_pending: dict[str, _PendingApproval] = {}


def _valid_identity(value: Any, *, allow_empty: bool = False, limit: int = _IDENTITY_TEXT_LIMIT) -> bool:
    data = _strict_utf8_bytes(value) if isinstance(value, str) else None
    return data is not None and (allow_empty or bool(data)) and len(data) <= limit


def _bounded_identity_display(value: Any, limit: int = _IDENTITY_TEXT_LIMIT) -> str:
    if not isinstance(value, str) or _strict_utf8_bytes(value) is None:
        return "[invalid]"
    clean = "".join(ch for ch in value if ch.isprintable())
    encoded = clean.encode("utf-8")
    if len(encoded) <= limit:
        return clean
    return encoded[: limit - 3].decode("utf-8", errors="ignore") + "..."


def _redact_bounded(text: Any) -> str:
    if _strict_utf8_bytes(str(text)) is None:
        return "[invalid]"
    try:
        from agent.redact import redact_sensitive_text
        text = redact_sensitive_text(str(text), force=True)
    except Exception:
        return "[redaction unavailable]"
    text = " ".join(str(text).split())
    if _strict_utf8_bytes(text) is None:
        return "[invalid]"
    while len(text.encode("utf-8", errors="replace")) > _EVENT_TEXT_LIMIT:
        text = text[:-1]
    return text


def _authority_publication_valid(authority: DelegatedApprovalAuthority) -> bool:
    required = (
        authority.subagent_id,
        authority.child_session_id,
        authority.parent_session_id,
        authority.owner_approval_session_key,
        authority.delegation_id,
    )
    return all(_valid_identity(value) for value in required) and _valid_identity(
        authority.owner_session_id or "", allow_empty=True
    )


def _event_serialized_size(event: dict) -> int:
    return len(json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _publish_parent_event(event: dict) -> None:
    if _event_serialized_size(event) > MAX_SERIALIZED_EVENT_BYTES:
        raise ValueError("delegated approval event exceeds aggregate bound")
    from tools.process_registry import process_registry
    process_registry.completion_queue.put(event)


def _audit(entry: _PendingApproval, decision: str, decided_by: str, reason: str) -> None:
    """Emit a fully re-bounded, redacted lifecycle record."""
    try:
        from tools.approval import _fire_approval_hook
        hook = "pre_approval_request" if decision == "requested" else "post_approval_response"
        payload = {
            "approval_id": _bounded_identity_display(entry.approval_id),
            "command": _redact_bounded(entry.raw_command),
            "command_digest": _bounded_identity_display(entry.command_digest, 64),
            "description": _redact_bounded(entry.description),
            "pattern_keys": [_bounded_identity_display(key, 120) for key in entry.pattern_keys[:4]],
            "surface": "delegated_parent",
            "parent_session_id": _bounded_identity_display(entry.authority.parent_session_id),
            "child_session_id": _bounded_identity_display(entry.authority.child_session_id),
            "subagent_id": _bounded_identity_display(entry.authority.subagent_id),
            "delegation_id": _bounded_identity_display(entry.authority.delegation_id),
            "choice": _bounded_identity_display(decision, 32),
            "decided_by": _bounded_identity_display(decided_by, 32),
            "reason": _bounded_identity_display(reason, 64),
        }
        if _event_serialized_size(payload) <= MAX_SERIALIZED_EVENT_BYTES:
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
            if authority.owner_session_id and (
                record.get("owner_session_id") != authority.owner_session_id
                or record.get("owner_transport") is not authority.owner_transport
                or record.get("owner_session_record") is not authority.owner_session_record
            ):
                return False
    except Exception:
        return False
    if authority.owner_session_id:
        try:
            from tui_gateway.server import _session_generation_matches
        except Exception:
            return False
        if not _session_generation_matches(
            authority.owner_session_id,
            authority.owner_transport,
            authority.owner_session_record,
            authority.owner_agent,
        ):
            return False
    return True


def await_parent_decision(
    *,
    command: str,
    description: str,
    pattern_keys: list[str],
    request_identity: RequestIdentity,
    timeout: float = _DEFAULT_EXPIRY_SECONDS,
) -> dict:
    from agent.delegation_context import get_delegated_approval_authority

    authority = get_delegated_approval_authority()
    identity_matches = (
        isinstance(request_identity, RequestIdentity)
        and request_identity.raw_command == command
        and request_identity.command_digest == _digest(command)
        and _valid_identity(request_identity.tool_call_id, limit=_TOOL_CALL_ID_LIMIT)
    )
    if (
        not isinstance(authority, DelegatedApprovalAuthority)
        or not identity_matches
        or not _authority_publication_valid(authority)
        or not _active_authority_matches(authority)
    ):
        return {"resolved": False, "choice": None, "ineligible": True}

    now = time.monotonic()
    bounded_timeout = min(max(float(timeout), 0.0), _MAX_EXPIRY_SECONDS)
    binding = _ApprovalBinding(
        authority=authority,
        raw_command=command,
        command_digest=request_identity.command_digest,
        tool_call_id=request_identity.tool_call_id,
        request_identity=request_identity,
        description=description,
        pattern_keys=tuple(pattern_keys),
    )
    entry = _PendingApproval(
        approval_id=secrets.token_urlsafe(24),
        binding=binding,
        created_monotonic=now,
        expires_monotonic=now + bounded_timeout,
    )
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
        "pattern_keys": [_bounded_identity_display(key, 120) for key in entry.pattern_keys[:4]],
        "choices": ["once", "deny", "escalate_to_user"],
        "expires_in_seconds": bounded_timeout,
        "untrusted_data": True,
        "system_authored": True,
        "parent_task_id": _redact_bounded(authority.parent_task_id),
        "delegated_goal": _redact_bounded(authority.delegated_goal),
    }
    if _event_serialized_size(event) > MAX_SERIALIZED_EVENT_BYTES:
        return {"resolved": False, "choice": None, "ineligible": True}
    with _lock:
        _pending[entry.approval_id] = entry
    _audit(entry, "requested", "system", "eligible_closed_python_inline")
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
                    entry.state.choice = "deny"
                    entry.state.terminal_reason = "authority_replaced"
                    revoked = True
            if revoked:
                entry.event.set()
                _audit(entry, "revoked", "system", "authority_replaced")
            break
    with _lock:
        _pending.pop(entry.approval_id, None)
        choice = entry.choice
    if choice is None:
        entry.state.terminal_reason = "expired"
        _audit(entry, "expired", "timeout", "monotonic_expiry")
        return {"resolved": False, "choice": None, "expired": True}
    return {"resolved": True, "choice": choice}


def resolve_parent_decision(parent_agent: Any, approval_id: str, choice: str) -> dict:
    """Consume one exact request; the parent cannot supply a tool-call id."""
    refusal = {"resolved": False, "status": "unavailable"}
    if choice not in _CHOICES or not _valid_identity(approval_id):
        return refusal
    with _lock:
        entry = _pending.get(approval_id)
        if entry is None:
            return refusal
        identity = entry.binding.request_identity
        if (
            parent_agent is not entry.authority.owner_agent
            or time.monotonic() >= entry.expires_monotonic
            or entry.choice is not None
            or identity.raw_command != entry.raw_command
            or identity.command_digest != entry.command_digest
            or identity.tool_call_id != entry.tool_call_id
            or entry.command_digest != _digest(entry.raw_command)
            or not _active_authority_matches(entry.authority)
        ):
            return refusal
        _pending.pop(approval_id, None)
        entry.state.choice = choice
        entry.state.terminal_reason = "parent_decision"
    entry.event.set()
    _audit(entry, choice, "parent_agent", "exact_capability_consumed")
    return {"resolved": True, "choice": choice}


def _revoke_targets(targets: list[_PendingApproval], reason: str) -> int:
    for entry in targets:
        entry.state.choice = "deny"
        entry.state.terminal_reason = reason
    for entry in targets:
        entry.event.set()
        _audit(entry, "revoked", "system", reason)
    return len(targets)


def revoke_for_child(child_agent: Any, reason: str = "child_completed") -> int:
    with _lock:
        targets = [entry for entry in _pending.values() if entry.authority.child_agent is child_agent]
        for entry in targets:
            _pending.pop(entry.approval_id, None)
    return _revoke_targets(targets, reason)


def revoke_for_parent_session(parent_session_id: str, reason: str = "parent_reset") -> int:
    with _lock:
        targets = [
            entry for entry in _pending.values()
            if entry.authority.parent_session_id == parent_session_id
            or entry.authority.owner_approval_session_key == parent_session_id
        ]
        for entry in targets:
            _pending.pop(entry.approval_id, None)
    return _revoke_targets(targets, reason)


def revoke_all(reason: str = "process_exit") -> int:
    with _lock:
        targets = list(_pending.values())
        _pending.clear()
    return _revoke_targets(targets, reason)


atexit.register(revoke_all)


def pending_requests() -> list[dict]:
    """Diagnostic snapshot excludes raw command and exact tool-call identity."""
    with _lock:
        return [
            {
                "approval_id": entry.approval_id,
                "subagent_id": entry.authority.subagent_id,
                "command_digest": entry.command_digest,
            }
            for entry in _pending.values()
        ]
