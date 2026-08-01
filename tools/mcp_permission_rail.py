"""Config-driven MCP prepare/execute permission rail.

The rail is generic: config names exact MCP server/tool patterns and result
paths. Unconfigured servers/tools keep normal MCP behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MAX_PREVIEW_CHARS = 3000
_HARD_MAX_PREVIEW_CHARS = 3000


@dataclass(frozen=True)
class MCPRailPolicy:
    server_name: str
    ttl_seconds: float
    max_backend_expiry_seconds: float
    max_pending_global: int
    max_pending_per_session: int
    max_preview_chars: int
    prepare_patterns: tuple[str, ...]
    execute_patterns: tuple[str, ...]
    status_patterns: tuple[str, ...]
    read_patterns: tuple[str, ...]
    bindings: tuple[tuple[str, str], ...]
    prepare_prefix: str
    execute_prefix: str
    deny_execute_patterns: tuple[str, ...]
    token_result_paths: tuple[str, ...]
    preview_result_paths: tuple[str, ...]
    expires_at_result_paths: tuple[str, ...]
    confirmation_code_result_paths: tuple[str, ...]
    execute_token_paths: tuple[str, ...]
    execute_confirmation_code_paths: tuple[str, ...]
    attestation: Any = None
    invalid_reason: str = ""


@dataclass(frozen=True)
class MCPRailDecision:
    policy: Optional[MCPRailPolicy]
    kind: str = "unprotected"
    prepare_tool: str = ""
    prepare_execute_tool: str = ""
    approved: bool = False
    denied_message: str = ""
    pending: Optional["PendingProposal"] = None
    call_args: Optional[Mapping[str, Any]] = None
    mcp_meta: Optional[Mapping[str, Any]] = None

    @property
    def protected_execute(self) -> bool:
        return self.kind == "execute" and self.policy is not None


@dataclass(frozen=True)
class PendingProposal:
    server_name: str
    prepare_tool: str
    execute_tool: str
    proposal_token_digest: str
    confirmation_code_digest: str
    preview_digest: str
    canonical_result_digest: str
    proposal_digest: str
    proposal_payload_digest: str
    preview: str
    expires_at: float
    context: Mapping[str, str]


@dataclass(frozen=True)
class _ExtractedProposal:
    proposal_token_digest: str
    confirmation_code_digest: str
    preview_digest: str
    canonical_result_digest: str
    proposal_digest: str
    proposal_payload_digest: str
    preview: str
    backend_expires_at_wall: float


_pending_lock = threading.Lock()
_pending: list[PendingProposal] = []
_known_protected_servers: set[str] = set()


def clear_pending_proposals() -> None:
    """Clear in-memory proposals. Intended for tests and session-bound cleanup."""
    with _pending_lock:
        _pending.clear()


def clear_session_pending_proposals(session_key: str) -> None:
    """Drop pending proposals for a finished session."""
    if not session_key:
        return
    with _pending_lock:
        _pending[:] = [
            proposal
            for proposal in _pending
            if proposal.context.get("session_key") != session_key
        ]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _audit(
    event: str,
    *,
    server_name: str,
    tool_name: str,
    proposal_digest: str = "",
    preview_digest: str = "",
    result_digest: str = "",
    reason: str = "",
) -> None:
    logger.info(
        "mcp_permission_rail event=%s server=%s tool=%s proposal_digest=%s "
        "preview_digest=%s result_digest=%s reason=%s",
        event,
        server_name,
        tool_name,
        proposal_digest[:12] if proposal_digest else "",
        preview_digest[:12] if preview_digest else "",
        result_digest[:12] if result_digest else "",
        reason,
    )


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if isinstance(value, (list, tuple)):
        return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    return ()


def _as_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _as_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _as_preview_limit(value: Any) -> int:
    parsed = _as_positive_int(value, _DEFAULT_MAX_PREVIEW_CHARS)
    return min(parsed, _HARD_MAX_PREVIEW_CHARS)


def _invalid_policy(server_name: str, reason: str) -> MCPRailPolicy:
    return MCPRailPolicy(
        server_name=server_name,
        ttl_seconds=120.0,
        max_backend_expiry_seconds=86400.0,
        max_pending_global=256,
        max_pending_per_session=8,
        max_preview_chars=_DEFAULT_MAX_PREVIEW_CHARS,
        prepare_patterns=(),
        execute_patterns=(),
        status_patterns=(),
        read_patterns=(),
        bindings=(),
        prepare_prefix="",
        execute_prefix="",
        deny_execute_patterns=(),
        token_result_paths=(),
        preview_result_paths=(),
        expires_at_result_paths=(),
        confirmation_code_result_paths=(),
        execute_token_paths=(),
        execute_confirmation_code_paths=(),
        attestation=None,
        invalid_reason=reason,
    )


def _load_policy(server_name: str) -> Optional[MCPRailPolicy]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception as exc:
        return _invalid_policy(server_name, f"config load failed: {exc}")

    root = cfg.get("mcp_permission_rails") if isinstance(cfg, dict) else None
    if not isinstance(root, dict):
        return None
    servers = root.get("servers")
    if not isinstance(servers, dict):
        return None
    raw = servers.get(server_name)
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None
    _known_protected_servers.add(server_name)

    tools = raw.get("tools") if isinstance(raw.get("tools"), dict) else {}
    result_paths = raw.get("result_paths") if isinstance(raw.get("result_paths"), dict) else {}
    bindings = []
    bindings_raw = raw.get("bindings") or ()
    if isinstance(bindings_raw, list):
        for item in bindings_raw:
            if not isinstance(item, dict):
                continue
            prepare = str(item.get("prepare") or "").strip()
            execute = str(item.get("execute") or "").strip()
            if prepare and execute:
                bindings.append((prepare, execute))

    prepare_patterns = _as_str_tuple(tools.get("prepare"))
    execute_patterns = _as_str_tuple(tools.get("execute"))
    prepare_prefix = str(raw.get("prepare_prefix") or "").strip()
    execute_prefix = str(raw.get("execute_prefix") or "").strip()
    if not execute_patterns:
        return _invalid_policy(server_name, "enabled protected server has no execute patterns")
    if prepare_patterns and not bindings and not (prepare_prefix and execute_prefix):
        return _invalid_policy(
            server_name,
            "enabled protected server has prepare patterns but no deterministic bindings",
        )

    try:
        from tools.hermes_attestation import HermesAttestationPolicy

        attestation = HermesAttestationPolicy.from_mapping(raw.get("attestation"))
    except Exception as exc:
        return _invalid_policy(server_name, f"attestation config invalid: {exc}")

    return MCPRailPolicy(
        server_name=server_name,
        ttl_seconds=_as_float(raw.get("ttl_seconds"), 120.0),
        max_backend_expiry_seconds=_as_float(raw.get("max_backend_expiry_seconds"), 86400.0),
        max_pending_global=_as_positive_int(raw.get("max_pending_global"), 256),
        max_pending_per_session=_as_positive_int(raw.get("max_pending_per_session"), 8),
        max_preview_chars=_as_preview_limit(raw.get("max_preview_chars")),
        prepare_patterns=prepare_patterns,
        execute_patterns=execute_patterns,
        status_patterns=_as_str_tuple(tools.get("status")),
        read_patterns=_as_str_tuple(tools.get("read")),
        bindings=tuple(bindings),
        prepare_prefix=prepare_prefix,
        execute_prefix=execute_prefix,
        deny_execute_patterns=_as_str_tuple(raw.get("deny_execute_tools")),
        token_result_paths=_as_str_tuple(result_paths.get("proposal_token") or ("proposal_token",)),
        preview_result_paths=_as_str_tuple(result_paths.get("preview") or ("preview",)),
        expires_at_result_paths=_as_str_tuple(result_paths.get("expires_at") or ("expires_at",)),
        confirmation_code_result_paths=_as_str_tuple(result_paths.get("confirmation_code") or ()),
        execute_token_paths=_as_str_tuple(raw.get("execute_token_paths") or ("proposal_token",)),
        execute_confirmation_code_paths=_as_str_tuple(
            raw.get("execute_confirmation_code_paths") or ("confirmation_code",)
        ),
        attestation=attestation,
    )


def _matches(tool_name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(tool_name, pattern) for pattern in patterns)


def _execute_for_prepare(policy: MCPRailPolicy, prepare_tool: str) -> str:
    matches = [
        execute
        for prepare, execute in policy.bindings
        if fnmatch.fnmatchcase(prepare_tool, prepare)
        and not any(char in execute for char in "*?[")
    ]
    if len(set(matches)) == 1:
        return matches[0]
    if policy.prepare_prefix and policy.execute_prefix and prepare_tool.startswith(policy.prepare_prefix):
        execute_tool = policy.execute_prefix + prepare_tool[len(policy.prepare_prefix) :]
        if _matches(execute_tool, policy.execute_patterns) and not _matches(
            execute_tool, policy.deny_execute_patterns
        ):
            return execute_tool
    return ""


def _current_context() -> dict[str, str]:
    try:
        from gateway.session_context import get_trusted_session_env
    except Exception:
        get_trusted_session_env = None

    def get(name: str) -> str:
        if get_trusted_session_env is None:
            return ""
        return get_trusted_session_env(name, "") or ""

    try:
        from tools.hermes_attestation import normalize_thread_id
    except Exception:
        normalize_thread_id = lambda value: value  # type: ignore[assignment]

    return {
        "platform": get("HERMES_SESSION_PLATFORM"),
        "source": get("HERMES_SESSION_SOURCE"),
        "chat_id": get("HERMES_SESSION_CHAT_ID"),
        "user_id": get("HERMES_SESSION_USER_ID"),
        "thread_id": normalize_thread_id(get("HERMES_SESSION_THREAD_ID")),
        "session_id": get("HERMES_SESSION_ID"),
        "session_key": get("HERMES_SESSION_KEY"),
        "message_id": get("HERMES_SESSION_MESSAGE_ID"),
    }


def _is_blocked_non_human_context(ctx: Mapping[str, str]) -> bool:
    source = str(ctx.get("source") or "").strip().lower()
    platform = str(ctx.get("platform") or "").strip().lower()
    if os.environ.get("HERMES_CRON_SESSION") or source == "cron" or platform == "cron":
        return True
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    try:
        from agent.delegation_context import is_delegated_child_process_context

        if is_delegated_child_process_context():
            return True
    except Exception:
        if os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"):
            return True
    return not (
        ctx.get("platform") == "telegram"
        and bool(ctx.get("chat_id"))
        and bool(ctx.get("user_id"))
        and bool(ctx.get("session_id"))
        and bool(ctx.get("session_key"))
        and bool(ctx.get("message_id"))
    )


def _context_matches(expected: Mapping[str, str], observed: Mapping[str, str]) -> bool:
    for key, expected_value in expected.items():
        if expected_value and str(observed.get(key) or "") != str(expected_value):
            return False
    return True


def _values_at_path(data: Any, path: str) -> list[Any]:
    node = data
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
            continue
        return []
    if node in (None, ""):
        return []
    return [node]


def _single_string_value(data: Any, paths: tuple[str, ...], label: str) -> str:
    values: list[str] = []
    for path in paths:
        for value in _values_at_path(data, path):
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                text = str(value).strip()
                if text:
                    values.append(text)
    unique = sorted(set(values))
    if len(unique) != 1:
        raise ValueError(f"ambiguous or missing {label}")
    return unique[0]


def _optional_string_value(data: Any, paths: tuple[str, ...], label: str) -> str:
    if not paths:
        return ""
    try:
        return _single_string_value(data, paths, label)
    except ValueError:
        return ""


def _canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _canonical_digest(data: Any) -> str:
    return _hash(_canonical_json(data))


def _single_json_value(data: Any, paths: tuple[str, ...], label: str) -> Any:
    values: list[Any] = []
    for path in paths:
        values.extend(_values_at_path(data, path))
    unique = sorted({_canonical_json(value) for value in values})
    if len(unique) != 1:
        raise ValueError(f"ambiguous or missing {label}")
    for value in values:
        if _canonical_json(value) == unique[0]:
            return value
    raise ValueError(f"ambiguous or missing {label}")


def _json_candidates_from_result(result: Any) -> list[dict[str, Any]]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return [structured]
    if structured is not None:
        return []

    candidates: list[dict[str, Any]] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            candidates.append(parsed)
    return candidates


def _displayable_preview(preview: Any, *, max_chars: int) -> str:
    escaped = _canonical_json(preview).encode("unicode_escape", "backslashreplace").decode("ascii")
    if len(escaped) > max_chars:
        raise ValueError(
            "preview exceeds max_preview_chars; BLOCKED because the full "
            "canonical preview cannot be displayed. narrow the request or use "
            "DPS Work for this operation."
        )
    return escaped


def _parse_backend_expiry(value: Any, *, now_wall: float, max_seconds: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = float(value)
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed = float(raw)
        except ValueError:
            iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            try:
                dt = datetime.fromisoformat(iso)
            except ValueError as exc:
                raise ValueError("invalid expires_at") from exc
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            parsed = dt.timestamp()
    else:
        raise ValueError("invalid expires_at")
    if parsed <= now_wall:
        raise ValueError("past expires_at")
    if parsed - now_wall > max_seconds:
        raise ValueError("overlong expires_at")
    return parsed


def _extract_prepare_proposal(policy: MCPRailPolicy, result: Any) -> _ExtractedProposal:
    proposals: list[_ExtractedProposal] = []
    for candidate in _json_candidates_from_result(result):
        if candidate.get("ok") is not True:
            raise ValueError("prepare result ok is not true")
        token = _single_string_value(candidate, policy.token_result_paths, "proposal_token")
        preview = _single_json_value(candidate, policy.preview_result_paths, "preview")
        expires_raw = _single_json_value(candidate, policy.expires_at_result_paths, "expires_at")
        confirmation_code = _optional_string_value(
            candidate,
            policy.confirmation_code_result_paths,
            "confirmation_code",
        )
        proposal_meta = {"proposal_digest": "", "proposal_payload_digest": ""}
        if getattr(getattr(policy, "attestation", None), "enabled", False):
            try:
                from tools.hermes_attestation import decode_proposal_token_metadata

                proposal_meta = decode_proposal_token_metadata(token)
            except Exception as exc:
                raise ValueError(f"proposal_token metadata invalid: {exc}") from exc
        proposals.append(
            _ExtractedProposal(
                proposal_token_digest=_hash(token),
                confirmation_code_digest=_hash(confirmation_code) if confirmation_code else "",
                preview_digest=_canonical_digest(preview),
                canonical_result_digest=_canonical_digest(candidate),
                proposal_digest=proposal_meta["proposal_digest"],
                proposal_payload_digest=proposal_meta["proposal_payload_digest"],
                preview=_displayable_preview(preview, max_chars=policy.max_preview_chars),
                backend_expires_at_wall=_parse_backend_expiry(
                    expires_raw,
                    now_wall=time.time(),
                    max_seconds=policy.max_backend_expiry_seconds,
                ),
            )
        )

    unique = {
        (
            item.proposal_token_digest,
            item.confirmation_code_digest,
            item.preview_digest,
            item.canonical_result_digest,
            item.proposal_digest,
            item.proposal_payload_digest,
            item.preview,
            item.backend_expires_at_wall,
        )
        for item in proposals
    }
    if len(unique) != 1:
        raise ValueError("ambiguous or missing proposal_token/preview/expires_at")
    return proposals[0]


def _extract_execute_token_digest(policy: MCPRailPolicy, args: Mapping[str, Any]) -> str:
    token = _single_string_value(args, policy.execute_token_paths, "proposal_token")
    return _hash(token)


def _protected_call_args(args: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        from tools.hermes_attestation import strip_internal_hermes_fields

        stripped = strip_internal_hermes_fields(args or {})
        return stripped if isinstance(stripped, Mapping) else (args or {})
    except Exception:
        return args or {}


def _prepare_attestation_meta(
    policy: MCPRailPolicy,
    tool_name: str,
    args: Mapping[str, Any],
    ctx: Mapping[str, str],
) -> Mapping[str, Any] | str:
    attestation = getattr(policy, "attestation", None)
    if not getattr(attestation, "enabled", False):
        return {}
    try:
        from tools.hermes_attestation import attestation_meta, sign_attestation

        token = sign_attestation(
            policy=attestation,
            phase="prepare",
            tool_name=tool_name,
            args=args,
            ctx=ctx,
        )
        return attestation_meta(token)
    except Exception as exc:
        _audit("denied", server_name=policy.server_name, tool_name=tool_name, reason="attestation_prepare")
        return f"BLOCKED: Protected MCP prepare attestation failed closed ({exc})."


def _execute_attestation_meta(
    policy: MCPRailPolicy,
    tool_name: str,
    args: Mapping[str, Any],
    ctx: Mapping[str, str],
    pending: PendingProposal,
    approval: Mapping[str, Any],
) -> Mapping[str, Any] | str:
    attestation = getattr(policy, "attestation", None)
    if not getattr(attestation, "enabled", False):
        return {}
    proposal = {
        "proposal_token_digest": pending.proposal_token_digest,
        "proposal_digest": pending.proposal_digest,
        "proposal_payload_digest": pending.proposal_payload_digest,
    }
    approval_meta = {
        "approval_request_id": str(approval.get("approval_request_id") or ""),
        "approval_event_id": str(approval.get("approval_event_id") or ""),
        "approved_at": str(approval.get("approved_at") or ""),
    }
    try:
        from tools.hermes_attestation import attestation_meta, sign_attestation

        token = sign_attestation(
            policy=attestation,
            phase="execute",
            tool_name=tool_name,
            args=args,
            ctx=ctx,
            approval=approval_meta,
            proposal=proposal,
        )
        return attestation_meta(token)
    except Exception as exc:
        _audit("denied", server_name=policy.server_name, tool_name=tool_name, reason="attestation_execute")
        return f"BLOCKED: Protected MCP execute attestation failed closed ({exc})."


def _execute_confirmation_digest(
    policy: MCPRailPolicy,
    args: Mapping[str, Any],
    expected_digest: str,
) -> str:
    if not expected_digest:
        return ""
    code = _single_string_value(args, policy.execute_confirmation_code_paths, "confirmation_code")
    return _hash(code)


def _is_read_only_annotation(tool_annotations: Any) -> bool:
    if isinstance(tool_annotations, Mapping):
        return tool_annotations.get("readOnlyHint") is True
    return getattr(tool_annotations, "readOnlyHint", None) is True


def before_tool_call(
    server_name: str,
    tool_name: str,
    args: Mapping[str, Any],
    tool_annotations: Any = None,
) -> MCPRailDecision:
    policy = _load_policy(server_name)
    if policy is None:
        return MCPRailDecision(policy=None)
    if policy.invalid_reason:
        _audit("denied", server_name=server_name, tool_name=tool_name, reason="invalid_config")
        return MCPRailDecision(
            policy=policy,
            kind="config",
            denied_message=(
                "BLOCKED: MCP permission rail failed closed because protected "
                f"server configuration is invalid ({policy.invalid_reason})."
            ),
        )

    _purge_expired()

    # Safe precedence: deny > execute > prepare > status > read.
    if _matches(tool_name, policy.deny_execute_patterns):
        _audit("denied", server_name=server_name, tool_name=tool_name, reason="config_deny")
        return MCPRailDecision(
            policy=policy,
            kind="execute",
            denied_message="BLOCKED: Protected MCP execute tool is disabled by configuration.",
        )
    if _matches(tool_name, policy.execute_patterns):
        kind = "execute"
    elif _matches(tool_name, policy.prepare_patterns):
        kind = "prepare"
    elif _matches(tool_name, policy.status_patterns):
        return MCPRailDecision(policy=policy, kind="status")
    elif _matches(tool_name, policy.read_patterns):
        return MCPRailDecision(policy=policy, kind="read")
    elif not _is_read_only_annotation(tool_annotations):
        _audit(
            "denied",
            server_name=server_name,
            tool_name=tool_name,
            reason="mutation_annotation_not_covered",
        )
        return MCPRailDecision(
            policy=policy,
            kind="unknown_mutation",
            denied_message=(
                "BLOCKED: MCP tool on protected server is mutation-capable "
                "but does not match a protected execute or deny pattern."
            ),
        )
    else:
        return MCPRailDecision(policy=policy)

    if kind == "prepare":
        execute_tool = _execute_for_prepare(policy, tool_name)
        if not execute_tool:
            return MCPRailDecision(
                policy=policy,
                kind="prepare",
                denied_message=(
                    "BLOCKED: MCP prepare tool is protected but its execute "
                    "relation is ambiguous or missing in config."
                ),
            )
        ctx = _current_context()
        call_args = _protected_call_args(args or {})
        if getattr(getattr(policy, "attestation", None), "enabled", False) and _is_blocked_non_human_context(ctx):
            _audit("context_blocked", server_name=server_name, tool_name=tool_name, reason="non_human_context")
            return MCPRailDecision(
                policy=policy,
                kind="prepare",
                denied_message=(
                    "BLOCKED: Protected MCP prepare attestation requires a live "
                    "Telegram human context with an originating message_id."
                ),
                call_args=call_args,
            )
        meta = _prepare_attestation_meta(policy, tool_name, call_args, ctx)
        if isinstance(meta, str):
            return MCPRailDecision(
                policy=policy,
                kind="prepare",
                denied_message=meta,
                call_args=call_args,
            )
        return MCPRailDecision(
            policy=policy,
            kind="prepare",
            prepare_tool=tool_name,
            prepare_execute_tool=execute_tool,
            call_args=call_args,
            mcp_meta=meta,
        )

    ctx = _current_context()
    if _is_blocked_non_human_context(ctx):
        _audit("context_blocked", server_name=server_name, tool_name=tool_name, reason="non_human_context")
        return MCPRailDecision(
            policy=policy,
            kind="execute",
            denied_message=(
                "BLOCKED: Protected MCP execute requires a live Telegram human "
                "context with an authenticated user_id and durable session_id."
            ),
        )

    try:
        call_args = _protected_call_args(args or {})
        token_digest = _extract_execute_token_digest(policy, call_args)
    except ValueError as exc:
        _audit("denied", server_name=server_name, tool_name=tool_name, reason="missing_token")
        return MCPRailDecision(
            policy=policy,
            kind="execute",
            denied_message=f"BLOCKED: Protected MCP execute has {exc}.",
        )

    pending = _consume_pending(policy, tool_name, token_digest, ctx)
    if isinstance(pending, str):
        return MCPRailDecision(policy=policy, kind="execute", denied_message=pending)
    try:
        confirmation_digest = _execute_confirmation_digest(
            policy,
            call_args,
            pending.confirmation_code_digest,
        )
    except ValueError as exc:
        return MCPRailDecision(
            policy=policy,
            kind="execute",
            denied_message=f"BLOCKED: Protected MCP execute has {exc}.",
        )
    if pending.confirmation_code_digest and confirmation_digest != pending.confirmation_code_digest:
        _audit(
            "denied",
            server_name=server_name,
            tool_name=tool_name,
            proposal_digest=pending.proposal_token_digest,
            reason="confirmation_mismatch",
        )
        return MCPRailDecision(
            policy=policy,
            kind="execute",
            denied_message=(
                "BLOCKED: Protected MCP execute confirmation_code does not "
                "match the prepare proposal."
            ),
        )

    _audit(
        "requested",
        server_name=server_name,
        tool_name=tool_name,
        proposal_digest=pending.proposal_token_digest,
        preview_digest=pending.preview_digest,
        result_digest=pending.canonical_result_digest,
    )
    approved, message, approval = _request_approval(pending)
    if not approved:
        _audit(
            "denied",
            server_name=server_name,
            tool_name=tool_name,
            proposal_digest=pending.proposal_token_digest,
            preview_digest=pending.preview_digest,
            result_digest=pending.canonical_result_digest,
            reason="human_denied_or_unavailable",
        )
        return MCPRailDecision(policy=policy, kind="execute", denied_message=message)

    _audit(
        "approved",
        server_name=server_name,
        tool_name=tool_name,
        proposal_digest=pending.proposal_token_digest,
        preview_digest=pending.preview_digest,
        result_digest=pending.canonical_result_digest,
    )
    meta = _execute_attestation_meta(policy, tool_name, call_args, ctx, pending, approval)
    if isinstance(meta, str):
        return MCPRailDecision(
            policy=policy,
            kind="execute",
            denied_message=meta,
            call_args=call_args,
        )
    return MCPRailDecision(
        policy=policy,
        kind="execute",
        approved=True,
        pending=pending,
        call_args=call_args,
        mcp_meta=meta,
    )


def _consume_pending(
    policy: MCPRailPolicy,
    execute_tool: str,
    token_digest: str,
    ctx: Mapping[str, str],
) -> PendingProposal | str:
    with _pending_lock:
        _purge_expired_locked(time.monotonic())
        matches = [
            proposal
            for proposal in _pending
            if proposal.server_name == policy.server_name
            and proposal.execute_tool == execute_tool
            and proposal.proposal_token_digest == token_digest
        ]
        if not matches:
            _audit(
                "denied",
                server_name=policy.server_name,
                tool_name=execute_tool,
                proposal_digest=token_digest,
                reason="no_matching_prepare",
            )
            return (
                "BLOCKED: Protected MCP execute has no matching unexpired "
                "prepare proposal for this session/tool/token."
            )

        context_matches = [proposal for proposal in matches if _context_matches(proposal.context, ctx)]
        if not context_matches:
            _audit(
                "context_blocked",
                server_name=policy.server_name,
                tool_name=execute_tool,
                proposal_digest=token_digest,
                reason="context_mismatch",
            )
            return "BLOCKED: Protected MCP execute context does not match the pending prepare proposal."
        if len(context_matches) != 1:
            _audit(
                "denied",
                server_name=policy.server_name,
                tool_name=execute_tool,
                proposal_digest=token_digest,
                reason="ambiguous_pending",
            )
            return "BLOCKED: Protected MCP execute matched ambiguous proposals."

        proposal = context_matches[0]
        _pending.remove(proposal)
        return proposal


def _request_approval(pending: PendingProposal) -> tuple[bool, str, Mapping[str, Any]]:
    try:
        from tools.approval import request_sensitive_human_approval
    except Exception:
        return False, "BLOCKED: Sensitive approval system is unavailable.", {}

    remaining = max(0.01, pending.expires_at - time.monotonic())
    decision = request_sensitive_human_approval(
        session_key=pending.context.get("session_key", ""),
        command=(
            f"Protected MCP execute: {pending.server_name}/{pending.execute_tool}\n"
            f"Proposal digest: {pending.proposal_token_digest[:12]}\n"
            f"Preview digest: {pending.preview_digest[:12]}\n"
            f"Result digest: {pending.canonical_result_digest[:12]}\n"
            "Escaped preview:\n"
            "-----BEGIN MCP PREVIEW-----\n"
            f"{pending.preview}\n"
            "-----END MCP PREVIEW-----"
        ),
        hook_command=(
            f"protected_mcp_execute server={pending.server_name} "
            f"tool={pending.execute_tool} "
            f"proposal_digest={pending.proposal_token_digest[:12]} "
            f"preview_digest={pending.preview_digest[:12]} "
            f"result_digest={pending.canonical_result_digest[:12]}"
        ),
        description="Protected MCP execute requires Telegram approval.",
        expected_context=dict(pending.context),
        ttl_seconds=remaining,
        pattern_key=f"mcp_permission_rail:{pending.server_name}:{pending.execute_tool}",
    )
    if decision.get("notify_failed"):
        return False, "BLOCKED: Failed to send protected MCP approval request.", decision
    if decision.get("resolved") is True and decision.get("choice") == "once":
        return True, "", decision
    if decision.get("resolved") is True and decision.get("choice") == "deny":
        return False, "BLOCKED: Protected MCP execute was denied by the user.", decision
    return False, "BLOCKED: Protected MCP execute was not approved by the Telegram user.", decision


def after_tool_result(decision: MCPRailDecision, result: Any) -> str:
    if decision.policy is None or decision.kind != "prepare":
        return ""
    if decision.denied_message:
        return decision.denied_message
    try:
        proposal = _extract_prepare_proposal(decision.policy, result)
    except ValueError as exc:
        _audit(
            "denied",
            server_name=decision.policy.server_name,
            tool_name=decision.prepare_execute_tool or "",
            reason="prepare_parse_failure",
        )
        return f"BLOCKED: Protected MCP prepare result has {exc}."

    ctx = _current_context()
    if _is_blocked_non_human_context(ctx):
        return ""
    now_wall = time.time()
    expires_wall = min(proposal.backend_expires_at_wall, now_wall + decision.policy.ttl_seconds)
    if expires_wall <= now_wall:
        return "BLOCKED: Protected MCP prepare result has expired expires_at."
    pending = PendingProposal(
        server_name=decision.policy.server_name,
        prepare_tool=decision.prepare_tool,
        execute_tool=decision.prepare_execute_tool,
        proposal_token_digest=proposal.proposal_token_digest,
        confirmation_code_digest=proposal.confirmation_code_digest,
        preview_digest=proposal.preview_digest,
        canonical_result_digest=proposal.canonical_result_digest,
        proposal_digest=proposal.proposal_digest,
        proposal_payload_digest=proposal.proposal_payload_digest,
        preview=proposal.preview,
        expires_at=time.monotonic() + (expires_wall - now_wall),
        context=ctx,
    )
    with _pending_lock:
        _purge_expired_locked(time.monotonic())
        _pending[:] = [
            existing
            for existing in _pending
            if not (
                existing.server_name == pending.server_name
                and existing.execute_tool == pending.execute_tool
                and existing.proposal_token_digest == pending.proposal_token_digest
                and _context_matches(existing.context, pending.context)
            )
        ]
        _pending.append(pending)
        _enforce_caps_locked(decision.policy)
    return ""


def _purge_expired() -> None:
    with _pending_lock:
        _purge_expired_locked(time.monotonic())


def _purge_expired_locked(now: float) -> None:
    kept: list[PendingProposal] = []
    expired: list[PendingProposal] = []
    for proposal in _pending:
        if proposal.expires_at <= now:
            expired.append(proposal)
        else:
            kept.append(proposal)
    if expired:
        _pending[:] = kept
    for proposal in expired:
        _audit(
            "expired",
            server_name=proposal.server_name,
            tool_name=proposal.execute_tool,
            proposal_digest=proposal.proposal_token_digest,
            preview_digest=proposal.preview_digest,
            result_digest=proposal.canonical_result_digest,
        )


def _enforce_caps_locked(policy: MCPRailPolicy) -> None:
    if policy.max_pending_per_session > 0:
        by_session: dict[str, list[PendingProposal]] = {}
        for proposal in _pending:
            by_session.setdefault(proposal.context.get("session_key", ""), []).append(proposal)
        to_remove: set[int] = set()
        for proposals in by_session.values():
            overflow = len(proposals) - policy.max_pending_per_session
            if overflow > 0:
                for proposal in proposals[:overflow]:
                    to_remove.add(id(proposal))
        if to_remove:
            _pending[:] = [proposal for proposal in _pending if id(proposal) not in to_remove]
    if policy.max_pending_global > 0 and len(_pending) > policy.max_pending_global:
        del _pending[: len(_pending) - policy.max_pending_global]
