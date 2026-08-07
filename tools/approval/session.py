"""Per-session approval state, human-wait accounting, and allowlists.

Thread-safe per-session approval state keyed by session_key: pending entries,
session-approved sets, yolo flags, permanent allowlist, gateway approval
queues/notify callbacks, human-wait windows, and denial-breaker tallies.
"""

import contextlib
import fnmatch
import logging
import re
import threading
import time
from typing import Optional

from tools.approval.context import get_current_session_key
from tools.approval.hardline import _approval_key_aliases

logger = logging.getLogger(__name__)

# Late-bound package access: tests monkeypatch these attributes on tools.approval
# and internal calls must observe the patches at call time.
import tools.approval as _approval_pkg  # noqa: E402

# =========================================================================
# Per-session approval state (thread-safe)
# =========================================================================

_lock = threading.Lock()
_pending: dict[str, dict] = {}
_session_approved: dict[str, set] = {}
_session_yolo: set[str] = set()
_permanent_approved: set = set()


# =========================================================================
# Human-wait accounting (per session)
# =========================================================================
# Tracks the wall-clock time the agent spends verifiably blocked on a HUMAN
# prompt (CLI approval prompt, gateway approval round-trip). The concurrent
# tool batch deadline in agent/tool_executor.py excludes this time so a slow
# human answer never times a batch out — but ONLY this time. Measuring human
# waits at the source (rather than residency in the authorization gate, which
# is arbitrary code) is what keeps a wedged pre_tool_call plugin or a dead
# approval client from growing the exclusion 1:1 with wall clock and defeating
# the deadline entirely (#79719).
#
# Keyed by session so one gateway session's pending approval cannot extend a
# different session's batch deadline. State is process-global like the rest
# of this module's approval state; entries are bounded by _HUMAN_WAIT_MAX_SESSIONS.


class _HumanWaitState:
    __slots__ = ("pending", "window_started", "completed_seconds")

    def __init__(self) -> None:
        self.pending = 0
        self.window_started: float | None = None
        self.completed_seconds = 0.0


_human_wait_lock = threading.Lock()
_human_wait_states: dict[str, _HumanWaitState] = {}
_HUMAN_WAIT_MAX_SESSIONS = 256
# Margin added on top of approvals.timeout when clamping a window's
# contribution (read-side AND close-side) and when bounding the authorization
# gate's serialization-lock acquire in agent/tool_executor.py. One constant so
# the clamps can't drift apart.
HUMAN_WAIT_MARGIN_S = 60.0


def human_wait_ceiling() -> float:
    # Imported lazily to break the session<->gate import cycle.
    """Max seconds a single window may contribute: approvals.timeout + margin.

    Every legitimate human wait self-terminates at ``approvals.timeout`` (the
    CLI prompt join and the gateway poll loop both enforce it), so a window
    that overstays this ceiling is itself wedged and must not keep extending
    a batch deadline. Also used by agent/tool_executor.py as the bound on the
    authorization gate's serialization-lock acquire, so the two bounds cannot
    drift. Never call while holding ``_human_wait_lock`` — it reads the
    config cache.
    """
    from tools.approval import _get_approval_timeout
    return float(_get_approval_timeout()) + HUMAN_WAIT_MARGIN_S


def _clamped_window_seconds(started: float, now: float, ceiling: float) -> float:
    """Seconds an open window contributes: elapsed, floored at 0, capped.

    Shared by the close-time accrual in :func:`human_wait_window` and the
    open-window read in :func:`human_wait_seconds` so the two clamps stay
    identical by construction.
    """
    return min(max(0.0, now - started), ceiling)


def _human_wait_state(session_key: str) -> _HumanWaitState:
    """Return (creating if needed) the wait state for *session_key*.

    Caller must hold ``_human_wait_lock``. Evicts idle entries (no pending
    waiter) insertion-order-first until the table is under the cap so an army
    of short-lived session keys cannot grow it without bound. Entries with an
    open window are never evicted (that would corrupt live accounting), so
    the cap is best-effort under 256+ concurrently-pending sessions.
    """
    state = _human_wait_states.get(session_key)
    if state is None:
        if len(_human_wait_states) >= _HUMAN_WAIT_MAX_SESSIONS:
            for key in list(_human_wait_states):
                if len(_human_wait_states) < _HUMAN_WAIT_MAX_SESSIONS:
                    break
                if _human_wait_states[key].pending == 0:
                    del _human_wait_states[key]
        state = _HumanWaitState()
        _human_wait_states[session_key] = state
    return state


@contextlib.contextmanager
def human_wait_window(session_key: str | None = None):
    """Mark the enclosed block as time spent blocked on a human prompt.

    Wrap ONLY code that is genuinely parked waiting for a user's answer (the
    CLI approval prompt, the gateway approval poll loop). The concurrent tool
    batch deadline excludes this time; wrapping anything else re-creates the
    #79719 hang where arbitrary wedged code pushes the deadline out forever.

    Overlapping windows for the same session coalesce (pending counter), so
    two serialized approval prompts don't double-count the same wall clock.
    """
    key = session_key if session_key is not None else _approval_pkg.get_current_session_key()
    now = time.monotonic()
    with _human_wait_lock:
        state = _human_wait_state(key)
        if state.pending == 0:
            state.window_started = now
        state.pending += 1
    try:
        yield
    finally:
        now = time.monotonic()
        # Clamp the accrual too: a window that overstayed the ceiling was
        # wedged — record at most the ceiling instead of retroactively
        # injecting the whole overstay into the exclusion.
        ceiling = human_wait_ceiling()
        with _human_wait_lock:
            state = _human_wait_states.get(key)
            if state is not None:
                state.pending -= 1
                if state.pending == 0:
                    if state.window_started is not None:
                        state.completed_seconds += _clamped_window_seconds(
                            state.window_started, now, ceiling
                        )
                    state.window_started = None


def human_wait_seconds(session_key: str | None = None) -> float:
    """Return total human-wait seconds recorded for the session.

    Completed windows plus the currently open one (if any). Monotonically
    non-decreasing for the life of the process — except when an idle session's
    entry is evicted under cap pressure, which can only shrink a consumer's
    baseline delta to zero (the safe direction: the deadline fires sooner).
    Deadline consumers snapshot a baseline at batch start and use the delta.

    Each window's contribution is clamped to :func:`human_wait_ceiling`:
    every legitimate human wait self-terminates at ``approvals.timeout``
    (both the CLI prompt join and the gateway poll loop enforce it), so a
    window that overstays that bound is itself wedged and must not keep
    extending a batch deadline (belt-and-braces for #79719).
    """
    key = session_key if session_key is not None else _approval_pkg.get_current_session_key()
    now = time.monotonic()
    # Resolve the clamp outside the lock: it reads the config cache, which
    # must never nest under _human_wait_lock.
    ceiling = human_wait_ceiling()
    with _human_wait_lock:
        state = _human_wait_states.get(key)
        if state is None:
            return 0.0
        total = state.completed_seconds
        if state.window_started is not None:
            total += _clamped_window_seconds(state.window_started, now, ceiling)
        return total

# =========================================================================
# Consecutive-denial circuit breaker for smart approvals
# =========================================================================
# Nothing stops the model from retrying variants of a smart-denied command —
# each retry burns another guardian LLM call and agent iteration. After
# ``approvals.denial_breaker_threshold`` consecutive guardian DENY verdicts
# in one session (default 3; 0 disables), the deny message returned to the
# model escalates to a hard-stop instruction. Any approval resets the tally.
# This changes only the TOOL RESULT text — no message-history surgery, no
# interrupts — so it is prompt-cache-invariant by construction. Inspired by
# ChatGPT Work's auto-review circuit breaker (3 consecutive denials).
_denial_tally: dict[str, int] = {}
# Plain dict with a small cap so an army of short-lived session keys cannot
# grow it without bound; oldest (least recently denied) entries are evicted.
_DENIAL_TALLY_MAX_SESSIONS = 256


def _get_denial_breaker_threshold() -> int:
    # Imported lazily to break the session<->gate import cycle.
    """Read ``approvals.denial_breaker_threshold`` from config.

    Defaults to 3 consecutive guardian denials; 0 (or negative) disables
    the breaker entirely.
    """
    from tools.approval import _get_approval_config
    try:
        return int(_get_approval_config().get("denial_breaker_threshold", 3))
    except (ValueError, TypeError):
        return 3


def _record_denial(session_key: str) -> int:
    """Increment and return the session's consecutive guardian-denial count.

    Pop-and-reinsert keeps actively-denying sessions at the most-recent end
    of the dict so eviction (insertion-ordered) drops genuinely idle keys.
    """
    with _lock:
        count = _denial_tally.pop(session_key, 0) + 1
        _denial_tally[session_key] = count
        while len(_denial_tally) > _DENIAL_TALLY_MAX_SESSIONS:
            _denial_tally.pop(next(iter(_denial_tally)))
        return count


def _reset_denials(session_key: str) -> None:
    """Clear the session's consecutive-denial tally (an approval happened)."""
    with _lock:
        _denial_tally.pop(session_key, None)


def _denial_breaker_addendum(session_key: str) -> str:
    """Return the escalated hard-stop text when the breaker has tripped.

    Read-only: callers increment via :func:`_record_denial` on the guardian
    DENY verdict; this just checks the session's tally against the
    configured threshold. Returns '' below the threshold (or when
    disabled), otherwise a leading-space addendum the caller appends
    verbatim to the deny message returned to the model.
    """
    with _lock:
        count = _denial_tally.get(session_key, 0)
    threshold = _approval_pkg._get_denial_breaker_threshold()
    if threshold <= 0 or count < threshold:
        return ""
    logger.warning(
        "Smart-approval circuit breaker tripped for session %s: "
        "%d consecutive denials (threshold %d)",
        session_key, count, threshold,
    )
    return (
        f" CIRCUIT BREAKER: {count} consecutive commands were blocked by "
        "the security reviewer. STOP attempting variations of this "
        "operation. Report the blocked operation to the user and either "
        "ask them to run it manually or use /approve."
    )

# =========================================================================
# Blocking gateway approval (mirrors CLI's synchronous input() flow)
# =========================================================================
# Per-session QUEUE of pending approvals.  Multiple threads (parallel
# subagents, execute_code RPC handlers) can block concurrently — each gets
# its own threading.Event.  /approve resolves the oldest, /approve all
# resolves every pending approval in the session.


class _ApprovalEntry:
    """One pending dangerous-command approval inside a gateway session."""
    __slots__ = ("event", "data", "result", "reason")

    def __init__(self, data: dict):
        self.event = threading.Event()
        self.data = data          # command, description, pattern_keys, …
        self.result: Optional[str] = None  # "once"|"session"|"always"|"deny"
        # Optional free-text reason supplied with an explicit deny
        # (``/deny <reason>``) so the agent can adapt instead of only
        # hearing "denied". Ported from qwibitai/nanoclaw#2832.
        self.reason: Optional[str] = None


_gateway_queues: dict[str, list] = {}        # session_key → [_ApprovalEntry, …]
_gateway_notify_cbs: dict[str, object] = {}  # session_key → callable(approval_data)


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

    When *resolve_all* is True every pending approval in the session is
    resolved at once (``/approve all``).  Otherwise only the oldest one
    is resolved (FIFO).

    *reason* is an optional free-text explanation attached to an explicit
    deny (``/deny <reason>``).  It is relayed back to the agent in the
    BLOCKED message so it can adapt instead of only hearing "denied".

    Returns the number of approvals resolved (0 means nothing was pending).
    """
    with _lock:
        queue = _gateway_queues.get(session_key)
        if not queue:
            return 0
        if resolve_all:
            targets = list(queue)
            queue.clear()
        else:
            targets = [queue.pop(0)]
        if not queue:
            _gateway_queues.pop(session_key, None)

    for entry in targets:
        entry.result = choice
        if reason:
            entry.reason = reason
        entry.event.set()
    return len(targets)


def has_blocking_approval(session_key: str) -> bool:
    """Check if a session has one or more blocking gateway approvals waiting."""
    with _lock:
        return bool(_gateway_queues.get(session_key))


def submit_pending(session_key: str, approval: dict):
    """Store a pending approval request for a session."""
    with _lock:
        _pending[session_key] = approval


def approve_session(session_key: str, pattern_key: str):
    """Approve a pattern for this session only."""
    with _lock:
        _session_approved.setdefault(session_key, set()).add(pattern_key)


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


def enable_session_yolo(session_key: str) -> None:
    """Enable YOLO bypass for a single session key."""
    if not session_key:
        return
    with _lock:
        _session_yolo.add(session_key)
    _release_permission_mode_dependents(session_key)


def disable_session_yolo(session_key: str) -> None:
    """Disable YOLO bypass for a single session key."""
    if not session_key:
        return
    with _lock:
        _session_yolo.discard(session_key)
    _release_permission_mode_dependents(session_key)


def clear_session(session_key: str) -> None:
    """Remove all approval and yolo state for a given session."""
    if not session_key:
        return
    with _lock:
        _session_approved.pop(session_key, None)
        _session_yolo.discard(session_key)
        _pending.pop(session_key, None)
        entries = _gateway_queues.pop(session_key, [])
    for entry in entries:
        # Session-boundary cleanup should cancel any blocked approval waits
        # immediately so the old run can unwind instead of idling until timeout.
        entry.result = "deny"
        entry.event.set()
    _release_permission_mode_dependents(session_key)


def is_session_yolo_enabled(session_key: str) -> bool:
    """Return True when YOLO bypass is enabled for a specific session."""
    if not session_key:
        return False
    with _lock:
        return session_key in _session_yolo


def is_current_session_yolo_enabled() -> bool:
    """Return True when the active approval session has YOLO bypass enabled."""
    return is_session_yolo_enabled(_approval_pkg.get_current_session_key(default=""))


def is_approved(session_key: str, pattern_key: str) -> bool:
    """Check if a pattern is approved (session-scoped or permanent).

    Accept both the current canonical key and the legacy regex-derived key so
    existing command_allowlist entries continue to work after key migrations.
    """
    aliases = _approval_key_aliases(pattern_key)
    with _lock:
        if any(alias in _permanent_approved for alias in aliases):
            return True
        session_approvals = _session_approved.get(session_key, set())
        return any(alias in session_approvals for alias in aliases)


def approve_permanent(pattern_key: str):
    """Add a pattern to the permanent allowlist."""
    with _lock:
        _permanent_approved.add(pattern_key)


def load_permanent(patterns: set):
    """Bulk-load permanent allowlist entries from config."""
    with _lock:
        _permanent_approved.update(patterns)


_ALLOWLIST_SHELL_OPERATOR_RE = re.compile(r"(?:\n|&&|\|\||[;&|<>`]|\$\()")


def _has_allowlist_shell_operator(command: str) -> bool:
    """Return True when a command is too compound for the allowlist shortcut."""
    return bool(_ALLOWLIST_SHELL_OPERATOR_RE.search(command or ""))


def _command_matches_permanent_allowlist(command: str) -> bool:
    """Return True when command_allowlist contains this command or a glob.

    Permanent approvals historically store dangerous-pattern keys such as
    ``recursive delete``. Manual entries in ``command_allowlist`` are command
    text, and may include shell-style wildcards like ``podman *``.
    """
    command = (command or "").strip()
    if not command:
        return False
    if _has_allowlist_shell_operator(command):
        return False

    with _lock:
        patterns = tuple(_permanent_approved)

    for pattern in patterns:
        if not isinstance(pattern, str):
            continue
        pattern = pattern.strip()
        if not pattern:
            continue
        if command == pattern:
            return True
        if any(ch in pattern for ch in "*?[") and fnmatch.fnmatchcase(command, pattern):
            return True
    return False



# =========================================================================
# Config persistence for permanent allowlist
# =========================================================================

def load_permanent_allowlist() -> set:
    """Load permanently allowed command patterns from config.

    Also syncs them into the approval module so is_approved() works for
    patterns added via 'always' in a previous session.
    """
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
        patterns = set(config.get("command_allowlist", []) or [])
        if patterns:
            load_permanent(patterns)
        return patterns
    except Exception as e:
        logger.warning("Failed to load permanent allowlist: %s", e)
        return set()


def save_permanent_allowlist(patterns: set):
    """Save permanently allowed command patterns to config."""
    try:
        from hermes_cli.config import load_config, save_config
        config = load_config()
        config["command_allowlist"] = list(patterns)
        save_config(config)
    except Exception as e:
        logger.warning("Could not save allowlist: %s", e)


