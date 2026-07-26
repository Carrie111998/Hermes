"""Kanban dispatch-enforcement middleware.

Provides an opt-in pre-execution gate for controller sessions: when
``kanban.enforce_dispatch_routing`` is enabled in config, a controller
must establish an auditable dispatch route or bounded exemption before
substantial worker-like tool execution. The state resets each turn.

Read-only framing/verification/inspection tools are always permitted.
The gate fails closed with an actionable message directing the controller
to route or exempt.

Session-scoped state keyed by ``session_id`` with cleanup on session
start/end/reset.  Hook registration goes through the supported PluginContext
path (``plugins/kanban-enforcement/``) — no private ``pm._hooks`` mutation.
Prompt-cache byte stability is preserved because no system-prompt mutation
occurs.
"""

from __future__ import annotations

import json
import logging
import threading
import weakref
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------

# Tools that perform substantial worker-like execution — these are the
# operations that should be routed to a Flash worker, not performed
# inline by the Pro controller.  Blocking them forces the controller to
# record a dispatch decision (or exemption) before executing.
_SUBSTANTIAL_WORKER_TOOLS: frozenset[str] = frozenset(
    {
        "terminal",
        "write_file",
        "patch",
        "execute_code",
        "delegate_task",
        "process",
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_snapshot",
        "web_extract",
        "skill_manage",
        "memory",
        "todo",
    }
)

# Tools the controller may always use without a dispatch route — these
# are framing, verification, inspection, or purely controller-judgment
# operations that do not produce worker artifacts.
_ALWAYS_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "search_files",
        "web_search",
        "vision_analyze",
        "clarify",
        "skills_list",
        "skill_view",
        "session_search",
        "kanban_create",
        "kanban_show",
        "kanban_list",
        "kanban_comment",
        "kanban_link",
        "kanban_complete",
        "kanban_block",
        "kanban_unblock",
        "kanban_attach",
        "kanban_attach_url",
        "kanban_attachments",
        "kanban_heartbeat",
        "kanban_notify_subscribe",
        "kanban_notify_unsubscribe",
    }
)

# Dual-use tools where the operation matters, not just the tool name.
# For each tool, a dict mapping action names to whether they are
# "stateful" (require dispatch) or "read-only" (always allowed).
# If the args don't contain the action key, the tool is treated as
# substantial (fail-closed).
_DUAL_USE_TOOLS: Dict[str, Dict[str, bool]] = {
    "cronjob": {
        "list": False,      # read-only
        "create": True,     # stateful
        "update": True,     # stateful
        "pause": True,      # stateful
        "resume": True,     # stateful
        "remove": True,     # stateful
        "run": True,        # stateful
    },
    "process": {
        "list": False,      # read-only
        "poll": False,      # read-only (observational)
        "log": False,       # read-only (observational)
        "kill": True,       # stateful
        "write": True,      # stateful
        "submit": True,     # stateful
        "close": True,      # stateful
    },
}

# Top-level action parameter names used by dual-use tools.
_ACTION_PARAM_NAMES = ("action",)


def _is_substantial(tool_name: str, args: Optional[Dict[str, Any]] = None) -> bool:
    """Return True when *tool_name* with *args* requires dispatch authorization.

    Classification order:
    1. Always-allowed set → never substantial.
    2. Dual-use tool → check action parameter against per-tool action map.
    3. Substantial set → substantial.
    4. Unknown tool → fail-closed when enforcement is enabled (substantial),
       to prevent new core tools from accidentally bypassing enforcement.
    """
    # Always-allowed tools are never substantial.
    if tool_name in _ALWAYS_ALLOWED_TOOLS:
        return False

    # Dual-use: classify by action.
    if tool_name in _DUAL_USE_TOOLS:
        if isinstance(args, dict):
            # Try each known action parameter name.
            for action_key in _ACTION_PARAM_NAMES:
                action = args.get(action_key)
                if isinstance(action, str) and action:
                    action_lower = action.strip().lower()
                    action_map = _DUAL_USE_TOOLS[tool_name]
                    if action_lower in action_map:
                        return action_map[action_lower]
                    # Unknown action value → fail-closed (substantial).
                    logger.debug(
                        "dispatch enforcement: unknown action %r for dual-use tool %s",
                        action_lower, tool_name,
                    )
                    return True
        # No recognizable action param → fail-closed (substantial).
        logger.debug(
            "dispatch enforcement: no action param for dual-use tool %s",
            tool_name,
        )
        return True

    # Known substantial tools.
    if tool_name in _SUBSTANTIAL_WORKER_TOOLS:
        return True

    # Unknown tools: fail-closed when enforcement is enabled.
    # New core tools that are worker-like must be explicitly classified;
    # read-only inspection tools added in future versions will be caught
    # by the always-allowed check above if classified, or blocked here
    # until the classification is completed.
    logger.debug(
        "dispatch enforcement: unknown tool %s — treating as substantial (fail-closed)",
        tool_name,
    )
    return True


# ---------------------------------------------------------------------------
# Per-session dispatch state
# ---------------------------------------------------------------------------

class _DispatchState:
    """Dispatch-authorisation state for a single session."""

    def __init__(self) -> None:
        self.route_task_id: Optional[str] = None
        """Task id of the active dispatch route (verified from DB readback)."""

        self.route_assignee: Optional[str] = None
        """The worker-terra profile assigned."""

        self.route_model: Optional[str] = None
        """The model pinned for the worker (e.g. deepseek-v4-flash)."""

        self.route_provider: Optional[str] = None
        """The provider for the worker (e.g. new-api)."""

        self.exemption_keyword: Optional[str] = None
        """Bounded exemption keyword from DISPATCH_EXEMPTIONS."""

        self.turn_ordinal: int = -1
        """Turn ordinal when the current authorisation was established."""

    def is_established(self, current_turn: int) -> bool:
        """Return True when dispatch is authorised and not stale."""
        if self.route_task_id is None and self.exemption_keyword is None:
            return False
        if self.turn_ordinal != current_turn:
            return False
        return True

    def record_route(
        self,
        task_id: str,
        assignee: str,
        model: str,
        provider: str,
        turn_ordinal: int,
    ) -> None:
        self.route_task_id = task_id
        self.route_assignee = assignee
        self.route_model = model
        self.route_provider = provider
        self.exemption_keyword = None
        self.turn_ordinal = turn_ordinal

    def record_exemption(self, keyword: str, turn_ordinal: int) -> None:
        self.route_task_id = None
        self.route_assignee = None
        self.route_model = None
        self.route_provider = None
        self.exemption_keyword = keyword
        self.turn_ordinal = turn_ordinal

    def reset(self) -> None:
        """Clear all authorisation."""
        self.route_task_id = None
        self.route_assignee = None
        self.route_model = None
        self.route_provider = None
        self.exemption_keyword = None
        self.turn_ordinal = -1


# Per-session state: session_id → (_DispatchState, turn_counter).
# Guarded by _state_lock.  Cleaned up on on_session_end.
_state_lock = threading.Lock()
_session_states: Dict[str, _DispatchState] = {}
_session_turns: Dict[str, int] = {}

# Maximum session entries before eviction (LRU).
_MAX_TRACKED_SESSIONS = 256


def _resolve_session_id(session_id: str = "") -> Optional[str]:
    """Normalize session_id for state lookup. Returns None when unset."""
    sid = (session_id or "").strip()
    return sid if sid else None


def _get_or_create_state(session_id: str) -> tuple[_DispatchState, int]:
    """Return (state, turn_ordinal) for *session_id*, creating if needed."""
    sid = _resolve_session_id(session_id)
    if sid is None:
        # Anonymous / unsessioned call — use a sentinel key.
        sid = "__unsessioned__"
    with _state_lock:
        if sid not in _session_states:
            _session_states[sid] = _DispatchState()
            _session_turns[sid] = 0
            # Evict oldest entries if over capacity.
            while len(_session_states) > _MAX_TRACKED_SESSIONS:
                oldest = next(iter(_session_states))
                if oldest == sid:
                    break
                _session_states.pop(oldest, None)
                _session_turns.pop(oldest, None)
        return (_session_states[sid], _session_turns[sid])


def _current_turn_for(session_id: str) -> int:
    """Return the current turn ordinal for *session_id*."""
    _, turn = _get_or_create_state(session_id)
    return turn


def advance_turn_for(session_id: str) -> None:
    """Mark the start of a new turn — invalidates prior authorisation.

    Called from the post_llm_call hook when the model produces a final
    text response (not a tool call), signalling the end of a turn.
    """
    sid = _resolve_session_id(session_id) or "__unsessioned__"
    with _state_lock:
        _session_turns[sid] = _session_turns.get(sid, 0) + 1
        if sid in _session_states:
            _session_states[sid].reset()
    logger.debug("dispatch enforcement: advanced turn for %s to %d", sid,
                 _session_turns.get(sid, 0))


def cleanup_session(session_id: str) -> None:
    """Remove all state for a session. Idempotent.

    Called from on_session_end and on_session_reset hooks.
    """
    sid = _resolve_session_id(session_id)
    if sid is None:
        return
    with _state_lock:
        _session_states.pop(sid, None)
        _session_turns.pop(sid, None)
    logger.debug("dispatch enforcement: cleaned up session %s", sid)


def reset_all_state() -> None:
    """Reset all enforcement state. Used in tests only."""
    with _state_lock:
        _session_states.clear()
        _session_turns.clear()


# Legacy aliases for test compatibility — tests that don't pass session_id
# get the unsessioned sentinel.
def _get_state() -> _DispatchState:
    """Return state for the sentinel (test backwards-compat)."""
    state, _ = _get_or_create_state("")
    return state


def _current_turn() -> int:
    """Return turn for the sentinel (test backwards-compat)."""
    return _current_turn_for("")


def advance_turn() -> None:
    """Advance turn for the sentinel (test backwards-compat)."""
    advance_turn_for("")


def reset_enforcement_state() -> None:
    """Reset all state (test backwards-compat)."""
    reset_all_state()


# ---------------------------------------------------------------------------
# Enforcement configuration
# ---------------------------------------------------------------------------

_CONFIG_CACHE: Dict[str, Any] = {}
_CONFIG_LAST_READ: float = 0.0
_CONFIG_TTL_SECONDS: float = 5.0


def _is_enforcement_enabled() -> bool:
    """Check whether dispatch-routing enforcement is active.

    Reads the config with a short TTL so live ``hermes config set``
    changes take effect without a restart, but doesn't touch the
    filesystem on every single tool call.
    """
    global _CONFIG_CACHE, _CONFIG_LAST_READ
    import time as _time

    now = _time.time()
    if now - _CONFIG_LAST_READ < _CONFIG_TTL_SECONDS:
        return bool(_CONFIG_CACHE.get("enabled", False))

    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        enforce = bool(kanban_cfg.get("enforce_dispatch_routing", False))
        _CONFIG_CACHE = {"enabled": enforce}
    except Exception:
        enforce = bool(_CONFIG_CACHE.get("enabled", False))
    _CONFIG_LAST_READ = now
    return enforce


# ---------------------------------------------------------------------------
# Hook callbacks
# ---------------------------------------------------------------------------


def _pre_tool_call_enforcement(
    tool_name: str,
    args: Optional[Dict[str, Any]] = None,
    session_id: str = "",
    task_id: str = "",
    tool_call_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    middleware_trace: Optional[List[Dict[str, Any]]] = None,
    **extra: Any,
) -> Optional[Dict[str, Any]]:
    """Pre-tool-call hook: enforce dispatch routing for substantial tools.

    Returns ``{"action": "block", "message": "..."}`` when a substantial
    worker tool is called without an established dispatch route or
    exemption.  Returns ``None`` when the tool is allowed.
    """
    if not _is_enforcement_enabled():
        return None

    # Classify: always-allowed, dual-use by action, substantial, or unknown.
    if not _is_substantial(tool_name, args):
        return None

    sid = _resolve_session_id(session_id) or "__unsessioned__"
    state, current_turn = _get_or_create_state(session_id)

    if state.is_established(current_turn):
        return None

    # Build an actionable block message.
    if state.route_task_id is not None and state.turn_ordinal != current_turn:
        # Authorisation existed but was from a prior turn.
        detail = (
            "Dispatch authorisation from turn {} has expired. "
            "Re-establish a dispatch route or exemption for turn {}."
        ).format(state.turn_ordinal, current_turn)
    else:
        detail = (
            "No dispatch route or exemption is active for this turn. "
            "Before executing substantial work, the controller must either:\n"
            "  1. Create a kanban task with dispatch_decision "
            '{"route": "worker-terra", "model": "deepseek-v4-flash", '
            '"provider": "new-api"} to route execution to a Flash worker; or\n'
            "  2. Create a kanban task with dispatch_decision "
            '{"exemption": "<keyword>"} for a bounded exemption.\n'
            "Valid exemption keywords: tiny, requires_full_context, "
            "security_critical, controller_judgment, quality_escalation, "
            "already_running.\n"
            "Read-only tools (read_file, search_files, web_search, "
            "kanban_show, etc.) are always permitted."
        )

    message = (
        "BLOCKED: dispatch enforcement — {detail} "
        "Use kanban_create with dispatch_decision to route this work "
        "to worker-terra Flash, or record an exemption."
    ).format(detail=detail)

    logger.debug("dispatch enforcement blocked %s for %s: %s", tool_name, sid, detail[:120])
    return {"action": "block", "message": message}


def _post_tool_call_enforcement(
    tool_name: str,
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    session_id: str = "",
    task_id: str = "",
    tool_call_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    duration_ms: int = 0,
    status: Optional[str] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    middleware_trace: Optional[List[Dict[str, Any]]] = None,
    **extra: Any,
) -> None:
    """Post-tool-call hook: capture kanban_create dispatch decisions.

    When kanban_create succeeds with a dispatch_decision, verifies the
    durable task state from the DB (not just the caller's args), then
    records the route or exemption so subsequent substantial tools in
    the same turn are authorised.
    """
    if not _is_enforcement_enabled():
        return

    if tool_name != "kanban_create":
        return

    # Only act on successful tool calls.
    if status == "blocked" or (error_type is not None and error_type):
        return

    # Parse the result to extract the task_id.
    try:
        if isinstance(result, str):
            res = json.loads(result)
        elif isinstance(result, dict):
            res = result
        else:
            return
    except Exception:
        return

    # Successful kanban_create returns {"success": True, "task_id": "..."}
    if not res.get("success"):
        return
    created_task_id = res.get("task_id")
    if not created_task_id:
        return

    # Extract the dispatch_decision from the args.
    if not isinstance(args, dict):
        return
    dd = args.get("dispatch_decision")
    if not isinstance(dd, dict):
        return

    # --- Durable readback ---
    # Read the actual created task from the board to verify the dispatch
    # decision was persisted correctly.  Do not trust the caller's args
    # alone - the DB is the source of truth.
    #
    # Resolve board: prefer the board returned in the kanban_create result
    # (most reliable - it reflects the actual DB that accepted the write),
    # fall back to the board in the original args.
    result_board = res.get("board") if isinstance(res, dict) else None
    args_board = str(args.get("board", "")).strip() or None
    board = result_board or args_board
    readback_ok = _verify_dispatch_decision_from_db(
        created_task_id=str(created_task_id),
        expected_assignee=str(args.get("assignee", "")).strip() or None,
        expected_model=str(dd.get("model", "")).strip() or None,
        expected_provider=str(dd.get("provider", "")).strip() or None,
        exemption_keyword=str(dd.get("exemption", "")).strip() or None,
        board=board,
    )
    if not readback_ok:
        logger.warning(
            "dispatch enforcement: DB readback mismatch for task %s — "
            "dispatch NOT authorised (args cannot be trusted)",
            created_task_id,
        )
        return

    state, current_turn = _get_or_create_state(session_id)

    if "route" in dd:
        dd_route = str(dd.get("route", "")).strip()
        dd_model = str(dd.get("model", "")).strip()
        dd_provider = str(dd.get("provider", "")).strip()
        # Extract assignee from args.
        assignee = str(args.get("assignee", "")).strip()
        if not assignee:
            assignee = dd_route.lower()
        state.record_route(
            task_id=str(created_task_id),
            assignee=assignee,
            model=dd_model,
            provider=dd_provider,
            turn_ordinal=current_turn,
        )
        logger.debug(
            "dispatch enforcement: route established task=%s assignee=%s turn=%d",
            created_task_id, assignee, current_turn,
        )
    elif "exemption" in dd:
        exemption = str(dd.get("exemption", "")).strip()
        state.record_exemption(keyword=exemption, turn_ordinal=current_turn)
        logger.debug(
            "dispatch enforcement: exemption %s turn=%d", exemption, current_turn,
        )


def _verify_dispatch_decision_from_db(
    created_task_id: str,
    expected_assignee: Optional[str],
    expected_model: Optional[str],
    expected_provider: Optional[str],
    exemption_keyword: Optional[str],
    board: Optional[str] = None,
) -> bool:
    """Read back the created task from the DB and verify dispatch_decision.

    Uses ``kdb.get_task()`` and direct ``task_events`` query to verify
    the durable task state — the DB is the source of truth, not the
    caller's args.

    Returns True when:
    - The task exists and is not archived/done/blocked.
    - For routes: assignee, model_override, and provider_override match.
      A ``dispatch_routed`` event exists in task_events.
    - For exemptions: assignee matches (if provided), model/provider
      overrides are absent, and a ``dispatch_exempted`` event exists
      whose payload contains the exact exemption keyword.
    """
    try:
        from hermes_cli import kanban_db as kdb

        conn = kdb.connect(board=board)

        # Get the task by id.
        task = kdb.get_task(conn, created_task_id)
        if task is None:
            logger.warning(
                "dispatch enforcement: task %s not found in DB",
                created_task_id,
            )
            return False

        # Must not be in a terminal state — stale tasks don't authorize.
        task_status = (task.status or "").lower()
        if task_status in ("archived", "done", "blocked"):
            logger.warning(
                "dispatch enforcement: task %s is %s — cannot authorize",
                created_task_id, task_status,
            )
            return False

        if exemption_keyword:
            # Exemptions don't populate model/provider overrides.
            # Verify the assignee is as expected and neither model_override
            # nor provider_override was accidentally set.
            db_assignee = (task.assignee or "").strip().lower()
            expected_a = (expected_assignee or "").strip().lower()
            if expected_a and db_assignee != expected_a:
                logger.warning(
                    "dispatch enforcement: exemption task %s assignee mismatch "
                    "(expected %r, got %r)",
                    created_task_id, expected_a, db_assignee,
                )
                return False
            # Exemptions must NOT have model/provider overrides (that's a route).
            if task.model_override or task.provider_override:
                logger.warning(
                    "dispatch enforcement: exemption task %s has model/provider "
                    "overrides — looks like a route, not an exemption",
                    created_task_id,
                )
                return False
        else:
            # Route: verify assignee, model_override, provider_override match.
            db_assignee = (task.assignee or "").strip().lower()
            if expected_assignee and db_assignee != expected_assignee.lower():
                logger.warning(
                    "dispatch enforcement: task %s assignee mismatch "
                    "(expected %r, got %r)",
                    created_task_id, expected_assignee, db_assignee,
                )
                return False

            db_model = (task.model_override or "").strip()
            if expected_model and db_model != expected_model:
                logger.warning(
                    "dispatch enforcement: task %s model_override mismatch "
                    "(expected %r, got %r)",
                    created_task_id, expected_model, db_model,
                )
                return False

            db_provider = (task.provider_override or "").strip()
            if expected_provider and db_provider != expected_provider:
                logger.warning(
                    "dispatch enforcement: task %s provider_override mismatch "
                    "(expected %r, got %r)",
                    created_task_id, expected_provider, db_provider,
                )
                return False

        # Verify the correct durable event exists in task_events.
        # Routes record dispatch_routed; exemptions record
        # dispatch_exempted.  For exemptions we additionally verify
        # the event payload contains the exact keyword (forged or
        # mismatched keywords must fail).
        try:
            if exemption_keyword:
                # Query for the exemption event and verify keyword in payload.
                row = conn.execute(
                    "SELECT payload FROM task_events "
                    "WHERE task_id = ? AND kind = 'dispatch_exempted' "
                    "LIMIT 1",
                    (created_task_id,),
                ).fetchone()
                if row is None:
                    logger.warning(
                        "dispatch enforcement: exemption task %s has no "
                        "dispatch_exempted event",
                        created_task_id,
                    )
                    return False
                # Verify the payload contains the exact exemption keyword.
                try:
                    payload = json.loads(row["payload"]) if row["payload"] else {}
                except Exception:
                    logger.warning(
                        "dispatch enforcement: exemption task %s has "
                        "unparseable event payload",
                        created_task_id,
                    )
                    return False
                payload_keyword = str(
                    payload.get("exemption", "")
                ).strip().lower()
                expected_kw = exemption_keyword.strip().lower()
                if payload_keyword != expected_kw:
                    logger.warning(
                        "dispatch enforcement: exemption task %s keyword "
                        "mismatch (expected %r, got %r in payload)",
                        created_task_id, expected_kw, payload_keyword,
                    )
                    return False
            else:
                row = conn.execute(
                    "SELECT 1 FROM task_events "
                    "WHERE task_id = ? AND kind = 'dispatch_routed' "
                    "LIMIT 1",
                    (created_task_id,),
                ).fetchone()
                if row is None:
                    logger.warning(
                        "dispatch enforcement: task %s has no "
                        "dispatch_routed event",
                        created_task_id,
                    )
                    return False
        except Exception:
            logger.exception(
                "dispatch enforcement: failed to query task_events for %s",
                created_task_id,
            )
            return False

        return True

    except Exception:
        logger.exception(
            "dispatch enforcement: DB readback failed for task %s",
            created_task_id,
        )
        return False


def _post_llm_call_enforcement(
    model: str = "",
    provider: str = "",
    session_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    response_text: str = "",
    tool_calls_count: int = 0,
    **extra: Any,
) -> None:
    """Post-LLM-call hook: advance turn on final text responses.

    When the model produces a text response (not a tool call), the turn
    is ending — invalidate the previous dispatch authorisation.
    """
    if not _is_enforcement_enabled():
        return

    # Only advance when the model responded with text, not when it
    # produced tool calls (the turn continues through tool execution).
    if tool_calls_count == 0:
        advance_turn_for(session_id)


def _on_session_start_enforcement(
    session_id: str = "",
    **extra: Any,
) -> None:
    """Clean up any stale state from a prior session with the same id."""
    if not _is_enforcement_enabled():
        return
    # Re-create ensures clean state for the new session.
    sid = _resolve_session_id(session_id)
    if sid:
        cleanup_session(sid)


def _on_session_end_enforcement(
    session_id: str = "",
    **extra: Any,
) -> None:
    """Release per-session state on session end."""
    if not _is_enforcement_enabled():
        return
    sid = _resolve_session_id(session_id)
    if sid:
        cleanup_session(sid)


# ---------------------------------------------------------------------------
# Hook registration (called by plugin __init__.py)
# ---------------------------------------------------------------------------

_registered: bool = False  # Backward-compatible observability flag only.
_registered_managers: "weakref.WeakSet[Any]" = weakref.WeakSet()
_registration_lock = threading.Lock()


def register_enforcement_hooks(ctx: Any) -> None:
    """Register enforcement hooks through the supported PluginContext path.

    Registration is idempotent per PluginContext. A newly discovered
    PluginManager receives a new context and must register its own callbacks;
    a process-global boolean would incorrectly skip registration after plugin
    rediscovery or manager replacement.
    """
    global _registered
    manager = getattr(ctx, "_manager", None)
    if manager is None:
        raise ValueError("PluginContext is missing its manager")
    with _registration_lock:
        if manager in _registered_managers:
            _registered = True
            return
        try:
            ctx.register_hook("pre_tool_call", _pre_tool_call_enforcement)
            ctx.register_hook("post_tool_call", _post_tool_call_enforcement)
            ctx.register_hook("post_llm_call", _post_llm_call_enforcement)
            ctx.register_hook("on_session_start", _on_session_start_enforcement)
            ctx.register_hook("on_session_end", _on_session_end_enforcement)
            _registered_managers.add(manager)
            _registered = True
            logger.info("dispatch enforcement hooks registered via plugin path")
        except Exception:
            logger.exception("failed to register dispatch enforcement hooks")


# ---------------------------------------------------------------------------
# Public API for tests and other callers
# ---------------------------------------------------------------------------


def dispatch_enforcement_is_established(session_id: str = "") -> bool:
    """Return True if dispatch is authorised for the current turn."""
    state, turn = _get_or_create_state(session_id)
    return state.is_established(turn)


def dispatch_enforcement_summary(session_id: str = "") -> Dict[str, Any]:
    """Return the current enforcement state for diagnostics."""
    state, current_turn = _get_or_create_state(session_id)
    return {
        "established": state.is_established(current_turn),
        "current_turn": current_turn,
        "established_at_turn": state.turn_ordinal,
        "route_task_id": state.route_task_id,
        "route_assignee": state.route_assignee,
        "route_model": state.route_model,
        "route_provider": state.route_provider,
        "exemption_keyword": state.exemption_keyword,
        "enforcement_enabled": _is_enforcement_enabled(),
    }


def set_enforcement_enabled_for_test(enabled: bool) -> None:
    """Override enforcement config for testing (no-op in production)."""
    global _CONFIG_CACHE, _CONFIG_LAST_READ
    import time as _time

    _CONFIG_CACHE = {"enabled": enabled}
    _CONFIG_LAST_READ = _time.time() + 3600  # long TTL for test stability
