"""Exact execution authority, owner prompting, and per-session state.

Command and script bytes are opaque. This module enforces only exact
identity, resource, session-generation, capability, and explicit profile-mode
contracts; semantic decisions remain with the model.
"""

import contextvars
import datetime as dt
import functools
import hashlib
import json
import logging
import ntpath
import os
import platform
import posixpath
import re
import sys
import threading
import time
import uuid
from typing import Optional
from hermes_cli.config import cfg_get

from tools.interrupt import is_interrupted
from utils import env_var_enabled, is_truthy_value

logger = logging.getLogger(__name__)


class ApprovalNotifyBoundaryError(RuntimeError):
    """Typed, non-secret failure raised by a gateway approval boundary.

    Approval notification may be more than a direct chat send (Cloud Muncho,
    for example, durably escalates a team request to the owner).  A typed
    exception lets that boundary return bounded model guidance without
    exposing transport errors, commands, descriptions, or credentials and
    without classifying exception text.
    """

    def __init__(self, code: str, model_message: str) -> None:
        normalized_code = str(code or "approval_notify_boundary_failed").strip()
        normalized_message = str(model_message or "").strip()
        if not re.fullmatch(r"[a-z0-9_]{1,96}", normalized_code):
            normalized_code = "approval_notify_boundary_failed"
        if not normalized_message or len(normalized_message) > 1_000:
            normalized_message = (
                "BLOCKED: The approval boundary failed before verified owner "
                "notification. Do not execute or bypass the protected action."
            )
        self.code = normalized_code
        self.model_message = normalized_message
        super().__init__(normalized_code)


def _exact_command_sha256(value: object) -> str:
    """Return the digest used by exact plan capabilities.

    The raw value never crosses the approval-notification boundary.  Keeping
    this normalization identical to :func:`consume_plan_capability` lets an
    owner escalation name the exact protected command without publishing it.
    """

    raw = str(value if value is not None else "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_EXECUTION_RESOURCE_FIELDS: dict[str, tuple[str, ...]] = {
    "local": (),
    "ssh": ("ssh_host", "ssh_user", "ssh_port", "ssh_key"),
    "docker": (
        "docker_image",
        "docker_volumes",
        "docker_mount_cwd_to_workspace",
        "host_cwd",
        "docker_run_as_host_user",
        "docker_network",
    ),
    "singularity": ("singularity_image",),
    "modal": ("modal_image", "modal_mode"),
    "daytona": ("daytona_image",),
    "isolated_worker": (
        "isolated_worker_socket",
        "isolated_worker_server_uid",
        "isolated_worker_server_gid",
        "isolated_worker_socket_uid",
        "isolated_worker_socket_gid",
    ),
    "vercel_sandbox": ("vercel_runtime",),
}


def _json_exact_value(value: object) -> object:
    """Return a deterministic JSON value without interpreting its meaning."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_exact_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _json_exact_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return str(value)


def _execution_resource_binding(
    env_type: str = "",
    *,
    env_config: Optional[dict] = None,
) -> tuple[str, str]:
    """Return the exact terminal backend and opaque resource digest.

    This is structural binding only.  It never inspects command/script text or
    assigns risk.  The digest prevents a capability commissioned for one
    concrete execution endpoint from authorizing the same bytes on another.
    """

    config = env_config
    if config is None:
        try:
            from tools.terminal_tool import _get_env_config

            config = _get_env_config()
        except Exception:
            config = {}
    config = config if isinstance(config, dict) else {}
    backend_kind = str(env_type or config.get("env_type") or "local").strip()
    if backend_kind not in _EXECUTION_RESOURCE_FIELDS:
        raise ValueError("unsupported execution backend binding")
    selected = {
        key: _json_exact_value(config.get(key))
        for key in _EXECUTION_RESOURCE_FIELDS[backend_kind]
    }
    if backend_kind == "local":
        selected = {
            "machine": platform.node(),
            "uid": os.getuid() if hasattr(os, "getuid") else None,
        }
    payload = json.dumps(
        {
            "version": 1,
            "backend_kind": backend_kind,
            "resource": selected,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return backend_kind, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_execution_cwd(
    cwd: str = "",
    *,
    env_type: str = "",
    env_config: Optional[dict] = None,
    base_cwd: str = "",
) -> str:
    """Return the mechanical cwd identity used by execution and authority.

    Command/script bytes are deliberately outside this function.  Paths are
    normalized lexically only: local paths expand ``~`` and become absolute;
    remote/backend paths use POSIX normalization and preserve remote ``~``.
    Symlinks are never resolved and filesystem contents are never inspected.
    Relative per-call paths are anchored to the current session/default cwd.
    """

    config = env_config if isinstance(env_config, dict) else {}
    backend_kind = str(env_type or config.get("env_type") or "local").strip()
    if backend_kind not in _EXECUTION_RESOURCE_FIELDS:
        raise ValueError("unsupported execution backend binding")

    raw_cwd = str(cwd or "")
    raw_base = str(base_cwd or config.get("cwd") or "")
    if "\x00" in raw_cwd or "\x00" in raw_base:
        raise ValueError("execution cwd cannot contain NUL")

    if backend_kind == "local":
        from tools.environments.local import _msys_to_windows_path

        def _local_path(value: str) -> str:
            expanded = os.path.expanduser(value)
            if platform.system() == "Windows":
                return _msys_to_windows_path(expanded)
            return expanded

        selected = _local_path(raw_cwd or raw_base or os.getcwd())
        if platform.system() == "Windows":
            if ntpath.isabs(selected):
                return ntpath.normpath(selected)
            anchor = _local_path(raw_base or os.getcwd())
            if not ntpath.isabs(anchor):
                anchor = ntpath.abspath(anchor)
            return ntpath.normpath(ntpath.join(anchor, selected))
        if os.path.isabs(selected):
            return os.path.normpath(selected)
        anchor = _local_path(raw_base or os.getcwd())
        if not os.path.isabs(anchor):
            anchor = os.path.abspath(anchor)
        return os.path.normpath(os.path.join(anchor, selected))

    selected = raw_cwd or raw_base or "~"
    if posixpath.isabs(selected) or selected == "~" or selected.startswith("~/"):
        return posixpath.normpath(selected)
    anchor = raw_base or str(config.get("cwd") or "") or "~"
    if not (
        posixpath.isabs(anchor)
        or anchor == "~"
        or anchor.startswith("~/")
    ):
        anchor = posixpath.join("~", anchor)
    return posixpath.normpath(posixpath.join(anchor, selected))


def _exact_execution_subject(
    execution_kind: str,
    raw_input: str,
    *,
    env_type: str = "",
    env_config: Optional[dict] = None,
    resource_sha256: str = "",
    effective_cwd: str = "",
) -> dict[str, str]:
    """Build one typed opaque execution subject.

    Raw bytes, tool kind, backend kind, endpoint resource, and terminal cwd are
    all bound in the subject digest.  Command/script bytes are never normalized
    or parsed.  The cwd uses only :func:`_normalize_execution_cwd`'s mechanical
    path contract; no keyword matching or semantic classification participates.
    """

    if execution_kind not in {"terminal", "execute_code"}:
        raise ValueError("unsupported execution kind")
    if not isinstance(raw_input, str) or not raw_input:
        raise ValueError("exact execution input must be a non-empty string")
    backend_kind, observed_resource_sha256 = _execution_resource_binding(
        env_type,
        env_config=env_config,
    )
    requested_resource_sha256 = str(resource_sha256 or "").strip()
    if (
        requested_resource_sha256
        and requested_resource_sha256 != observed_resource_sha256
    ):
        raise ValueError("execution resource binding mismatch")
    resource_sha256 = observed_resource_sha256
    if not re.fullmatch(r"[0-9a-f]{64}", resource_sha256):
        raise ValueError("execution resource digest must be sha256")
    raw_input_sha256 = hashlib.sha256(raw_input.encode("utf-8")).hexdigest()
    cwd_sha256 = ""
    if execution_kind == "terminal":
        normalized_cwd = _normalize_execution_cwd(
            effective_cwd,
            env_type=backend_kind,
            env_config=env_config,
        )
        cwd_sha256 = hashlib.sha256(
            normalized_cwd.encode("utf-8")
        ).hexdigest()
    canonical = json.dumps(
        {
            "version": 2,
            "execution_kind": execution_kind,
            "raw_input_sha256": raw_input_sha256,
            "backend_kind": backend_kind,
            "resource_sha256": resource_sha256,
            "cwd_sha256": cwd_sha256,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "execution_kind": execution_kind,
        "raw_input_sha256": raw_input_sha256,
        "backend_kind": backend_kind,
        "resource_sha256": resource_sha256,
        "cwd_sha256": cwd_sha256,
        "subject_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }

# Freeze YOLO mode at module import time. Reading os.environ on every call
# would allow any skill running inside the process to set this variable and
# instantly bypass all approval checks — a prompt-injection escalation path.
_YOLO_MODE_FROZEN: bool = is_truthy_value(os.getenv("HERMES_YOLO_MODE", ""))

# Per-thread/per-task gateway session identity.
# Gateway runs agent turns concurrently in executor threads, so reading a
# process-global env var for session identity is racy. Keep env fallback for
# legacy single-threaded callers, but prefer the context-local value when set.
_approval_session_key: contextvars.ContextVar[str] = contextvars.ContextVar(
    "approval_session_key",
    default="",
)
_approval_turn_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "approval_turn_id",
    default="",
)
_approval_tool_call_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "approval_tool_call_id",
    default="",
)
# Delegated children may spend authority that the authenticated owner already
# granted to an exact command, but they must never mint or broaden authority.
# This context-local execution mode is bound by ``delegate_task`` before the
# child conversation starts and is copied through every tool-worker boundary.
# It is deliberately not a model/tool argument and carries no command or
# identity data; the existing session/epoch/owner/TTL/use-count checks remain
# the sole authorization source in ``consume_plan_capability``.
_delegated_exact_plan_consumer: contextvars.ContextVar[bool] = (
    contextvars.ContextVar(
        "delegated_exact_plan_consumer",
        default=False,
    )
)

# Interactive-CLI flag. Concurrent ACP sessions run on a shared
# ThreadPoolExecutor (acp_adapter/server.py), so mutating the process-global
# os.environ["HERMES_INTERACTIVE"] races: one session's restore in `finally`
# can clobber another session's set mid-run, dropping it onto the
# non-interactive broad-authority path so a terminal operation executes without
# the approval callback firing (GHSA-96vc-wcxf-jjff). A contextvar is
# thread/task-local, so each executor worker (or asyncio task) sees only its
# own value. None = unset → fall back to the env var for legacy
# single-threaded CLI callers that still export HERMES_INTERACTIVE.
_hermes_interactive_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "hermes_interactive",
    default=None,
)


def set_hermes_interactive_context(interactive: bool) -> contextvars.Token:
    """Bind interactive mode for the current context (thread or asyncio task).

    Use this instead of mutating ``os.environ["HERMES_INTERACTIVE"]`` from
    concurrent executor threads. When unset (default), interactive detection
    falls back to the ``HERMES_INTERACTIVE`` env var for legacy callers.
    """
    return _hermes_interactive_ctx.set("1" if interactive else "")


def reset_hermes_interactive_context(token: contextvars.Token) -> None:
    """Restore the prior value from :func:`set_hermes_interactive_context`."""
    _hermes_interactive_ctx.reset(token)


def _is_interactive_cli() -> bool:
    """True when running an interactive CLI/ACP session.

    Prefers the context-local flag (set by concurrent ACP sessions) and falls
    back to the ``HERMES_INTERACTIVE`` env var for single-threaded callers.
    """
    ctx_val = _hermes_interactive_ctx.get()
    if ctx_val is not None:
        return is_truthy_value(ctx_val)
    return env_var_enabled("HERMES_INTERACTIVE")


def _fire_approval_hook(hook_name: str, **kwargs) -> None:
    """Invoke a plugin lifecycle hook for the approval system.

    Lazy-imports the plugin manager to avoid circular imports (approval.py is
    imported very early, long before plugins are discovered). Never raises --
    plugin errors are logged and swallowed.

    Only fires for the two approval-specific hooks in VALID_HOOKS:
    pre_approval_request, post_approval_response.
    """
    try:
        from hermes_cli.lifecycle import invoke_hook
    except Exception:
        # Plugin system not available in this execution context
        # (e.g. bare tool-only imports, minimal test environments).
        return
    try:
        kwargs.setdefault("turn_id", _approval_turn_id.get())
        kwargs.setdefault("tool_call_id", _approval_tool_call_id.get())
        invoke_hook(hook_name, **kwargs)
    except Exception as exc:
        # invoke_hook() already swallows per-callback errors, so reaching here
        # means the dispatch layer itself failed. Log and move on -- approval
        # flow is safety-critical, plugin observability is not.
        logger.debug("Approval hook %s dispatch failed: %s", hook_name, exc)


def set_current_session_key(session_key: str) -> contextvars.Token[str]:
    """Bind the active approval session key to the current context."""
    return _approval_session_key.set(session_key or "")


def reset_current_session_key(token: contextvars.Token[str]) -> None:
    """Restore the prior approval session key context."""
    _approval_session_key.reset(token)


def set_current_observability_context(
    *,
    turn_id: str = "",
    tool_call_id: str = "",
) -> tuple[contextvars.Token[str], contextvars.Token[str]]:
    """Bind active tool correlation IDs to approval hooks."""
    return (
        _approval_turn_id.set(turn_id or ""),
        _approval_tool_call_id.set(tool_call_id or ""),
    )


def reset_current_observability_context(
    tokens: tuple[contextvars.Token[str], contextvars.Token[str]],
) -> None:
    """Restore prior approval hook correlation IDs."""
    turn_token, tool_token = tokens
    _approval_tool_call_id.reset(tool_token)
    _approval_turn_id.reset(turn_token)


def bind_delegated_exact_plan_consumer() -> contextvars.Token[bool]:
    """Bind a delegated child to consume-only exact-plan authority.

    The returned token must be reset by the caller after it has copied the
    child execution context.  Nested delegation is naturally monotonic: a
    child can copy this restriction into grandchildren but cannot turn it off
    in an independently running context.
    """

    return _delegated_exact_plan_consumer.set(True)


def reset_delegated_exact_plan_consumer(
    token: contextvars.Token[bool],
) -> None:
    """Restore the previous delegated-authority execution mode."""

    _delegated_exact_plan_consumer.reset(token)


def is_delegated_exact_plan_consumer() -> bool:
    """Return whether the active execution context is a delegated child."""

    return _delegated_exact_plan_consumer.get() is True


def get_current_session_key(default: str = "default") -> str:
    """Return the active session key, preferring context-local state.

    Resolution order:
    1. approval-specific contextvars (set by gateway before agent.run)
    2. session_context contextvars (set by _set_session_env)
    3. os.environ fallback (CLI, cron, tests)
    """
    session_key = _approval_session_key.get()
    if session_key:
        return session_key
    from gateway.session_context import get_session_env
    return get_session_env("HERMES_SESSION_KEY", default)


def _get_session_platform() -> str:
    """Return the current gateway platform from contextvars/env fallback."""
    try:
        from gateway.session_context import get_session_env

        return get_session_env("HERMES_SESSION_PLATFORM", "") or ""
    except Exception:
        return os.getenv("HERMES_SESSION_PLATFORM", "") or ""


def _is_cron_session() -> bool:
    """Return whether this execution context is a scheduled cron run.

    Gateway-hosted cron uses task-local ContextVar state so a scheduled job
    cannot leak its unattended approval policy into unrelated live turns.
    The environment fallback keeps standalone cron processes compatible.
    """
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        value = os.getenv("HERMES_CRON_SESSION", "")
    else:
        try:
            value = get_session_env("HERMES_CRON_SESSION", "")
        except Exception:
            # The gateway scheduler no longer writes a process-global cron
            # marker. If task-local authority unexpectedly cannot be read,
            # treating the turn as interactive could bypass cron_mode. Fail
            # closed as unattended instead.
            logger.warning(
                "Could not read task-local cron authority; treating execution "
                "as cron",
                exc_info=True,
            )
            return True
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _is_gateway_approval_context() -> bool:
    """True when this call is inside a gateway/API session.

    Legacy gateway integrations set HERMES_GATEWAY_SESSION in process env.
    Newer concurrent gateway paths bind HERMES_SESSION_PLATFORM via
    contextvars so approval mode does not depend on process-global flags.

    Cron jobs are NEVER gateway-approval contexts even when they originate
    from a gateway platform (cron binds HERMES_SESSION_PLATFORM via
    contextvars for delivery routing). Cron approvals are governed by
    ``approvals.cron_mode`` config, not interactive resolve — letting cron
    fall through to the gateway branch would submit a pending approval
    with no listener and block the job indefinitely.
    """
    if _is_cron_session():
        return False
    if env_var_enabled("HERMES_GATEWAY_SESSION"):
        return True
    return bool(_get_session_platform())

# Sensitive write targets that should trigger approval even when referenced
# via shell expansions like $HOME or $HERMES_HOME, or by the resolved absolute
# active profile home path such as /home/hermes/.hermes/config.yaml. The
# resolved-absolute form is folded into the ~/.hermes/ patterns at detection
# time by _normalize_command_for_detection() — see the rewrite step there — so
# these static patterns stay free of any import-time path snapshot (which would
# go stale when HERMES_HOME is set after this module is imported, e.g. under the
# hermetic test conftest or any deferred-profile-resolution path).
# =========================================================================
# Per-session approval state (thread-safe)
# =========================================================================

_lock = threading.Lock()
_plan_capabilities: dict[str, dict[str, dict]] = {}
_plan_capability_consume_locks: dict[tuple[str, str, str], threading.Lock] = {}
# Process-lifetime replay ledger for runtime-observed approval messages.  It is
# intentionally not cleared with a session: clearing/restarting an agent task
# must not turn the same owner message into fresh authority.  Cross-process
# restoration remains deliberately unsupported until there is an atomic
# durable lease/consume protocol.
_plan_approval_source_states: dict[str, str] = {}
_session_yolo: set[str] = set()
_session_yolo_generations: dict[str, int] = {}
# Stable routing keys survive /new, /resume, and other conversation boundaries.
# A monotonic process-local generation therefore fences every local authority
# write that was commissioned before a boundary.  The old epoch digest is also
# retained as a tombstone so a paused gateway worker that starts a *new* local
# grant after the boundary cannot attach it to the successor generation.
_session_authority_generations: dict[str, int] = {}
_retired_session_capability_epochs: set[tuple[str, str]] = set()

# =========================================================================
# Blocking gateway approval (mirrors CLI's synchronous input() flow)
# =========================================================================
# Per-session QUEUE of pending approvals.  Multiple threads (parallel
# subagents, execute_code RPC handlers) can block concurrently — each gets
# its own threading.Event.  /approve resolves the oldest, /approve all
# resolves every pending approval in the session.


class _ApprovalEntry:
    """One pending exact-operation approval inside a gateway session."""
    __slots__ = (
        "approval_id",
        "event",
        "data",
        "result",
        "reason",
        "authority_generation",
        "capability_epoch_sha256",
    )

    def __init__(
        self,
        data: dict,
        *,
        authority_generation: int = 0,
        capability_epoch_sha256: str = "",
    ):
        self.approval_id = uuid.uuid4().hex
        self.event = threading.Event()
        self.data = dict(data)    # command, description, pattern_keys, …
        self.data["approval_id"] = self.approval_id
        self.result: Optional[str] = None  # "once"|"session"|"always"|"deny"
        self.authority_generation = int(authority_generation)
        self.capability_epoch_sha256 = str(capability_epoch_sha256 or "")
        self.data["_authority_generation"] = self.authority_generation
        self.data["_capability_epoch_sha256"] = self.capability_epoch_sha256
        # Optional free-text reason supplied with an explicit deny
        # (``/deny <reason>``) so the agent can adapt instead of only
        # hearing "denied". Ported from qwibitai/nanoclaw#2832.
        self.reason: Optional[str] = None


_gateway_queues: dict[str, list] = {}        # session_key → [_ApprovalEntry, …]
_gateway_notify_cbs: dict[str, object] = {}  # session_key → callable(approval_data)


def _is_exact_one_operation_entry(entry: _ApprovalEntry) -> bool:
    """Return whether broad/FIFO approval is structurally unavailable."""

    return (
        entry.data.get("exact_execution") is True
        and entry.data.get("allow_session") is False
    )


def register_gateway_notify(session_key: str, cb) -> None:
    """Register a per-session callback for sending approval requests to the user.

    The callback signature is ``cb(approval_data: dict) -> None`` where
    *approval_data* contains ``command``, ``description``, and
    ``pattern_keys``.  The callback bridges sync→async (runs in the agent
    thread, must schedule the actual send on the event loop).
    """
    with _lock:
        _gateway_notify_cbs[session_key] = cb


def unregister_gateway_notify(session_key: str) -> None:
    """Unregister the per-session gateway approval callback.

    Signals ALL blocked threads for this session so they don't hang forever
    (e.g. when the agent run finishes or is interrupted).
    """
    with _lock:
        _gateway_notify_cbs.pop(session_key, None)
        entries = _gateway_queues.pop(session_key, [])
    for entry in entries:
        entry.event.set()


def resolve_gateway_approval(session_key: str, choice: str,
                             resolve_all: bool = False,
                             reason: Optional[str] = None) -> int:
    """Called by the gateway's /approve or /deny handler to unblock
    waiting agent thread(s).

    Legacy approvals retain FIFO/``all`` behavior. Exact one-operation
    approvals are never eligible: they require their opaque approval ID via
    :func:`resolve_gateway_approval_by_id`.

    *reason* is an optional free-text explanation attached to an explicit
    deny (``/deny <reason>``).  It is relayed back to the agent in the
    BLOCKED message so it can adapt instead of only hearing "denied".

    Returns the number of approvals resolved (0 means nothing was pending).
    """
    with _lock:
        queue = _gateway_queues.get(session_key)
        if not queue:
            return 0
        eligible = [
            entry for entry in queue
            if not _is_exact_one_operation_entry(entry)
        ]
        if not eligible:
            return 0
        targets = eligible if resolve_all else eligible[:1]
        for target in targets:
            queue.remove(target)
        if not queue:
            _gateway_queues.pop(session_key, None)

    for entry in targets:
        entry.result = choice
        if reason:
            entry.reason = reason
        entry.event.set()
    return len(targets)


def resolve_gateway_approval_by_id(
    session_key: str,
    approval_id: str,
    choice: str,
    *,
    reason: Optional[str] = None,
) -> int:
    """Resolve exactly one opaque pending approval in one exact session.

    Unlike :func:`resolve_gateway_approval`, this function never falls back to
    FIFO and cannot resolve a sibling entry when the supplied ID is stale or
    belongs to another session.  It is the control-plane primitive for API
    clients that may have several concurrent approval prompts.

    Returns ``1`` only after the exact live entry has been detached and
    signalled, otherwise ``0``.  Choice validation is duplicated at this
    boundary so non-HTTP callers cannot inject an unrecognized decision.
    """

    normalized_session = str(session_key or "")
    normalized_id = str(approval_id or "")
    normalized_choice = str(choice or "")
    if (
        not normalized_session
        or re.fullmatch(r"[0-9a-f]{32}", normalized_id) is None
        or normalized_choice not in {"once", "session", "always", "deny"}
    ):
        return 0

    with _lock:
        queue = _gateway_queues.get(normalized_session)
        if not queue:
            return 0
        target = next(
            (
                entry
                for entry in queue
                if getattr(entry, "approval_id", "") == normalized_id
            ),
            None,
        )
        if target is None:
            return 0
        if (
            _is_exact_one_operation_entry(target)
            and normalized_choice not in {"once", "deny"}
        ):
            return 0
        queue.remove(target)
        if not queue:
            _gateway_queues.pop(normalized_session, None)

    target.result = normalized_choice
    if reason:
        target.reason = str(reason)
    target.event.set()
    return 1


def cancel_gateway_approvals(session_key: str) -> int:
    """Cancel every pending entry during trusted session teardown.

    This is deliberately separate from user-facing FIFO/``all`` resolution:
    teardown must wake blocked workers, but it must not turn ``/deny all`` or
    any other broad user control into authority over exact entries.
    """

    with _lock:
        entries = _gateway_queues.pop(str(session_key or ""), [])
    for entry in entries:
        entry.result = "deny"
        entry.event.set()
    return len(entries)


def prepare_gateway_owner_escalation_binding(
    session_key: str,
    approval_id: str,
    *,
    owner_user_id: str,
    owner_guild_id: str,
    source_lane_id: str,
    case_id: str,
    plan_id: str,
    plan_revision: int,
    command_sha256: str,
) -> bool:
    """Prepare an exact cross-session owner response binding.

    Production Discord threads intentionally isolate sessions per user.  The
    owner therefore cannot resolve a team member's waiting entry through the
    ordinary per-session queue.  This private binding is the narrow bridge: it
    contains only immutable IDs/hashes, remains inactive until the public
    route-back has a verified receipt, and never grants session/permanent
    authority.
    """

    normalized = {
        "session_key": str(session_key or ""),
        "approval_id": str(approval_id or "").lower(),
        "owner_user_id": str(owner_user_id or ""),
        "owner_guild_id": str(owner_guild_id or ""),
        "source_lane_id": str(source_lane_id or ""),
        "case_id": str(case_id or ""),
        "plan_id": str(plan_id or ""),
        "command_sha256": str(command_sha256 or "").lower(),
    }
    if (
        not normalized["session_key"]
        or re.fullmatch(r"[0-9a-f]{32}", normalized["approval_id"]) is None
        or re.fullmatch(r"[0-9]{17,20}", normalized["owner_user_id"]) is None
        or re.fullmatch(r"[0-9]{17,20}", normalized["owner_guild_id"]) is None
        or re.fullmatch(r"[0-9]{17,20}", normalized["source_lane_id"]) is None
        or re.fullmatch(
            r"case:[A-Za-z0-9][A-Za-z0-9._:/-]{0,239}",
            normalized["case_id"],
        )
        is None
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}",
            normalized["plan_id"],
        )
        is None
        or type(plan_revision) is not int
        or not 1 <= plan_revision <= 999_999_999
        or re.fullmatch(r"[0-9a-f]{64}", normalized["command_sha256"]) is None
    ):
        return False

    binding = {
        "status": "prepared",
        "owner_user_id": normalized["owner_user_id"],
        "owner_guild_id": normalized["owner_guild_id"],
        "source_lane_id": normalized["source_lane_id"],
        "case_id": normalized["case_id"],
        "plan_id": normalized["plan_id"],
        "plan_revision": plan_revision,
        "command_sha256": normalized["command_sha256"],
    }
    with _lock:
        queue = _gateway_queues.get(normalized["session_key"])
        if not queue:
            return False
        target = next(
            (
                entry
                for entry in queue
                if getattr(entry, "approval_id", "") == normalized["approval_id"]
            ),
            None,
        )
        if target is None:
            return False
        if target.data.get("command_sha256") != normalized["command_sha256"]:
            return False
        existing = target.data.get("_owner_escalation_binding")
        if isinstance(existing, dict):
            return existing == binding
        target.data["_owner_escalation_binding"] = binding
    return True


def activate_gateway_owner_escalation_binding(
    session_key: str,
    approval_id: str,
) -> bool:
    """Activate a prepared owner binding after verified public delivery."""

    normalized_session = str(session_key or "")
    normalized_id = str(approval_id or "").lower()
    if (
        not normalized_session
        or re.fullmatch(r"[0-9a-f]{32}", normalized_id) is None
    ):
        return False
    with _lock:
        queue = _gateway_queues.get(normalized_session)
        target = next(
            (
                entry
                for entry in queue or ()
                if getattr(entry, "approval_id", "") == normalized_id
            ),
            None,
        )
        if target is None:
            return False
        binding = target.data.get("_owner_escalation_binding")
        if not isinstance(binding, dict) or binding.get("status") != "prepared":
            return False
        target.data["_owner_escalation_binding"] = {
            **binding,
            "status": "active",
        }
    return True


def clear_gateway_owner_escalation_binding(
    session_key: str,
    approval_id: str,
) -> None:
    """Remove an undelivered owner binding without resolving the command."""

    normalized_session = str(session_key or "")
    normalized_id = str(approval_id or "").lower()
    with _lock:
        for entry in _gateway_queues.get(normalized_session, ()):
            if getattr(entry, "approval_id", "") == normalized_id:
                binding = entry.data.get("_owner_escalation_binding")
                if isinstance(binding, dict) and binding.get("status") == "prepared":
                    entry.data.pop("_owner_escalation_binding", None)
                return


def resolve_gateway_owner_escalation_by_id(
    approval_id: str,
    choice: str,
    *,
    owner_user_id: str,
    owner_guild_id: str,
    response_lane_id: str,
    reason: Optional[str] = None,
) -> int:
    """Resolve one active, receipt-bound owner escalation across sessions.

    This is deliberately not a general global approval lookup.  The opaque ID
    must name an entry carrying an *active* owner-escalation binding, and the
    authenticated caller IDs and exact public source lane must all match.  An
    escalated approval can authorize only this one command (``once``) or deny
    it; broader authority remains an explicit model-authored plan capability.
    """

    normalized_id = str(approval_id or "").lower()
    normalized_choice = str(choice or "")
    expected = {
        "owner_user_id": str(owner_user_id or ""),
        "owner_guild_id": str(owner_guild_id or ""),
        "source_lane_id": str(response_lane_id or ""),
    }
    if (
        re.fullmatch(r"[0-9a-f]{32}", normalized_id) is None
        or normalized_choice not in {"once", "deny"}
        or any(
            re.fullmatch(r"[0-9]{17,20}", value) is None
            for value in expected.values()
        )
    ):
        return 0

    with _lock:
        matches = []
        for session_key, queue in _gateway_queues.items():
            for entry in queue:
                if getattr(entry, "approval_id", "") != normalized_id:
                    continue
                binding = entry.data.get("_owner_escalation_binding")
                if not isinstance(binding, dict) or binding.get("status") != "active":
                    continue
                if any(binding.get(key) != value for key, value in expected.items()):
                    continue
                if binding.get("command_sha256") != entry.data.get("command_sha256"):
                    continue
                matches.append((session_key, entry, dict(binding)))
        if len(matches) != 1:
            return 0
        session_key, target, binding = matches[0]

    if normalized_choice == "once":
        if not _canonical_active_plan_matches(
            case_id=str(binding.get("case_id") or ""),
            plan_id=str(binding.get("plan_id") or ""),
            plan_revision=binding.get("plan_revision"),
        ):
            return 0

    with _lock:
        queue = _gateway_queues.get(session_key)
        if not queue or target not in queue:
            return 0
        current_binding = target.data.get("_owner_escalation_binding")
        if current_binding != binding or current_binding.get("status") != "active":
            return 0
        queue.remove(target)
        if not queue:
            _gateway_queues.pop(session_key, None)

    target.result = normalized_choice
    if normalized_choice == "deny" and reason:
        target.reason = str(reason)
    target.event.set()
    return 1


def get_pending_gateway_approvals(
    session_key: str,
    *,
    include_authority_binding: bool = False,
) -> list[dict]:
    """Return snapshots of pending approvals for one exact session.

    Private generation/epoch bindings are excluded by default so a transport
    cannot leak authority metadata merely by serializing this helper's result.
    The API adapter opts in only while validating an exact approval ID; its
    public projections independently strip every underscore-prefixed field.
    """

    with _lock:
        snapshots = [
            dict(entry.data)
            for entry in _gateway_queues.get(str(session_key or ""), ())
        ]
    if include_authority_binding:
        return snapshots
    return [
        {
            key: value
            for key, value in snapshot.items()
            if not str(key).startswith("_")
        }
        for snapshot in snapshots
    ]


def has_blocking_approval(session_key: str) -> bool:
    """Check if a session has one or more blocking gateway approvals waiting."""
    with _lock:
        return bool(_gateway_queues.get(session_key))


def has_exact_blocking_approval(session_key: str) -> bool:
    """Return whether the session contains an exact-ID-only approval."""

    with _lock:
        return any(
            _is_exact_one_operation_entry(entry)
            for entry in _gateway_queues.get(str(session_key or ""), ())
        )


def _observed_capability_epoch_sha256() -> str:
    """Return the gateway-owned epoch digest bound to the current worker."""

    return _observed_session_value("HERMES_CAPABILITY_EPOCH_SHA256")


def _current_authority_generation_locked(session_key: str) -> int:
    return int(_session_authority_generations.get(session_key, 0))


def capture_session_authority_fence(session_key: str) -> tuple[int, str]:
    """Capture the exact process-local authority generation for current work.

    A retired gateway epoch can never capture the successor generation.  This
    closes the post-boundary race where a paused old worker wakes only after
    cleanup and otherwise appears to be a brand-new writer on the stable key.
    """

    session_key = str(session_key or "")
    epoch_sha256 = _observed_capability_epoch_sha256()
    with _lock:
        if epoch_sha256 and (
            session_key,
            epoch_sha256,
        ) in _retired_session_capability_epochs:
            raise PermissionError("session authority epoch has been retired")
        return _current_authority_generation_locked(session_key), epoch_sha256


def session_authority_fence_is_current(
    session_key: str,
    authority_generation: int,
    capability_epoch_sha256: str = "",
) -> bool:
    """Mechanically validate one captured local-authority fence."""

    session_key = str(session_key or "")
    epoch_sha256 = str(capability_epoch_sha256 or "")
    with _lock:
        return bool(
            int(authority_generation)
            == _current_authority_generation_locked(session_key)
            and not (
                epoch_sha256
                and (session_key, epoch_sha256)
                in _retired_session_capability_epochs
            )
        )


def _release_permission_mode_dependents(session_key: str) -> None:
    """Drop resources whose immutable mode is derived from Hermes YOLO.

    The import stays lazy so approval-only sessions do not load computer-use.
    Releasing on both edges makes enabling YOLO replace an existing standard
    backend and makes disabling YOLO revoke a private unrestricted daemon
    immediately, even when no later computer-use call occurs.
    """
    try:
        from tools.computer_use import release_computer_use_session

        release_computer_use_session(session_key)
    except Exception:
        logger.debug(
            "Failed to release permission-mode dependent resources for %s",
            session_key,
            exc_info=True,
        )


def enable_session_yolo(
    session_key: str,
    *,
    expected_generation: int | None = None,
) -> bool:
    """Enable YOLO only for the captured boundary generation."""

    if is_delegated_exact_plan_consumer():
        logger.warning("Delegated execution cannot enable session YOLO")
        return False
    if not session_key:
        return False
    epoch_sha256 = _observed_capability_epoch_sha256()
    with _lock:
        current_generation = _current_authority_generation_locked(session_key)
        if (
            expected_generation is not None
            and int(expected_generation) != current_generation
        ) or (
            epoch_sha256
            and (session_key, epoch_sha256)
            in _retired_session_capability_epochs
        ):
            return False
        _session_yolo.add(session_key)
        _session_yolo_generations[session_key] = current_generation
    _release_permission_mode_dependents(session_key)
    return True


def disable_session_yolo(
    session_key: str,
    *,
    expected_generation: int | None = None,
) -> bool:
    """Disable YOLO bypass for a single session key."""
    if not session_key:
        return False
    with _lock:
        if (
            expected_generation is not None
            and int(expected_generation)
            != _current_authority_generation_locked(session_key)
        ):
            return False
        _session_yolo.discard(session_key)
        _session_yolo_generations.pop(session_key, None)
    _release_permission_mode_dependents(session_key)
    return True


def revoke_session_capabilities_durably(
    session_key: str,
    *,
    reason: str = "gateway_session_boundary",
) -> dict:
    """Durably tombstone the exact runtime-bound session epoch.

    The caller must first bind the old trusted SessionContext.  No session or
    epoch value is accepted as payload authority: both are re-derived from that
    isolated context and the raw session key is checked against its digest.
    """

    session_key = str(session_key or "").strip()
    if not session_key:
        raise ValueError("session_key is required for durable capability revoke")
    reason = str(reason or "gateway_session_boundary").strip()[:1000]
    if not reason:
        raise ValueError("durable capability revoke reason is required")

    writer_boundary_required = _writer_boundary_policy_required()
    try:
        from gateway.canonical_writer_boundary import (
            canonical_writer_call,
            trusted_runtime_envelope,
            writer_boundary_configured,
        )
        from gateway.canonical_writer_protocol import CanonicalWriterOperation

        use_privileged_writer = writer_boundary_configured()
    except Exception as exc:
        if writer_boundary_required:
            raise RuntimeError(
                "privileged Canonical writer is required for session rotation"
            ) from exc
        return {
            "success": True,
            "writer_required": False,
            "scope_revoked": False,
            "revoked": 0,
        }

    if not use_privileged_writer:
        if writer_boundary_required:
            raise RuntimeError(
                "privileged Canonical writer is required for session rotation"
            )
        return {
            "success": True,
            "writer_required": False,
            "scope_revoked": False,
            "revoked": 0,
        }

    session_hash, capability_epoch_sha256 = _validated_writer_capability_binding(
        session_key,
        trusted_runtime_envelope(),
    )
    result = canonical_writer_call(
        CanonicalWriterOperation.CAPABILITY_REVOKE_SESSION.value,
        {"reason": reason},
        idempotency_key=(
            "capability-revoke-session:"
            f"{session_hash}:{capability_epoch_sha256}"
        ),
    )
    revocation_event_id = result.get("revocation_event_id")
    try:
        parsed_event_id = uuid.UUID(str(revocation_event_id))
        canonical_event_id = str(parsed_event_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError(
            "privileged writer did not confirm the exact session-epoch tombstone"
        ) from exc
    inserted = result.get("inserted")
    deduped = result.get("deduped")
    if (
        result.get("success") is not True
        or result.get("session_key_sha256") != session_hash
        or result.get("capability_epoch_sha256") != capability_epoch_sha256
        or result.get("scope_type") != "session"
        or result.get("scope_revoked") is not True
        or result.get("authority_active") is not False
        or parsed_event_id.int == 0
        or str(revocation_event_id) != canonical_event_id
        or type(inserted) is not bool
        or type(deduped) is not bool
        or inserted is deduped
    ):
        raise RuntimeError(
            "privileged writer did not confirm the exact session-epoch tombstone"
        )
    return dict(result)


def clear_session_local(
    session_key: str,
    *,
    retire_capability_epoch_sha256: str = "",
) -> None:
    """Atomically fence and clear local authority for one routing key."""

    session_key = str(session_key or "")
    if not session_key:
        return
    retired_epoch = str(retire_capability_epoch_sha256 or "").strip()
    with _lock:
        _session_authority_generations[session_key] = (
            _current_authority_generation_locked(session_key) + 1
        )
        if retired_epoch:
            _retired_session_capability_epochs.add((session_key, retired_epoch))
        _plan_capabilities.pop(session_key, None)
        for consume_key in tuple(_plan_capability_consume_locks):
            if consume_key[0] == session_key:
                _plan_capability_consume_locks.pop(consume_key, None)
        _session_yolo.discard(session_key)
        _session_yolo_generations.pop(session_key, None)
        entries = _gateway_queues.pop(session_key, [])
    for entry in entries:
        # Session-boundary cleanup should cancel any blocked approval waits
        # immediately so the old run can unwind instead of idling until timeout.
        entry.result = "deny"
        entry.event.set()
    _release_permission_mode_dependents(session_key)


def clear_session(session_key: str) -> None:
    """Best-effort durable revoke plus unconditional local session cleanup.

    SessionStore epoch rotations use ``revoke_session_capabilities_durably`` as
    a mandatory pre-publication gate and then call ``clear_session_local``.
    This compatibility helper remains best-effort for non-routing cleanup paths.
    """

    if not session_key:
        return
    try:
        revoke_session_capabilities_durably(
            session_key,
            reason="gateway_session_cleared",
        )
    except Exception as exc:
        logger.warning("Privileged session capability revoke failed: %s", exc)
    clear_session_local(session_key)


def is_session_yolo_enabled(session_key: str) -> bool:
    """Return True when YOLO bypass is enabled for a specific session."""
    # A parent's broad session bypass is not a delegable capability. Children
    # can consume an exact owner-approved command use, but cannot inherit YOLO
    # merely because they run under the same routing session.
    if is_delegated_exact_plan_consumer():
        return False
    if not session_key:
        return False
    observed_epoch = _observed_capability_epoch_sha256()
    with _lock:
        return bool(
            session_key in _session_yolo
            and _session_yolo_generations.get(session_key)
            == _current_authority_generation_locked(session_key)
            and not (
                observed_epoch
                and (session_key, observed_epoch)
                in _retired_session_capability_epochs
            )
        )


def is_current_session_yolo_enabled() -> bool:
    """Return True when the active approval session has YOLO bypass enabled."""
    return is_session_yolo_enabled(get_current_session_key(default=""))


def _observed_session_value(name: str) -> str:
    """Read one runtime-bound session value with the legacy env fallback."""
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env(name, "") or "").strip()
    except Exception:
        return str(os.getenv(name, "") or "").strip()


def _observed_session_user_id() -> str:
    """Return only the runtime-bound identity for the current agent turn."""
    return _observed_session_value("HERMES_SESSION_USER_ID")


def _observed_session_platform() -> str:
    """Return the normalized runtime-bound source platform."""
    return _observed_session_value("HERMES_SESSION_PLATFORM").casefold()


def _observed_session_message_id() -> str:
    """Return the current inbound message receipt id, never a model ref."""
    return _observed_session_value("HERMES_SESSION_MESSAGE_ID")


def _runtime_observed_approval_source_refs() -> dict[str, str]:
    """Build approval linkage solely from immutable runtime session context."""
    values = {
        "platform": _observed_session_platform(),
        "user_id": _observed_session_user_id(),
        "message_id": _observed_session_message_id(),
        "thread_id": _observed_session_value("HERMES_SESSION_THREAD_ID"),
        "chat_id": _observed_session_value("HERMES_SESSION_CHAT_ID"),
    }
    return {key: value for key, value in values.items() if value}


def _plan_approval_source_sha256(
    source_refs: Optional[dict],
    *,
    require_runtime_observed: bool = False,
    observed_platform: str = "",
    observed_user_id: str = "",
    observed_message_id: str = "",
) -> str:
    """Hash one approval turn without storing its raw content.

    Canonical/gateway authority is derived only from the current immutable
    runtime receipt.  ``source_refs`` remains a local/CLI compatibility input
    and is never an authority fallback in a scoped runtime.
    """
    if require_runtime_observed:
        if not observed_message_id:
            raise PermissionError(
                "plan capability requires the current runtime-observed owner message_id"
            )
        stable_ref = json.dumps(
            {
                "platform": observed_platform,
                "user_id": observed_user_id,
                "message_id": observed_message_id,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(stable_ref.encode("utf-8")).hexdigest()

    observed_message_id = _observed_session_message_id()
    refs = source_refs if isinstance(source_refs, dict) else {}
    stable_ref = (
        observed_message_id
        or str(refs.get("message_id") or refs.get("event_ref") or refs.get("manual_ref") or "").strip()
        or _approval_turn_id.get()
        or "unscoped-approval-turn"
    )
    return hashlib.sha256(stable_ref.encode("utf-8")).hexdigest()


def _canonical_brain_required() -> bool:
    """Return explicit Canonical policy, independent of helper availability.

    Once configuration enables Canonical Brain, missing code/credentials/DB
    access must fail later validation closed; it must never downgrade the
    approval path into a local capability.
    """
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
    except Exception:
        return False
    canonical = cfg.get("canonical_brain") if isinstance(cfg, dict) else None
    if not isinstance(canonical, dict):
        return False
    audit = canonical.get("audit_bridge")
    return bool(
        canonical.get("tools_enabled")
        or (isinstance(audit, dict) and audit.get("enabled"))
    )


def _writer_boundary_policy_required() -> bool:
    """Read only the static writer-boundary enablement policy.

    This deliberately does not probe code, the Unix socket, credentials, or
    service health. Once enabled, those failures must block capability use
    instead of silently falling back to process-local authority.
    """
    from gateway.canonical_writer_boundary import writer_boundary_policy_required

    return writer_boundary_policy_required()


def _validated_writer_capability_binding(
    session_key: str,
    runtime_envelope: dict,
) -> tuple[str, str]:
    """Return exact trusted session/epoch digests or fail closed.

    The routing session key is deterministic and survives ``/new``. Durable
    mutation authority therefore also requires the gateway-generated
    capability epoch, which rotates at every routing/security boundary.
    """

    expected_session_hash = hashlib.sha256(
        str(session_key or "").encode("utf-8")
    ).hexdigest()
    if runtime_envelope.get("session_key_sha256") != expected_session_hash:
        raise PermissionError(
            "plan capability session does not match the runtime-observed session"
        )
    capability_epoch_sha256 = str(
        runtime_envelope.get("capability_epoch_sha256") or ""
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", capability_epoch_sha256):
        raise PermissionError(
            "plan capability requires a trusted routing-boundary epoch"
        )
    return expected_session_hash, capability_epoch_sha256


def _canonical_active_plan_matches(
    *,
    case_id: str,
    plan_id: str,
    plan_revision: int | None = None,
) -> bool:
    """Mechanically verify the exact active plan in Canonical Brain.

    No free-form field is interpreted here.  Authority is bound only to the
    exact case id, exact plan id, and explicit ``state == active`` projection.
    Any query or shape failure is fail-closed.
    """
    try:
        from tools.canonical_brain_tool import canonical_active_plan_matches

        return bool(canonical_active_plan_matches(
            case_id=case_id,
            plan_id=plan_id,
            plan_revision=plan_revision,
        ))
    except Exception as exc:
        logger.warning("Canonical Brain active-plan validation failed: %s", exc)
        return False


def _canonical_active_plan_continues_approved_revision(
    *,
    case_id: str,
    plan_id: str,
    approved_plan_revision: int,
) -> bool:
    """Verify that the same plan remains active at or after approval.

    The owner approves an exact revision and an immutable set of command
    hashes.  Model-authored progress checkpoints may advance that same plan's
    revision without expanding those command hashes.  Supersession and
    terminal states stop matching because the active-plan lookup is exact on
    case and plan id.  Any malformed value or read failure remains fail-closed.
    """
    try:
        from tools.canonical_brain_tool import canonical_active_plan_revision

        active_revision = canonical_active_plan_revision(
            case_id=case_id,
            plan_id=plan_id,
        )
    except Exception as exc:
        logger.warning("Canonical Brain active-plan validation failed: %s", exc)
        return False
    return bool(
        isinstance(active_revision, int)
        and not isinstance(active_revision, bool)
        and isinstance(approved_plan_revision, int)
        and not isinstance(approved_plan_revision, bool)
        and approved_plan_revision >= 1
        and active_revision >= approved_plan_revision
    )


def _canonical_receipt_committed(result: object) -> bool:
    """Require a newly inserted row and verified readback for authority."""
    return bool(
        isinstance(result, dict)
        and result.get("success") is True
        and result.get("inserted") is True
        and result.get("readback_verified") is True
    )


def _reserve_plan_approval_source(approval_source_sha256: str) -> str:
    """Reserve one observed owner message against concurrent/replayed grants."""
    reservation = f"pending:{uuid.uuid4()}"
    with _lock:
        if approval_source_sha256 in _plan_approval_source_states:
            raise PermissionError(
                "runtime-observed approval message was already used for a plan capability"
            )
        _plan_approval_source_states[approval_source_sha256] = reservation
    return reservation


def _release_plan_approval_source(
    approval_source_sha256: str,
    reservation: str,
) -> None:
    """Release only this failed, not-yet-granted reservation."""
    with _lock:
        if _plan_approval_source_states.get(approval_source_sha256) == reservation:
            _plan_approval_source_states.pop(approval_source_sha256, None)


def _plan_capability_consume_lock(
    session_key: str,
    plan_id: str,
    command_sha256: str,
) -> threading.Lock:
    """Return the stable lock serializing one exact capability counter."""
    key = (session_key, plan_id, command_sha256)
    with _lock:
        return _plan_capability_consume_locks.setdefault(key, threading.Lock())


def grant_plan_capability(
    *,
    session_key: str,
    plan_id: str,
    plan_revision: int | None = None,
    exact_commands: list[str],
    approved_by_user_id: str,
    exact_code_scripts: Optional[list[str]] = None,
    ttl_seconds: int = 3600,
    max_uses_per_command: int = 3,
    canonical_case_id: str = "",
    source_refs: Optional[dict] = None,
) -> dict:
    """Grant expiring exact terminal/code capabilities for an approved plan.

    Hermes/GPT decides that the authenticated owner's current message approves
    the plan. This function only verifies identity/config and hashes exact
    terminal/code subjects; it performs no semantic approval classification.
    """
    if is_delegated_exact_plan_consumer():
        raise PermissionError(
            "delegated execution cannot grant or broaden plan authority"
        )
    from hermes_cli.config import load_config_readonly

    cfg = load_config_readonly() or {}
    approvals = cfg.get("approvals") if isinstance(cfg, dict) else {}
    approvals = approvals if isinstance(approvals, dict) else {}
    owners = {
        str(value).strip()
        for value in (approvals.get("plan_owner_user_ids") or [])
        if str(value).strip()
    }
    requested_approved_by_user_id = str(approved_by_user_id or "").strip()
    session_key = str(session_key or "").strip()
    plan_id = str(plan_id or "").strip()
    canonical_case_id = str(canonical_case_id or "").strip()
    if not session_key or not plan_id:
        raise ValueError("session_key and plan_id are required")
    authority_generation, authority_epoch_sha256 = (
        capture_session_authority_fence(session_key)
    )
    if (
        plan_revision is not None
        and (
            isinstance(plan_revision, bool)
            or not isinstance(plan_revision, int)
            or plan_revision < 1
        )
    ):
        raise ValueError("plan_revision must be a positive integer")
    if canonical_case_id and not canonical_case_id.startswith("case:"):
        raise ValueError("canonical_case_id must start with case:")
    if source_refs is not None and not isinstance(source_refs, dict):
        raise ValueError("source_refs must be an object")
    observed_user_id = _observed_session_user_id()
    observed_platform = _observed_session_platform()
    observed_message_id = _observed_session_message_id()
    # Gateway-scoped dangerous plan authority always depends on Canonical
    # Task Workspace in this fork. A transient config/helper outage must not
    # downgrade a live messaging turn into a local-only grant.
    writer_boundary_required = _writer_boundary_policy_required()
    canonical_required = (
        writer_boundary_required
        or _canonical_brain_required()
        or bool(observed_platform)
    )
    runtime_scoped = (
        canonical_required
        or bool(canonical_case_id)
        or bool(observed_platform)
    )
    if runtime_scoped and not observed_user_id:
        raise PermissionError(
            "plan capability requires a runtime-observed owner identity"
        )
    if (
        runtime_scoped
        and requested_approved_by_user_id
        and observed_user_id != requested_approved_by_user_id
    ):
        raise PermissionError(
            "plan capability owner does not match the runtime-observed user"
        )
    if runtime_scoped and observed_platform != "discord":
        raise PermissionError(
            "configured plan owner IDs are bound to the observed Discord platform"
        )
    if runtime_scoped and not observed_message_id:
        raise PermissionError(
            "plan capability requires the current runtime-observed owner message_id"
        )
    # Runtime identity is authoritative.  The model-tool dispatcher does not
    # pass user identity as a handler kwarg, and accepting a caller-supplied id
    # as a substitute would make the boundary forgeable.  Local non-gateway
    # callers retain the explicit argument for compatibility.
    approved_by_user_id = (
        observed_user_id if runtime_scoped else requested_approved_by_user_id
    )
    if not owners or approved_by_user_id not in owners:
        raise PermissionError(
            "plan capability requires an authenticated configured owner"
        )
    if canonical_required and not canonical_case_id:
        raise PermissionError(
            "Canonical Brain plan capability requires an exact canonical_case_id"
        )
    if canonical_required and plan_revision is None:
        raise PermissionError(
            "Canonical Brain plan capability requires an exact plan_revision"
        )
    if canonical_required and not _canonical_active_plan_matches(
        case_id=canonical_case_id,
        plan_id=plan_id,
        plan_revision=plan_revision,
    ):
        raise PermissionError(
            "Canonical Brain case does not contain the exact active plan_id/revision"
        )
    effective_plan_revision = plan_revision or 1
    if not isinstance(exact_commands, list):
        raise ValueError("exact_commands must be an array")
    if exact_code_scripts is None:
        exact_code_scripts = []
    if not isinstance(exact_code_scripts, list):
        raise ValueError("exact_code_scripts must be an array")
    exact_subject_count = len(exact_commands) + len(exact_code_scripts)
    if not 1 <= exact_subject_count <= 64:
        raise ValueError(
            "exact_commands and exact_code_scripts must contain 1..64 items total"
        )
    ttl_seconds = max(60, min(int(ttl_seconds), 8 * 3600))
    max_uses_per_command = max(1, min(int(max_uses_per_command), 10))
    try:
        from tools.terminal_tool import _get_env_config, get_session_cwd

        execution_env_config = _get_env_config()
        session_cwd = get_session_cwd(session_key) or ""
    except Exception:
        execution_env_config = {}
        session_cwd = ""
    backend_kind, resource_sha256 = _execution_resource_binding(
        env_config=execution_env_config,
    )
    terminal_cwd = _normalize_execution_cwd(
        session_cwd,
        env_type=backend_kind,
        env_config=execution_env_config,
    )
    command_uses: dict[str, int] = {}
    subject_bindings: dict[str, dict[str, str]] = {}
    for command in exact_commands:
        if not isinstance(command, str) or not command:
            raise ValueError("exact_commands cannot contain empty commands")
        subject = _exact_execution_subject(
            "terminal",
            command,
            env_type=backend_kind,
            env_config=execution_env_config,
            resource_sha256=resource_sha256,
            effective_cwd=terminal_cwd,
        )
        digest = subject["subject_sha256"]
        command_uses[digest] = max_uses_per_command
        subject_bindings[digest] = subject
    for code in exact_code_scripts:
        if not isinstance(code, str) or not code:
            raise ValueError("exact_code_scripts cannot contain empty scripts")
        subject = _exact_execution_subject(
            "execute_code",
            code,
            env_type=backend_kind,
            env_config=execution_env_config,
            resource_sha256=resource_sha256,
        )
        digest = subject["subject_sha256"]
        command_uses[digest] = max_uses_per_command
        subject_bindings[digest] = subject
    now = time.time()
    authority_source_refs = (
        _runtime_observed_approval_source_refs()
        if runtime_scoped
        else dict(source_refs or {})
    )
    approval_source_sha256 = _plan_approval_source_sha256(
        source_refs,
        require_runtime_observed=runtime_scoped,
        observed_platform=observed_platform,
        observed_user_id=observed_user_id,
        observed_message_id=observed_message_id,
    )
    try:
        from gateway.canonical_writer_boundary import (
            canonical_writer_call,
            trusted_runtime_envelope,
            writer_boundary_configured,
        )
        from gateway.canonical_writer_protocol import CanonicalWriterOperation

        use_privileged_writer = writer_boundary_configured()
    except Exception:
        use_privileged_writer = False
    if writer_boundary_required and not use_privileged_writer:
        raise RuntimeError(
            "privileged Canonical writer is required but unavailable"
        )
    if canonical_required and use_privileged_writer:
        runtime_envelope = trusted_runtime_envelope()
        expected_session_hash, capability_epoch_sha256 = (
            _validated_writer_capability_binding(
                session_key,
                runtime_envelope,
            )
        )
        expires_at = now + ttl_seconds
        approval_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            (
                "canonical-plan-approval:"
                f"{approval_source_sha256}:{canonical_case_id}:{plan_id}:"
                f"revision:{effective_plan_revision}:"
                f"{expected_session_hash}:{capability_epoch_sha256}"
            ),
        ))
        result = canonical_writer_call(
            CanonicalWriterOperation.CAPABILITY_GRANT.value,
            {
                "approval_id": approval_id,
                "case_id": canonical_case_id,
                "plan_id": plan_id,
                "plan_revision": effective_plan_revision,
                "approval_source_sha256": approval_source_sha256,
                "command_hashes": sorted(command_uses),
                "expires_at": dt.datetime.fromtimestamp(
                    expires_at,
                    dt.timezone.utc,
                ).replace(microsecond=0).isoformat(),
                "max_uses": max_uses_per_command,
            },
            idempotency_key=(
                "capability-grant:"
                f"{approval_source_sha256}:{plan_id}:"
                f"revision:{effective_plan_revision}:{capability_epoch_sha256}"
            ),
        )
        if (
            result.get("success") is not True
            or result.get("state") != "granted"
            or result.get("authority_active") is not True
            or result.get("session_key_sha256") != expected_session_hash
            or result.get("capability_epoch_sha256")
                != capability_epoch_sha256
            or result.get("approved_by_user_id") != approved_by_user_id
            or result.get("plan_revision") != effective_plan_revision
        ):
            raise RuntimeError(
                "privileged Canonical writer did not durably grant the capability:"
                + str(
                    result.get("error_code")
                    or result.get("state")
                    or "grant_blocked"
                )
            )
        return {
            **result,
            "approval_id": str(result.get("approval_id") or approval_id),
            "plan_id": plan_id,
            "plan_revision": effective_plan_revision,
            "state": str(result["state"]),
            "approved_by_user_id": approved_by_user_id,
            "session_key_sha256": expected_session_hash,
            "capability_epoch_sha256": capability_epoch_sha256,
            "command_hashes": sorted(command_uses),
            "execution_subjects": [
                subject_bindings[digest] for digest in sorted(subject_bindings)
            ],
            "command_count": len(command_uses),
            "expires_at_epoch": expires_at,
            "expires_in_seconds": ttl_seconds,
            "max_uses_per_command": max_uses_per_command,
            "approval_source_sha256": approval_source_sha256,
            "canonical_readback_verified": True,
        }
    with _lock:
        existing = _plan_capabilities.get(session_key, {}).get(plan_id)
        if (
            existing
            and authority_generation
            == _current_authority_generation_locked(session_key)
            and not (
                authority_epoch_sha256
                and (session_key, authority_epoch_sha256)
                in _retired_session_capability_epochs
            )
            and int(existing.get("_authority_generation", -1))
            == authority_generation
            and str(existing.get("_capability_epoch_sha256") or "")
            == authority_epoch_sha256
            and float(existing.get("expires_at") or 0) > now
            and existing.get("approved_by_user_id") == approved_by_user_id
            and set((existing.get("command_uses") or {}).keys()) == set(command_uses)
            and existing.get("subject_bindings") == subject_bindings
            and int(existing.get("max_uses_per_command") or 0) == max_uses_per_command
            and str(existing.get("canonical_case_id") or "") == canonical_case_id
            and int(existing.get("plan_revision") or 0) == effective_plan_revision
            and existing.get("approval_source_sha256") == approval_source_sha256
            and existing.get("durably_granted") is True
        ):
            return {
                "approval_id": existing["approval_id"],
                "plan_id": plan_id,
                "plan_revision": effective_plan_revision,
                "state": "granted",
                "approved_by_user_id": approved_by_user_id,
                "session_key_sha256": existing["session_key_sha256"],
                "command_hashes": sorted(command_uses),
                "execution_subjects": [
                    subject_bindings[digest]
                    for digest in sorted(subject_bindings)
                ],
                "command_count": len(command_uses),
                "granted_at": existing["granted_at"],
                "expires_at": dt.datetime.fromtimestamp(
                    existing["expires_at"], dt.timezone.utc
                ).replace(microsecond=0).isoformat(),
                "expires_at_epoch": existing["expires_at"],
                "expires_in_seconds": max(0, int(existing["expires_at"] - now)),
                "max_uses_per_command": max_uses_per_command,
                "approval_source_sha256": approval_source_sha256,
                "existing_capability": True,
            }
    approval_source_reservation = (
        _reserve_plan_approval_source(approval_source_sha256)
        if runtime_scoped
        else ""
    )
    expires_at = now + ttl_seconds
    approval_id = str(uuid.uuid4())
    capability = {
        "approval_id": approval_id,
        "plan_id": plan_id,
        "plan_revision": effective_plan_revision,
        "approved_by_user_id": approved_by_user_id,
        "session_key_sha256": hashlib.sha256(session_key.encode("utf-8")).hexdigest(),
        "expires_at": expires_at,
        "granted_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).replace(microsecond=0).isoformat(),
        "approval_source_sha256": approval_source_sha256,
        "command_uses": command_uses,
        "subject_bindings": subject_bindings,
        "execution_backend_kind": backend_kind,
        "execution_resource_sha256": resource_sha256,
        "consume_idempotency_receipts": {},
        "max_uses_per_command": max_uses_per_command,
        "canonical_case_id": canonical_case_id,
        "source_refs": authority_source_refs,
        "durably_granted": not canonical_case_id,
        "_authority_generation": authority_generation,
        "_capability_epoch_sha256": authority_epoch_sha256,
    }
    authority_stale = False
    with _lock:
        authority_stale = bool(
            authority_generation
            != _current_authority_generation_locked(session_key)
            or (
                authority_epoch_sha256
                and (session_key, authority_epoch_sha256)
                in _retired_session_capability_epochs
            )
        )
        if authority_stale:
            previous_capability = None
        else:
            session_plans = _plan_capabilities.setdefault(session_key, {})
            previous_capability = session_plans.get(plan_id)
            session_plans[plan_id] = capability
            if approval_source_reservation and not canonical_case_id:
                _plan_approval_source_states[approval_source_sha256] = (
                    f"used:{approval_id}"
                )
    if authority_stale:
        if approval_source_reservation:
            _release_plan_approval_source(
                approval_source_sha256,
                approval_source_reservation,
            )
        raise PermissionError(
            "plan capability session authority rotated before local grant"
        )
    receipt = {
        "approval_id": approval_id,
        "plan_id": plan_id,
        "plan_revision": effective_plan_revision,
        "state": "granted",
        "approved_by_user_id": approved_by_user_id,
        "session_key_sha256": capability["session_key_sha256"],
        "command_hashes": sorted(command_uses),
        "execution_subjects": [
            subject_bindings[digest] for digest in sorted(subject_bindings)
        ],
        "command_count": len(command_uses),
        "granted_at": capability["granted_at"],
        "expires_at": dt.datetime.fromtimestamp(expires_at, dt.timezone.utc).replace(microsecond=0).isoformat(),
        "expires_at_epoch": expires_at,
        "expires_in_seconds": ttl_seconds,
        "max_uses_per_command": max_uses_per_command,
        "approval_source_sha256": approval_source_sha256,
    }
    if canonical_case_id:
        try:
            from tools.canonical_brain_tool import record_plan_approval_receipt

            recorded = json.loads(record_plan_approval_receipt(
                case_id=canonical_case_id,
                receipt=receipt,
                source_refs=authority_source_refs,
            ))
        except Exception as exc:
            recorded = {"success": False, "error": str(exc)}
        if not _canonical_receipt_committed(recorded):
            with _lock:
                session_plans = _plan_capabilities.get(session_key, {})
                if session_plans.get(plan_id) is capability:
                    if previous_capability is None:
                        session_plans.pop(plan_id, None)
                    else:
                        session_plans[plan_id] = previous_capability
            if approval_source_reservation:
                _release_plan_approval_source(
                    approval_source_sha256,
                    approval_source_reservation,
                )
            raise RuntimeError(
                "canonical approval receipt was not durably verified; capability revoked"
            )
        with _lock:
            current = _plan_capabilities.get(session_key, {}).get(plan_id)
            if current is not capability:
                if approval_source_reservation:
                    _plan_approval_source_states[approval_source_sha256] = (
                        f"used:{approval_id}"
                    )
                raise RuntimeError(
                    "canonical approval capability changed before durable grant completed"
                )
            capability["durably_granted"] = True
            if approval_source_reservation:
                _plan_approval_source_states[approval_source_sha256] = (
                    f"used:{approval_id}"
                )
        receipt["canonical_event_id"] = recorded.get("event_id")
        receipt["canonical_readback_verified"] = bool(recorded.get("readback_verified"))
    return receipt


def consume_plan_capability(
    session_key: str,
    command: str,
    *,
    env_type: str = "",
    env_config: Optional[dict] = None,
    resource_sha256: str = "",
    effective_cwd: str = "",
    idempotency_key: str = "",
) -> str | None:
    """Consume one typed exact terminal-command use and return its plan id."""

    subject = _exact_execution_subject(
        "terminal",
        command,
        env_type=env_type,
        env_config=env_config,
        resource_sha256=resource_sha256,
        effective_cwd=effective_cwd,
    )
    return _consume_plan_capability_digest(
        session_key,
        subject,
        idempotency_key=idempotency_key,
    )


def consume_execute_code_plan_capability(
    session_key: str,
    code: str,
    *,
    env_type: str = "",
    env_config: Optional[dict] = None,
    resource_sha256: str = "",
    idempotency_key: str = "",
) -> str | None:
    """Consume one typed exact execute_code script use."""

    subject = _exact_execution_subject(
        "execute_code",
        code,
        env_type=env_type,
        env_config=env_config,
        resource_sha256=resource_sha256,
    )
    return _consume_plan_capability_digest(
        session_key,
        subject,
        idempotency_key=idempotency_key,
    )


def _consume_plan_capability_digest(
    session_key: str,
    subject: dict[str, str],
    *,
    idempotency_key: str = "",
) -> str | None:
    """Consume one mechanically identified, idempotent execution subject."""

    session_key = str(session_key or "")
    digest = str(subject.get("subject_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    observed_user_id = _observed_session_user_id()
    observed_platform = _observed_session_platform()
    try:
        authority_generation, authority_epoch_sha256 = (
            capture_session_authority_fence(session_key)
        )
    except PermissionError:
        logger.warning(
            "Plan capability rejected: calling session authority epoch is retired"
        )
        return None
    caller_idempotency_key = str(
        idempotency_key or _approval_tool_call_id.get() or uuid.uuid4().hex
    )
    idempotency_payload = json.dumps(
        {
            "version": 1,
            "session_key_sha256": hashlib.sha256(
                session_key.encode("utf-8")
            ).hexdigest(),
            "capability_epoch_sha256": authority_epoch_sha256,
            "subject_sha256": digest,
            "caller_idempotency_key": caller_idempotency_key,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    consume_idempotency_sha256 = hashlib.sha256(
        idempotency_payload.encode("utf-8")
    ).hexdigest()
    writer_boundary_required = _writer_boundary_policy_required()
    canonical_required = (
        writer_boundary_required
        or _canonical_brain_required()
        or bool(observed_platform)
    )

    try:
        from gateway.canonical_writer_boundary import (
            canonical_writer_call,
            trusted_runtime_envelope,
            writer_boundary_configured,
        )
        from gateway.canonical_writer_protocol import CanonicalWriterOperation

        use_privileged_writer = writer_boundary_configured()
    except Exception:
        use_privileged_writer = False
    if writer_boundary_required and not use_privileged_writer:
        logger.warning(
            "Privileged plan capability rejected: required writer unavailable"
        )
        return None
    if canonical_required and use_privileged_writer:
        if observed_platform != "discord" or not observed_user_id:
            logger.warning(
                "Privileged plan capability rejected: current Discord owner identity is missing"
            )
            return None
        runtime_envelope = trusted_runtime_envelope()
        if str(runtime_envelope.get("user_id") or "") != observed_user_id:
            logger.warning(
                "Privileged plan capability rejected: runtime owner identity mismatch"
            )
            return None
        try:
            _expected_session_hash, capability_epoch_sha256 = (
                _validated_writer_capability_binding(
                    session_key,
                    runtime_envelope,
                )
            )
        except PermissionError:
            logger.warning(
                "Privileged plan capability rejected: runtime session/epoch mismatch"
            )
            return None
        consume_idempotency_key = (
            f"capability-consume:{consume_idempotency_sha256}"
        )
        try:
            result = canonical_writer_call(
                CanonicalWriterOperation.CAPABILITY_CONSUME.value,
                {
                    "command_sha256": digest,
                    "idempotency_key": consume_idempotency_key,
                },
                idempotency_key=consume_idempotency_key,
            )
        except Exception as exc:
            logger.warning("Privileged plan capability consume failed: %s", exc)
            return None
        if (
            result.get("success") is not True
            or result.get("authorized") is not True
            or result.get("capability_epoch_sha256")
                != capability_epoch_sha256
            or result.get("command_sha256") != digest
            or result.get("approved_by_user_id") != observed_user_id
            or isinstance(result.get("plan_revision"), bool)
            or not isinstance(result.get("plan_revision"), int)
            or int(result["plan_revision"]) < 1
            or isinstance(result.get("active_plan_revision"), bool)
            or not isinstance(result.get("active_plan_revision"), int)
            or int(result["active_plan_revision"])
                < int(result["plan_revision"])
        ):
            logger.warning(
                "Privileged plan capability was not authorized: %s",
                result.get("error_code") or "not_authorized",
            )
            return None
        plan_id = str(result.get("plan_id") or "").strip()
        return plan_id or None

    with _lock:
        plans = _plan_capabilities.get(session_key, {})
        for plan_id, capability in list(plans.items()):
            if float(capability.get("expires_at") or 0) <= time.time():
                plans.pop(plan_id, None)
        candidate_plan_ids = [
            plan_id
            for plan_id, capability in plans.items()
            if capability.get("durably_granted") is True
            and int(capability.get("_authority_generation", -1))
            == authority_generation
            and str(capability.get("_capability_epoch_sha256") or "")
            == authority_epoch_sha256
            and (
                int((capability.get("command_uses") or {}).get(digest, 0)) > 0
                or (
                    (capability.get("consume_idempotency_receipts") or {}).get(
                        consume_idempotency_sha256
                    )
                    == digest
                )
            )
        ]

    for plan_id in candidate_plan_ids:
        consume_lock = _plan_capability_consume_lock(session_key, plan_id, digest)
        with consume_lock:
            with _lock:
                capability = _plan_capabilities.get(session_key, {}).get(plan_id)
                if not capability or capability.get("durably_granted") is not True:
                    continue
                if (
                    authority_generation
                    != _current_authority_generation_locked(session_key)
                    or (
                        authority_epoch_sha256
                        and (session_key, authority_epoch_sha256)
                        in _retired_session_capability_epochs
                    )
                    or int(capability.get("_authority_generation", -1))
                    != authority_generation
                    or str(capability.get("_capability_epoch_sha256") or "")
                    != authority_epoch_sha256
                ):
                    continue
                prior_idempotent_subject = (
                    capability.get("consume_idempotency_receipts") or {}
                ).get(consume_idempotency_sha256)
                if prior_idempotent_subject == digest:
                    return str(plan_id)
                if float(capability.get("expires_at") or 0) <= time.time():
                    _plan_capabilities.get(session_key, {}).pop(plan_id, None)
                    continue
                if int((capability.get("command_uses") or {}).get(digest, 0)) <= 0:
                    continue
                approved_by = str(capability.get("approved_by_user_id") or "")
                canonical_case_id = str(capability.get("canonical_case_id") or "")
                plan_revision = int(capability.get("plan_revision") or 0)

            runtime_scoped = (
                canonical_required
                or bool(canonical_case_id)
                or bool(observed_platform)
            )
            if runtime_scoped and observed_platform != "discord":
                logger.warning(
                    "Plan capability %s rejected: configured owner IDs require Discord",
                    plan_id,
                )
                continue
            if runtime_scoped and not observed_user_id:
                logger.warning(
                    "Plan capability %s rejected: runtime-observed owner is missing",
                    plan_id,
                )
                continue
            if observed_user_id and observed_user_id != approved_by:
                logger.warning(
                    "Plan capability %s rejected: runtime-observed user mismatch",
                    plan_id,
                )
                continue
            if canonical_required:
                if not canonical_case_id or not (
                    _canonical_active_plan_continues_approved_revision(
                        case_id=canonical_case_id,
                        plan_id=plan_id,
                        approved_plan_revision=plan_revision,
                    )
                ):
                    logger.warning(
                        "Plan capability %s rejected: no exact active Canonical Brain plan",
                        plan_id,
                    )
                    continue

            # Re-read after the durable plan check: a concurrent revoke/grant
            # must not let us consume a stale capability generation.
            with _lock:
                current = _plan_capabilities.get(session_key, {}).get(plan_id)
                if current is not capability:
                    continue
                remaining_before = int(
                    (capability.get("command_uses") or {}).get(digest, 0)
                )
                if (
                    capability.get("durably_granted") is not True
                    or authority_generation
                    != _current_authority_generation_locked(session_key)
                    or (
                        authority_epoch_sha256
                        and (session_key, authority_epoch_sha256)
                        in _retired_session_capability_epochs
                    )
                    or float(capability.get("expires_at") or 0) <= time.time()
                    or remaining_before <= 0
                ):
                    continue
                remaining_after = remaining_before - 1
                capability["command_uses"][digest] = remaining_after

            if canonical_case_id:
                receipt = {
                    "approval_id": capability["approval_id"],
                    "plan_id": plan_id,
                    "plan_revision": plan_revision,
                    "approved_by_user_id": approved_by,
                    "state": "authorized",
                    "session_key_sha256": capability["session_key_sha256"],
                    "command_sha256": digest,
                    "remaining_uses_for_command": remaining_after,
                    "checked_at": dt.datetime.now(dt.timezone.utc).replace(
                        microsecond=0
                    ).isoformat(),
                    "expires_at": dt.datetime.fromtimestamp(
                        capability["expires_at"], dt.timezone.utc
                    ).replace(microsecond=0).isoformat(),
                }
                try:
                    from tools.canonical_brain_tool import record_plan_capability_check

                    recorded = json.loads(record_plan_capability_check(
                        case_id=canonical_case_id,
                        receipt=receipt,
                        source_refs=capability.get("source_refs") or {},
                    ))
                except Exception as exc:
                    recorded = {"success": False, "error": str(exc)}
                if not _canonical_receipt_committed(recorded):
                    with _lock:
                        current = _plan_capabilities.get(session_key, {}).get(plan_id)
                        current_remaining = (
                            int((current.get("command_uses") or {}).get(digest, 0))
                            if current is capability
                            else None
                        )
                        if current is capability and current_remaining == remaining_after:
                            current["command_uses"][digest] = remaining_before
                    logger.warning(
                        "Plan capability %s was not consumed because a new, "
                        "verified Canonical Brain receipt was not inserted",
                        plan_id,
                    )
                    return None
            with _lock:
                current = _plan_capabilities.get(session_key, {}).get(plan_id)
                if current is not capability:
                    return None
                receipts = capability.setdefault(
                    "consume_idempotency_receipts",
                    {},
                )
                previous_subject = receipts.get(consume_idempotency_sha256)
                if previous_subject not in {None, digest}:
                    return None
                receipts[consume_idempotency_sha256] = digest
            return str(plan_id)
    return None


def revoke_plan_capability(session_key: str, plan_id: str) -> bool:
    """Remove one exact plan capability from its session."""
    writer_boundary_required = _writer_boundary_policy_required()
    try:
        from gateway.canonical_writer_boundary import (
            canonical_writer_call,
            trusted_runtime_envelope,
            writer_boundary_configured,
        )
        from gateway.canonical_writer_protocol import CanonicalWriterOperation

        use_privileged_writer = writer_boundary_configured()
    except Exception:
        use_privileged_writer = False
    if writer_boundary_required and not use_privileged_writer:
        logger.warning(
            "Privileged plan capability revoke blocked: required writer unavailable"
        )
        return False
    if use_privileged_writer:
        try:
            expected_session_hash, capability_epoch_sha256 = (
                _validated_writer_capability_binding(
                    session_key,
                    trusted_runtime_envelope(),
                )
            )
        except PermissionError:
            return False
        try:
            result = canonical_writer_call(
                CanonicalWriterOperation.CAPABILITY_REVOKE.value,
                {
                    "plan_id": str(plan_id or ""),
                    "reason": "plan_terminal_or_superseded",
                },
                idempotency_key=(
                    "capability-revoke:"
                    f"{expected_session_hash}:{capability_epoch_sha256}:{plan_id}"
                ),
            )
        except Exception as exc:
            logger.warning("Privileged plan capability revoke failed: %s", exc)
            return False
        return bool(
            result.get("success") is True
            and result.get("capability_epoch_sha256")
                == capability_epoch_sha256
            and int(result.get("revoked") or 0) > 0
        )
    try:
        authority_generation, authority_epoch_sha256 = (
            capture_session_authority_fence(session_key)
        )
    except PermissionError:
        return False
    with _lock:
        plans = _plan_capabilities.get(str(session_key or ""), {})
        capability = plans.get(str(plan_id or ""))
        if (
            not capability
            or authority_generation
            != _current_authority_generation_locked(str(session_key or ""))
            or int(capability.get("_authority_generation", -1))
            != authority_generation
            or str(capability.get("_capability_epoch_sha256") or "")
            != authority_epoch_sha256
        ):
            return False
        plans.pop(str(plan_id or ""), None)
        return True


# =========================================================================
# Approval prompting + orchestration
# =========================================================================

def prompt_dangerous_approval(command: str, description: str,
                              timeout_seconds: int | None = None,
                              allow_permanent: bool = True,
                              approval_callback=None,
                              allow_session: bool = True,
                              approval_id: str = "",
                              exact_execution: bool = False) -> str:
    """Prompt the user to approve one operation (CLI compatibility API).

    Args:
        allow_permanent: When False, hide the [a]lways option (used when
            the caller requires exact one-operation authority).
        allow_session: When False, expose only one-operation approval/deny.
        approval_id: Optional opaque request identity for structured clients.
        exact_execution: Marks the exact one-operation topology.
        approval_callback: Optional callback registered by the CLI for
            prompt_toolkit integration. Signature:
            (command, description, *, allow_permanent=True, ...) -> str.

    Returns: 'once', 'session', 'always', 'deny', or 'timeout'.
        'timeout' means the prompt expired without a user response — the
        action must still be blocked (fail-closed), but callers should
        report it as "no response" rather than an explicit user denial.
    """
    if timeout_seconds is None:
        timeout_seconds = _get_approval_timeout()

    if approval_callback is not None:
        try:
            callback_kwargs = {"allow_permanent": allow_permanent}
            # Preserve compatibility with third-party/legacy callbacks whose
            # keyword-only contract predates exact execution. The additional
            # identity/scope fields are sent only for the exact topology that
            # requires them.
            if not allow_session or approval_id or exact_execution:
                callback_kwargs.update({
                    "allow_session": allow_session,
                    "approval_id": approval_id,
                    "exact_execution": exact_execution,
                })
            return approval_callback(
                command,
                description,
                **callback_kwargs,
            )
        except Exception as e:
            logger.error("Approval callback failed: %s", e, exc_info=True)
            return "deny"

    # Fail-closed guard: if prompt_toolkit owns the terminal (interactive
    # CLI session) and no approval callback is registered on this thread,
    # the input() fallback below would spawn a daemon thread whose read
    # can never see Enter -- the user's keystrokes go to prompt_toolkit,
    # not input(), producing an invisible 60s deadlock (issue #15216).
    # Deny fast and log loudly instead so the caller can surface a real
    # error to the agent. Any thread that needs interactive approval must
    # install a callback via tools.terminal_tool.set_approval_callback()
    # before reaching this point (see delegate_tool.py, run_agent.py
    # _execute_tool_calls_concurrent / _spawn_background_review for the
    # established pattern).
    try:
        from prompt_toolkit.application.current import get_app_or_none
        if get_app_or_none() is not None:
            logger.warning(
                "Operation approval requested on a thread with no "
                "approval callback while prompt_toolkit is active; denying "
                "to avoid stdin deadlock. command=%r description=%r",
                command, description,
            )
            return "deny"
    except Exception:
        # prompt_toolkit not installed, or detection failed -- fall through
        # to the legacy input() path (safe in non-TUI contexts: scripts,
        # tests, sshd, etc.).
        pass

    os.environ["HERMES_SPINNER_PAUSE"] = "1"
    try:
        # Resolve the active UI language once per prompt so we don't re-read
        # config/YAML inside the retry loop below.
        from agent.i18n import t
        while True:
            print()
            print(f"  {t('approval.dangerous_header', description=description)}")
            if exact_execution:
                canonical_json = json.dumps(command, ensure_ascii=False)
                digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
                print("      Exact operation review (canonical UTF-8 JSON string):")
                print(canonical_json)
                print(f"      SHA-256: {digest}")
            else:
                print(f"      {command}")
            print()
            if not allow_session:
                print("      [o]nce  |  [d]eny")
            elif allow_permanent:
                print(t("approval.choose_long"))
            else:
                print(t("approval.choose_short"))
            print()
            sys.stdout.flush()

            result = {"choice": ""}

            def get_input():
                try:
                    if not allow_session:
                        prompt = "      Choice [o/D]: "
                    else:
                        prompt = (
                            t("approval.prompt_long")
                            if allow_permanent
                            else t("approval.prompt_short")
                        )
                    result["choice"] = input(prompt).strip().lower()
                except (EOFError, OSError):
                    result["choice"] = ""

            thread = threading.Thread(target=get_input, daemon=True)
            thread.start()
            thread.join(timeout=timeout_seconds)

            if thread.is_alive():
                print("\n" + t("approval.timeout"))
                # Distinct from an explicit deny: the user never answered.
                # Callers still block (fail-closed) but tell the agent the
                # prompt timed out instead of claiming the user refused.
                return "timeout"

            choice = result["choice"]
            if choice in {'o', 'once'}:
                print(t("approval.allowed_once"))
                return "once"
            elif choice in {'s', 'session'}:
                if not allow_session:
                    print(t("approval.denied"))
                    return "deny"
                print(t("approval.allowed_session"))
                return "session"
            elif choice in {'a', 'always'}:
                if not allow_session:
                    print(t("approval.denied"))
                    return "deny"
                if not allow_permanent:
                    print(t("approval.allowed_session"))
                    return "session"
                print(t("approval.allowed_always"))
                return "always"
            else:
                print(t("approval.denied"))
                return "deny"

    except (EOFError, KeyboardInterrupt):
        print("\n" + t("approval.cancelled"))
        return "deny"
    finally:
        if "HERMES_SPINNER_PAUSE" in os.environ:
            del os.environ["HERMES_SPINNER_PAUSE"]
        print()
        sys.stdout.flush()


def _normalize_approval_mode(mode) -> str:
    """Normalize approval mode values loaded from YAML/config.

    YAML 1.1 treats bare words like `off` as booleans, so a config entry like
    `approvals:\n  mode: off` is parsed as False unless quoted. Treat that as the
    intended string mode instead of falling back to manual approvals.

    Unknown string values (e.g. 'auto') are rejected with a warning rather than
    being silently accepted and falling through every mode check downstream.
    ``smart`` is intentionally downgraded to ``manual``: an auxiliary model
    must not decide authorization for the primary agent. Always returns one of
    ``manual`` or ``off``.
    """
    _VALID_MODES = ("manual", "off")
    if isinstance(mode, bool):
        return "off" if mode is False else "manual"
    if isinstance(mode, str):
        normalized = mode.strip().lower()
        if not normalized:
            return "manual"
        if normalized == "smart":
            logger.warning("approvals.mode='smart' is retired; using owner-driven manual approval")
            return "manual"
        if normalized in _VALID_MODES:
            return normalized
        logger.warning(
            "Unknown approvals.mode %r — defaulting to 'manual'. "
            "Valid values: %s",
            mode,
            ", ".join(_VALID_MODES),
        )
        return "manual"
    return "manual"


def _get_approval_config() -> dict:
    """Return the live, read-only approvals config cache entry.

    Callers must not mutate this mapping or nested values.
    """
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
        return config.get("approvals", {}) or {}
    except Exception as e:
        logger.warning("Failed to load approval config: %s", e)
        return {}


def _get_approval_mode() -> str:
    """Read the approval mode from config. Returns 'manual' or 'off'."""
    mode = _get_approval_config().get("mode", "manual")
    return _normalize_approval_mode(mode)


def is_approval_bypass_active_for_session(session_key: str) -> bool:
    """Return whether one exact session bypasses Hermes approval prompts.

    Collapses the canonical three-source bypass check used across the codebase
    into one place:
      - process-scoped ``--yolo`` / ``HERMES_YOLO_MODE`` (frozen at import time
        so a mid-process skill can't flip it — a prompt-injection escalation
        path; see ``_YOLO_MODE_FROZEN`` above),
      - the session-scoped gateway ``/yolo`` toggle,
      - ``approvals.mode: off`` in config.

    Scheduled jobs are governed by ``approvals.cron_mode`` instead of the
    interactive ``approvals.mode``. An explicit process/session yolo still
    wins, but a global interactive ``mode: off`` must not silently turn
    ``cron_mode: deny`` into approve.

    This is the pure whole-surface bypass expression. Exact plan and
    one-operation capabilities are checked at their own consumption boundary.
    """
    if _YOLO_MODE_FROZEN or is_session_yolo_enabled(session_key):
        return True
    if _is_cron_session():
        return _get_cron_approval_mode() == "approve"
    return _get_approval_mode() == "off"


def is_approval_bypass_active() -> bool:
    """Return whether the current approval context has bypass enabled."""
    return is_approval_bypass_active_for_session(
        get_current_session_key(default="")
    )


def _get_approval_timeout() -> int:
    """Read the approval timeout from config. Defaults to 300 seconds.

    The default matches DEFAULT_CONFIG["approvals"]["timeout"]. Gateway
    approvals arrive as push notifications the user may not see for a couple
    of minutes; 60s proved too tight in practice (Telegram taps landed after
    the wait had already failed closed).
    """
    try:
        return int(_get_approval_config().get("timeout", 300))
    except (ValueError, TypeError):
        return 300


def _get_cron_approval_mode() -> str:
    """Read the cron approval mode from config. Returns 'deny' or 'approve'."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
        mode = str(cfg_get(config, "approvals", "cron_mode", default="deny")).lower().strip()
        if mode in {"approve", "off", "allow", "yes"}:
            return "approve"
        return "deny"
    except Exception:
        return "deny"


def _should_skip_container_guards(env_type: str, has_host_access: bool = False) -> bool:
    """Return True when backend isolation authorizes the whole execution surface.

    Isolated container backends sandbox the agent away from the host, so their
    commands can't damage real files/services and we skip the approval layer.
    Docker is the exception once host paths are bind-mounted into the container:
    at that point a command like ``rm -rf /workspace`` reaches host files, so it
    must go through the normal approval flow.
    """
    if env_type == "docker":
        return not has_host_access
    return env_type in (
        "singularity",
        "modal",
        "daytona",
        "isolated_worker",
        "vercel_sandbox",
    )


def _delegated_exact_capability_required(execution_kind: str) -> dict:
    """Return the stable fail-closed contract for a delegated host execution.

    ``execution_kind`` is an exact tool identifier supplied by the caller, not
    a classification of command or code content.
    """

    return {
        "approved": False,
        "message": (
            f"BLOCKED: delegated {execution_kind} execution outside a "
            "mechanically isolated backend requires an unexpired exact "
            "owner-approved plan capability for this exact input."
        ),
        "status": "blocked",
        "outcome": "exact_plan_capability_required",
        "error_code": "delegated_exact_plan_capability_required",
        "user_consent": False,
    }


def _exact_plan_capability_required(execution_kind: str) -> dict:
    """Return the stable exact-authority miss contract for top-level work."""

    if is_delegated_exact_plan_consumer():
        return _delegated_exact_capability_required(execution_kind)
    return {
        "approved": False,
        "message": (
            f"BLOCKED: {execution_kind} execution is outside the exact "
            "subjects commissioned by the active owner-approved plan. Add "
            "this exact input to a new owner-approved plan revision or use "
            "the exact one-operation approval prompt."
        ),
        "status": "blocked",
        "outcome": "exact_plan_capability_required",
        "error_code": "exact_plan_capability_required",
        "user_consent": False,
    }


def _request_exact_execution_approval(
    *,
    session_key: str,
    execution_kind: str,
    raw_input: str,
    subject: dict[str, str],
    approval_callback=None,
) -> dict:
    """Transport one opaque exact-action grant without semantic policy.

    Only ``once`` can authorize the action.  Session/permanent responses are
    rejected rather than being converted into broad authority.
    """

    if is_delegated_exact_plan_consumer():
        return _delegated_exact_capability_required(execution_kind)
    # This exact owner surface is already identity-bound. Preserve the model's
    # bytes instead of applying a post-model keyword/regex rewrite. Exact-value
    # secret tainting belongs at the credential source boundary, not here.
    display_input = raw_input
    description = (
        f"exact {execution_kind} input is not present in the active plan "
        "capability; approval applies to this operation only"
    )
    exact_id = subject["subject_sha256"]
    is_gateway = _is_gateway_approval_context()
    is_ask = env_var_enabled("HERMES_EXEC_ASK")
    if is_gateway or is_ask:
        with _lock:
            notify_cb = _gateway_notify_cbs.get(session_key)
        if notify_cb is None:
            return _exact_plan_capability_required(execution_kind)
        decision = _await_gateway_decision(
            session_key,
            notify_cb,
            {
                "command": display_input,
                "command_sha256": exact_id,
                "pattern_key": f"exact:{execution_kind}:{exact_id}",
                "pattern_keys": [f"exact:{execution_kind}:{exact_id}"],
                "description": description,
                "allow_permanent": False,
                "allow_session": False,
                "exact_execution": True,
                "execution_kind": execution_kind,
                "backend_kind": subject["backend_kind"],
                "resource_sha256": subject["resource_sha256"],
                "cwd_sha256": subject["cwd_sha256"],
            },
            surface="gateway_exact_execution",
            include_authority_fence=True,
        )
        if decision.get("notify_failed"):
            result = _exact_plan_capability_required(execution_kind)
            result["message"] = decision.get("notify_model_message") or result["message"]
            boundary_code = (
                decision.get("notify_error_code") or "exact_approval_notify_failed"
            )
            result["outcome"] = boundary_code
            result["error_code"] = boundary_code
            return result
        if (
            decision.get("resolved") is not True
            or decision.get("choice") != "once"
            or decision.get("authority_stale") is True
        ):
            if decision.get("authority_stale") is True:
                outcome = "boundary_invalidated"
                reason = "was invalidated by a session boundary"
                addendum = " Old approval cannot authorize the new session."
            elif decision.get("resolved") is not True:
                outcome = "timeout"
                reason = "timed out without user response"
                addendum = " Silence is not consent."
            else:
                outcome = "denied"
                reason = "was denied by the user"
                addendum = ""
            deny_reason = str(decision.get("reason") or "").strip()
            reason_addendum = (
                f' Reason given by the user: "{deny_reason}".'
                if outcome == "denied" and deny_reason
                else ""
            )
            return {
                "approved": False,
                "message": (
                    f"BLOCKED: Exact {execution_kind} operation {reason}."
                    f"{reason_addendum} The user has NOT consented to this "
                    "action. Do NOT retry it, do NOT rephrase it, and do NOT "
                    "attempt the same outcome with a different command or "
                    f"tool.{addendum}"
                ),
                "status": "blocked",
                "outcome": outcome,
                "error_code": f"exact_execution_{outcome}",
                "user_consent": False,
                "deny_reason": deny_reason or None,
                "execution_subject_sha256": exact_id,
            }
        return {
            "approved": True,
            "message": None,
            "user_approved": True,
            "exact_one_operation": True,
            "execution_subject_sha256": exact_id,
        }

    if not _is_interactive_cli() and approval_callback is None:
        return _exact_plan_capability_required(execution_kind)
    pattern_key = f"exact:{execution_kind}:{exact_id}"
    _fire_approval_hook(
        "pre_approval_request",
        command=display_input,
        description=description,
        pattern_key=pattern_key,
        pattern_keys=[pattern_key],
        session_key=session_key,
        surface="cli",
    )
    choice = prompt_dangerous_approval(
        display_input,
        description,
        allow_permanent=False,
        approval_callback=approval_callback,
        allow_session=False,
        approval_id=uuid.uuid4().hex,
        exact_execution=True,
    )
    _fire_approval_hook(
        "post_approval_response",
        command=display_input,
        description=description,
        pattern_key=pattern_key,
        pattern_keys=[pattern_key],
        session_key=session_key,
        surface="cli",
        choice=choice,
    )
    if choice != "once":
        timed_out = choice == "timeout"
        outcome = "timeout" if timed_out else "denied"
        reason = (
            "timed out without user response"
            if timed_out
            else "was denied by the user"
        )
        addendum = " Silence is not consent." if timed_out else ""
        return {
            "approved": False,
            "message": (
                f"BLOCKED: Exact {execution_kind} operation {reason}. The "
                "user has NOT consented to this action. Do NOT retry it, do "
                "NOT rephrase it, and do NOT attempt the same outcome with a "
                f"different command or tool.{addendum}"
            ),
            "status": "blocked",
            "outcome": outcome,
            "error_code": f"exact_execution_{outcome}",
            "user_consent": False,
            "execution_subject_sha256": exact_id,
        }
    return {
        "approved": True,
        "message": None,
        "user_approved": True,
        "exact_one_operation": True,
        "execution_subject_sha256": exact_id,
    }


def _check_opaque_terminal_authority(
    command: str,
    env_type: str,
    *,
    approval_callback=None,
    has_host_access: bool = False,
    env_config: Optional[dict] = None,
    resource_sha256: str = "",
    effective_cwd: str = "",
    session_key: str = "",
    exact_authority: Optional[dict] = None,
) -> dict:
    """Resolve terminal execution without interpreting command prose."""

    if _should_skip_container_guards(
        env_type,
        has_host_access=has_host_access,
    ):
        return {"approved": True, "message": None}

    session_key = str(
        session_key
        or get_current_session_key(default="")
        or os.getenv("HERMES_SESSION_KEY", "")
        or "default"
    )

    exact_decision = exact_authority
    if exact_decision is None:
        exact_decision = check_exact_execution_authority(
            command,
            "terminal",
            env_type,
            env_config=env_config,
            resource_sha256=resource_sha256,
            effective_cwd=effective_cwd,
            session_key=session_key,
            has_host_access=has_host_access,
            approval_callback=approval_callback,
        )
    if exact_decision is not None:
        return exact_decision

    if _YOLO_MODE_FROZEN or is_current_session_yolo_enabled():
        return {"approved": True, "message": None}

    if _is_cron_session():
        if _get_cron_approval_mode() == "approve":
            return {"approved": True, "message": None}
        return {
            "approved": False,
            "message": (
                "BLOCKED: this profile does not authorize terminal execution "
                "for scheduled tasks. No command text was inspected."
            ),
            "status": "blocked",
            "outcome": "cron_terminal_execution_not_authorized",
            "error_code": "cron_terminal_execution_not_authorized",
            "user_consent": False,
        }

    if _get_approval_mode() == "off":
        return {"approved": True, "message": None}

    is_cli = _is_interactive_cli()
    is_gateway = _is_gateway_approval_context()
    is_ask = env_var_enabled("HERMES_EXEC_ASK")
    if (
        not is_cli
        and not is_gateway
        and not is_ask
        and approval_callback is None
    ):
        return _exact_plan_capability_required("terminal")

    subject = _exact_execution_subject(
        "terminal",
        command,
        env_type=env_type,
        env_config=env_config,
        resource_sha256=resource_sha256,
        effective_cwd=effective_cwd,
    )
    return _request_exact_execution_approval(
        session_key=session_key,
        execution_kind="terminal",
        raw_input=command,
        subject=subject,
        approval_callback=approval_callback,
    )


def check_exact_execution_authority(
    raw_input: str,
    execution_kind: str,
    env_type: str,
    *,
    env_config: Optional[dict] = None,
    resource_sha256: str = "",
    effective_cwd: str = "",
    session_key: str = "",
    has_host_access: bool = False,
    approval_callback=None,
) -> Optional[dict]:
    """Resolve exact authority before any legacy semantic approval path.

    ``None`` means this session has no exact-plan authority topology and the
    legacy compatibility path may continue.  Any returned decision is final.
    """

    if _should_skip_container_guards(
        env_type,
        has_host_access=has_host_access,
    ):
        return {"approved": True, "message": None}
    session_key = str(session_key or get_current_session_key())
    subject = _exact_execution_subject(
        execution_kind,
        raw_input,
        env_type=env_type,
        env_config=env_config,
        resource_sha256=resource_sha256,
        effective_cwd=effective_cwd,
    )
    if execution_kind == "terminal":
        plan_id = consume_plan_capability(
            session_key,
            raw_input,
            env_type=env_type,
            env_config=env_config,
            resource_sha256=subject["resource_sha256"],
            effective_cwd=effective_cwd,
        )
    else:
        plan_id = consume_execute_code_plan_capability(
            session_key,
            raw_input,
            env_type=env_type,
            env_config=env_config,
            resource_sha256=subject["resource_sha256"],
        )
    if plan_id:
        return {
            "approved": True,
            "message": None,
            "plan_capability": plan_id,
            "execution_subject_sha256": subject["subject_sha256"],
        }
    # Delegated workers may consume only exact subjects commissioned by their
    # trusted controller. A miss cannot broaden delegated authority.
    if is_delegated_exact_plan_consumer():
        return _delegated_exact_capability_required(execution_kind)

    # A top-level model tool call is the semantic decision for the current
    # owner-authenticated task. Exact plan capabilities remain useful as
    # auditable receipts, but a miss must not route command prose into a
    # regex/keyword policy or force a long task into micro-approvals. The
    # caller's explicit approval mode resolves the opaque action mechanically.
    return None



def _await_gateway_decision(session_key: str, notify_cb, approval_data: dict,
                            *, surface: str = "gateway",
                            include_authority_fence: bool = False) -> dict:
    """Enqueue *approval_data*, notify the user, and block the calling agent
    thread until the request is resolved or the gateway approval timeout
    elapses — firing pre/post approval hooks and cleaning up the queue entry.

    Shared by the terminal command guard (``check_all_command_guards``) and
    the execute_code guard (``check_execute_code_guard``) so the fiddly
    heartbeat-polling wait loop lives in one place.

    Returns ``{"resolved": bool, "choice": str|None}`` on completion, or
    ``{"resolved": False, "choice": None, "notify_failed": True}`` if the
    notify callback raised.  Persistence of an approved choice and building
    the final tool-facing result dict remain the caller's responsibility.
    """
    command = approval_data.get("command", "")
    description = approval_data.get("description", "")
    primary_key = approval_data.get("pattern_key", "")
    all_keys = approval_data.get("pattern_keys", [primary_key])

    capability_epoch_sha256 = _observed_capability_epoch_sha256()
    with _lock:
        authority_generation = _current_authority_generation_locked(session_key)
        if capability_epoch_sha256 and (
            session_key,
            capability_epoch_sha256,
        ) in _retired_session_capability_epochs:
            stale_result = {
                "resolved": True,
                "choice": "deny",
            }
            if include_authority_fence:
                stale_result.update({
                    "authority_stale": True,
                    "authority_generation": authority_generation,
                    "capability_epoch_sha256": capability_epoch_sha256,
                })
            return stale_result
        entry = _ApprovalEntry(
            approval_data,
            authority_generation=authority_generation,
            capability_epoch_sha256=capability_epoch_sha256,
        )
        _gateway_queues.setdefault(session_key, []).append(entry)

    def _drop_entry() -> None:
        with _lock:
            queue = _gateway_queues.get(session_key, [])
            if entry in queue:
                queue.remove(entry)
            if not queue:
                _gateway_queues.pop(session_key, None)

    # Notify plugins that an approval is being requested. Fires before the
    # gateway notify callback so observers get the event in real time.
    _fire_approval_hook(
        "pre_approval_request",
        command=command,
        description=description,
        pattern_key=primary_key,
        pattern_keys=list(all_keys),
        session_key=session_key,
        surface=surface,
    )

    # Notify the user (bridges sync agent thread → async gateway)
    try:
        notify_cb({
            key: value
            for key, value in entry.data.items()
            if not str(key).startswith("_")
        })
    except ApprovalNotifyBoundaryError as exc:
        logger.warning("Gateway approval boundary blocked notification: %s", exc.code)
        _drop_entry()
        notify_failure = {
            "resolved": False,
            "choice": None,
            "notify_failed": True,
            "notify_error_code": exc.code,
            "notify_model_message": exc.model_message,
        }
        if include_authority_fence:
            notify_failure.update({
                "authority_generation": entry.authority_generation,
                "capability_epoch_sha256": entry.capability_epoch_sha256,
            })
        return notify_failure
    except Exception as exc:
        logger.warning("Gateway approval notify failed: %s", exc)
        _drop_entry()
        _fire_approval_hook(
            "post_approval_response",
            command=command,
            description=description,
            pattern_key=primary_key,
            pattern_keys=list(all_keys),
            session_key=session_key,
            surface=surface,
            choice="notify_failed",
        )
        notify_failure = {
            "resolved": False,
            "choice": None,
            "notify_failed": True,
        }
        if include_authority_fence:
            notify_failure.update({
                "authority_generation": entry.authority_generation,
                "capability_epoch_sha256": entry.capability_epoch_sha256,
            })
        return notify_failure

    # Block until the user responds or the canonical approval timeout elapses
    # (default 300s). Poll in short slices so we can fire activity heartbeats
    # every ~10s to the agent's inactivity tracker — otherwise the gateway
    # watchdog kills the agent while the user is still responding. Mirrors
    # _wait_for_process() cadence.
    timeout = _get_approval_timeout()

    try:
        from tools.environments.base import touch_activity_if_due
    except Exception:  # pragma: no cover
        touch_activity_if_due = None

    _now = time.monotonic()
    _deadline = _now + max(timeout, 0)
    _activity_state = {"last_touch": _now, "start": _now}
    resolved = False
    while True:
        # Respect interrupt signals (e.g. /stop, /new, or an inactivity
        # timeout from the gateway) so a pending approval doesn't keep the
        # session wedged on threading.Event.wait() until the 5-minute approval
        # timeout. The wait runs on the agent's execution thread, which is the
        # exact thread AIAgent.interrupt() flags — so is_interrupted() here
        # sees the signal. Resolve as "deny" so the agent loop receives a
        # normal denial and unwinds cleanly (#8697).
        if is_interrupted():
            logger.info(
                "Approval wait interrupted by user signal — "
                "returning deny for session %s",
                session_key,
            )
            entry.result = "deny"
            entry.event.set()
            resolved = True
            break
        _remaining = _deadline - time.monotonic()
        if _remaining <= 0:
            break
        if entry.event.wait(timeout=min(1.0, _remaining)):
            resolved = True
            break
        if touch_activity_if_due is not None:
            touch_activity_if_due(_activity_state, "waiting for user approval")

    _drop_entry()

    choice = entry.result
    authority_stale = not session_authority_fence_is_current(
        session_key,
        entry.authority_generation,
        entry.capability_epoch_sha256,
    )
    if authority_stale:
        # A resolver can pop the entry immediately before boundary cleanup.
        # Its later signal must not turn that detached old waiter into consent
        # under the successor conversation generation.
        resolved = True
        choice = "deny"
    # Normalize outcome for the post hook. Unresolved (timeout) and None both
    # mean the user never responded; report that explicitly so plugins can
    # distinguish timeout from explicit deny.
    _outcome = "timeout" if not resolved else (choice if choice else "timeout")
    _fire_approval_hook(
        "post_approval_response",
        command=command,
        description=description,
        pattern_key=primary_key,
        pattern_keys=list(all_keys),
        session_key=session_key,
        surface=surface,
        choice=_outcome,
    )
    result = {
        "resolved": resolved,
        "choice": choice,
        "reason": entry.reason,
    }
    if include_authority_fence:
        result.update({
            "authority_stale": authority_stale,
            "authority_generation": entry.authority_generation,
            "capability_epoch_sha256": entry.capability_epoch_sha256,
        })
    return result


def check_all_command_guards(command: str, env_type: str,
                             approval_callback=None,
                             has_host_access: bool = False,
                             env_config: Optional[dict] = None,
                             resource_sha256: str = "",
                             effective_cwd: str = "",
                             session_key: str = "",
                             exact_authority: Optional[dict] = None) -> dict:
    """Resolve one opaque command using exact/owner-configured authority.

    No meaning is derived from command bytes. ``has_host_access`` is an exact
    backend property: a Docker sandbox with a host bind is not isolated and
    therefore follows the configured whole-surface authority mode.
    """
    return _check_opaque_terminal_authority(
        command,
        env_type,
        approval_callback=approval_callback,
        has_host_access=has_host_access,
        env_config=env_config,
        resource_sha256=resource_sha256,
        effective_cwd=effective_cwd,
        session_key=session_key,
        exact_authority=exact_authority,
    )


def check_execute_code_guard(code: str, env_type: str,
                             has_host_access: bool = False,
                             env_config: Optional[dict] = None,
                             resource_sha256: str = "",
                             session_key: str = "",
                             approval_callback=None) -> dict:
    """Resolve opaque Python execution using exact structural authority only.

    The script bytes are never parsed or classified. Isolated backends,
    exact owner-approved plan capabilities, process/session YOLO, cron's
    whole-surface mode, and the profile's whole-surface approval mode are the
    only authority inputs. Manual mode can grant one exact operation only.
    """
    if _should_skip_container_guards(
        env_type,
        has_host_access=has_host_access,
    ):
        return {"approved": True, "message": None}

    session_key = str(
        session_key
        or get_current_session_key(default="")
        or os.getenv("HERMES_SESSION_KEY", "")
        or "default"
    )

    exact_decision = check_exact_execution_authority(
        code,
        "execute_code",
        env_type,
        env_config=env_config,
        resource_sha256=resource_sha256,
        session_key=session_key,
        has_host_access=has_host_access,
        approval_callback=approval_callback,
    )
    if exact_decision is not None:
        return exact_decision

    if _YOLO_MODE_FROZEN or is_current_session_yolo_enabled():
        return {"approved": True, "message": None}

    if _is_cron_session():
        if _get_cron_approval_mode() == "approve":
            return {"approved": True, "message": None}
        return {
            "approved": False,
            "message": (
                "BLOCKED: this profile does not authorize execute_code for "
                "scheduled tasks. No script text was inspected."
            ),
            "status": "blocked",
            "outcome": "blocked",
            "error_code": "cron_execute_code_not_authorized",
            "user_consent": False,
        }

    if _get_approval_mode() == "off":
        return {"approved": True, "message": None}

    is_cli = _is_interactive_cli()
    is_gateway = _is_gateway_approval_context()
    is_ask = env_var_enabled("HERMES_EXEC_ASK")
    if (
        not is_cli
        and not is_gateway
        and not is_ask
        and approval_callback is None
    ):
        return _exact_plan_capability_required("execute_code")

    subject = _exact_execution_subject(
        "execute_code",
        code,
        env_type=env_type,
        env_config=env_config,
        resource_sha256=resource_sha256,
    )
    return _request_exact_execution_approval(
        session_key=session_key,
        execution_kind="execute_code",
        raw_input=code,
        subject=subject,
        approval_callback=approval_callback,
    )

# =========================================================================
# MCP elicitation entry point
# =========================================================================

def request_elicitation_consent(
    message: str,
    description: str,
    *,
    timeout_seconds: int | None = None,
    surface: str = "mcp-elicitation",
) -> str:
    """Route an MCP elicitation request to whichever approval surface owns
    the active session and return a normalized result.

    Gateway sessions (Telegram, Slack, Discord, etc.) go through
    ``_await_gateway_decision`` so the notify_cb posts a message and the
    agent thread blocks until the user responds via the platform UI.
    CLI/TUI sessions go through ``prompt_dangerous_approval``.

    Always fails closed: missing notify_cb in a gateway session, timeouts,
    and exceptions all map to ``"decline"`` so a server treats them as
    "user did not approve" rather than retrying or hanging.

    Returns one of ``"accept" | "decline" | "cancel"``.
    """
    try:
        session_key = get_current_session_key()
    except Exception as exc:  # pragma: no cover -- defensive
        logger.warning("Elicitation consent: session lookup failed: %s", exc)
        return "decline"

    if _is_gateway_approval_context():
        with _lock:
            notify_cb = _gateway_notify_cbs.get(session_key)
        if notify_cb is None:
            logger.warning(
                "Elicitation requested in gateway session %s but no "
                "notify_cb is registered — failing closed",
                session_key,
            )
            return "decline"

        approval_data = {
            "command": message,
            "command_sha256": _exact_command_sha256(message),
            "description": description,
            "pattern_key": "mcp_elicitation",
            "pattern_keys": ["mcp_elicitation"],
            "exact_execution": True,
            "allow_session": False,
            "allow_permanent": False,
        }
        try:
            decision = _await_gateway_decision(
                session_key,
                notify_cb,
                approval_data,
                surface=surface,
                include_authority_fence=True,
            )
        except Exception as exc:
            logger.error(
                "Elicitation gateway dispatch failed: %s", exc, exc_info=True,
            )
            return "decline"

        if decision.get("notify_failed") or decision.get("authority_stale"):
            return "decline"
        if not decision.get("resolved"):
            return "cancel"
        choice = decision.get("choice")
        if choice == "once" and session_authority_fence_is_current(
            session_key,
            decision["authority_generation"],
            decision.get("capability_epoch_sha256", ""),
        ):
            return "accept"
        return "decline"

    # CLI / TUI path. allow_permanent=False because elicitation is a
    # per-call confirmation — there is no pattern to remember.
    try:
        approval_id = uuid.uuid4().hex
        choice = prompt_dangerous_approval(
            message,
            description,
            timeout_seconds=timeout_seconds,
            allow_permanent=False,
            allow_session=False,
            approval_id=approval_id,
            exact_execution=True,
        )
    except Exception as exc:
        logger.error(
            "Elicitation CLI prompt failed: %s", exc, exc_info=True,
        )
        return "decline"

    if choice == "once":
        return "accept"
    if choice == "timeout":
        # Prompt expired without a user response — mirror the gateway's
        # unresolved outcome ("cancel") rather than an explicit decline.
        return "cancel"
    return "decline"
