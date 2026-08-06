"""
Session-scoped context variables for the Hermes gateway.

Replaces the previous ``os.environ``-based session state
(``HERMES_SESSION_PLATFORM``, ``HERMES_SESSION_CHAT_ID``, etc.) with
Python's ``contextvars.ContextVar``.

**Why this matters**

The gateway processes messages concurrently via ``asyncio``.  When two
messages arrive at the same time the old code did:

    os.environ["HERMES_SESSION_THREAD_ID"] = str(context.source.thread_id)

Because ``os.environ`` is *process-global*, Message A's value was
silently overwritten by Message B before Message A's agent finished
running.  Background-task notifications and tool calls therefore routed
to the wrong thread.

``contextvars.ContextVar`` values are *task-local*: each ``asyncio``
task (and any ``run_in_executor`` thread it spawns) gets its own copy,
so concurrent messages never interfere.

**Backward compatibility**

The public helper ``get_session_env(name, default="")`` mirrors the old
``os.getenv("HERMES_SESSION_*", ...)`` calls.  Existing tool code only
needs to replace the import + call site:

    # before
    import os
    platform = os.getenv("HERMES_SESSION_PLATFORM", "")

    # after
    from gateway.session_context import get_session_env
    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
"""

from contextlib import contextmanager
from contextvars import ContextVar
import re
import threading
from typing import Any, Iterator

# Sentinel to distinguish "never set in this context" from "explicitly set to empty".
# When a contextvar holds _UNSET, we fall back to os.environ (CLI/cron compat).
# When it holds "" (after clear_session_vars resets it), we return "" — no fallback.
_UNSET: Any = object()

# Process-level flag: has any code in this process bound a session via
# set_session_vars()? Concurrent multi-session hosts (the messaging gateway, the
# ACP adapter, the API server, the TUI, cron) all do; a pure single-process
# CLI/one-shot that never engages the session-context system does not.
#
# The subprocess-env bridge (tools/environments/local.py) reads this to choose
# its leak policy: when engaged, the ContextVars are authoritative and an _UNSET
# var means "no session bound in THIS task" — so a process-global os.environ
# mirror (written last-writer-wins by whatever concurrent session ran most
# recently) must NOT be inherited into a child process. When never engaged, the
# os.environ fallback is preserved (no concurrency to leak across). Monotonic
# latch — once any host binds a session, the process stays engaged for life.
_session_context_engaged: bool = False


def session_context_engaged() -> bool:
    """True if any session has been bound via set_session_vars in this process.

    See the ``_session_context_engaged`` comment for the leak-policy rationale.
    """
    return _session_context_engaged

# ---------------------------------------------------------------------------
# Per-task session variables
# ---------------------------------------------------------------------------

_SESSION_PLATFORM: ContextVar = ContextVar("HERMES_SESSION_PLATFORM", default=_UNSET)
_SESSION_SOURCE: ContextVar = ContextVar("HERMES_SESSION_SOURCE", default=_UNSET)
_SESSION_CHAT_ID: ContextVar = ContextVar("HERMES_SESSION_CHAT_ID", default=_UNSET)
_SESSION_CHAT_TYPE: ContextVar = ContextVar("HERMES_SESSION_CHAT_TYPE", default=_UNSET)
_SESSION_CHAT_NAME: ContextVar = ContextVar("HERMES_SESSION_CHAT_NAME", default=_UNSET)
_SESSION_THREAD_ID: ContextVar = ContextVar("HERMES_SESSION_THREAD_ID", default=_UNSET)
_SESSION_SCOPE_ID: ContextVar = ContextVar("HERMES_SESSION_SCOPE_ID", default=_UNSET)
# In-process transport provenance. This deliberately has no _VAR_MAP entry:
# adapter objects must never cross into subprocess environments.
_SESSION_TRANSPORT_ADAPTER: ContextVar = ContextVar(
    "HERMES_SESSION_TRANSPORT_ADAPTER", default=_UNSET
)


class _SlackHistoryTurnBudget:
    """One-shot, task-local authorization shared by copied contexts."""

    def __init__(self, authorized: bool) -> None:
        self.authorized = bool(authorized)
        self.consumed = False
        self.lock = threading.Lock()


_SESSION_SLACK_HISTORY_BUDGET: ContextVar = ContextVar(
    "HERMES_SESSION_SLACK_HISTORY_BUDGET", default=_UNSET
)
_SESSION_MOA_ACTIVE: ContextVar = ContextVar(
    "HERMES_SESSION_MOA_ACTIVE", default=False
)
_SESSION_USER_ID: ContextVar = ContextVar("HERMES_SESSION_USER_ID", default=_UNSET)
_SESSION_USER_NAME: ContextVar = ContextVar("HERMES_SESSION_USER_NAME", default=_UNSET)
_SESSION_KEY: ContextVar = ContextVar("HERMES_SESSION_KEY", default=_UNSET)
_SESSION_ID: ContextVar = ContextVar("HERMES_SESSION_ID", default=_UNSET)
# In-process UI session/window id for multi-session desktop/TUI hosts. This is
# intentionally separate from HERMES_SESSION_ID: the latter is the durable
# conversation/session-db id, while the UI id is the live frontend tab/window
# that commissioned a detached completion. Background completions use it as a
# precise return address so a stale/rotated durable session key cannot be
# consumed by whichever desktop poller wakes first.
_SESSION_UI_SESSION_ID: ContextVar = ContextVar("HERMES_UI_SESSION_ID", default=_UNSET)
# ID of the message that triggered the current turn. Used as a reply anchor
# so background-process notifications stay inside the originating Telegram
# private-chat topic (those lanes route only with thread id + reply anchor).
_SESSION_MESSAGE_ID: ContextVar = ContextVar("HERMES_SESSION_MESSAGE_ID", default=_UNSET)

_SESSION_PROFILE: ContextVar = ContextVar("HERMES_SESSION_PROFILE", default=_UNSET)

# Per-session cron marker. Unlike the process-global legacy env var, this is
# scoped to one cron job / inbound session. _UNSET preserves the legacy env
# fallback for CLI/tests; "1" marks cron; "" explicitly marks non-cron and
# masks any leaked process env value.
_CRON_SESSION: ContextVar = ContextVar("HERMES_CRON_SESSION", default=_UNSET)

# Whether the current session's delivery channel can route an ASYNC completion
# back to the agent AFTER the current turn ends (i.e. wake a fresh turn).
#
# True  — long-lived CLI sessions (in-process completion_queue drain) and the
#         real gateway platforms (Telegram/Discord/Slack/...), which hold a
#         persistent outbound channel and run the watcher/drain loops.
# False — finite runtimes that can end before a detached completion returns:
#         stateless API-server requests and dispatcher-spawned Kanban workers.
#
# Tools that promise async delivery (terminal notify_on_complete /
# watch_patterns, delegate_task background=True) read this via
# ``async_delivery_supported()`` and refuse to hand out a promise the channel
# can't keep — turning a silent no-op into an explicit contract.
#
# Default _UNSET => treated as supported, so CLI (which never sets a platform)
# and any contextvar-unaware path keep working. Stateless adapters opt OUT by
# setting ``supports_async_delivery = False`` on the adapter class; the gateway
# propagates that into this contextvar at session-bind time.
_SESSION_ASYNC_DELIVERY: ContextVar = ContextVar("HERMES_SESSION_ASYNC_DELIVERY", default=_UNSET)

# Cron auto-delivery vars — set per-job in run_job() so concurrent jobs
# don't clobber each other's delivery targets.
_CRON_AUTO_DELIVER_PLATFORM: ContextVar = ContextVar("HERMES_CRON_AUTO_DELIVER_PLATFORM", default=_UNSET)
_CRON_AUTO_DELIVER_CHAT_ID: ContextVar = ContextVar("HERMES_CRON_AUTO_DELIVER_CHAT_ID", default=_UNSET)
_CRON_AUTO_DELIVER_THREAD_ID: ContextVar = ContextVar("HERMES_CRON_AUTO_DELIVER_THREAD_ID", default=_UNSET)


_SLACK_HISTORY_EXPLICIT_PATTERNS = (
    re.compile(
        r"^\s*(?:<@[^>]+>\s*)?(?:kan\s+du\s+|sn[aä]lla\s+|v[aä]nligen\s+)?"
        r"(?:l[aä]s(?:a)?|h[aä]mta|visa|sammanfatta|analysera|se|ta\s+med|anv[aä]nd)\b"
        r"\s+(?:(?:de|den|det)\s+)?(?:(?:senaste|tidigare)\s+)?(?:slack[- ]?)?"
        r"(?:fr[aå]gorna|meddelandena|kontexten|historiken|diskussionen|tr[aå]den)\b"
        r"\s+(?:ovan|tidigare|senaste|i\s+(?:den\s+h[aä]r\s+)?(?:kanalen|tr[aå]den))\b"
        r"(?:\s+(?:och|sedan|d[aä]refter)\s+(?:"
        r"svara(?:\s+p[aå]\s+dem)?|sammanfatta(?:\s+vad\s+som\s+sagts)?|"
        r"analysera(?:\s+dem)?|ber[aä]tta(?:\s+vad\s+du\s+hittar)?|lista(?:\s+dem)?"
        r"))?\s*[.!?]?\s*$",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"^\s*(?:<@[^>]+>\s*)?(?:please\s+|(?:can|could|would)\s+you\s+)?"
        r"(?:read|fetch|show|summari[sz]e|analy[sz]e|include|use|look\s+at)\b"
        r"\s+(?:the\s+)?(?:(?:recent|previous|latest|earlier)\s+)?(?:slack[- ]?)?"
        r"(?:questions|messages|context|history|discussion|thread)\b"
        r"\s+(?:above|earlier|previous|latest|recent|in\s+(?:this|the)\s+(?:channel|thread))\b"
        r"(?:\s+(?:and|then)\s+(?:"
        r"answer(?:\s+them)?|summari[sz]e(?:\s+them)?|analy[sz]e(?:\s+them)?|"
        r"tell\s+me(?:\s+what\s+you\s+find)?|list(?:\s+them)?"
        r"))?\s*[.!?]?\s*$",
        re.IGNORECASE | re.DOTALL,
    ),
)

_SLACK_HISTORY_NEGATED_PATTERNS = (
    re.compile(
        r"\b(?:inte|ej|aldrig)\b.{0,120}\b"
        r"(?:fr[aå]gorna|meddelandena|kontexten|historiken|diskussionen|tr[aå]den)\b"
        r"|\b(?:l[aä]s|h[aä]mta|visa|sammanfatta|analysera|se|ta\s+med|anv[aä]nd)\b"
        r".{0,40}\b(?:inte|ej|aldrig)\b.{0,120}\b"
        r"(?:fr[aå]gorna|meddelandena|kontexten|historiken|diskussionen|tr[aå]den)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:do(?:es|did)?\s+.{0,20}\bnot|don't|doesn't|didn't|never)\b"
        r".{0,60}\b(?:read|fetch|show|summari[sz]e|analy[sz]e|include|use|look\s+at)\b"
        r".{0,120}\b(?:questions|messages|context|history|discussion|thread)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:nej|inte\s+till[aå]tet|inte\s+beh[oö]rig|[aå]terkall(?:ar|a|at)|"
        r"tar?\s+tillbaka|avbryt|stryk\s+det|g[oö]r\s+inte\s+det|"
        r"inte\s+ska\s+be\s+om)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:no|not\s+(?:allowed|authorized|authorised|permitted)|revoke[ds]?|"
        r"revoked|withdraw(?:n|s)?|cancel(?:led|s)?|take\s+it\s+back|"
        r"do\s+not|don't|scratch\s+that|never\s+mind|forget\s+(?:it|that)|"
        r"disregard\s+(?:it|that)|not\s+to\s+request)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:gl[oö]m\s+det|strunta\s+i\s+det|ignorera\s+det|"
        r"bortse\s+fr[aå]n\s+det)\b",
        re.IGNORECASE,
    ),
    # An imperative-shaped phrase can still be a quotation or description of
    # somebody else's instruction. Such text is not fresh user authorization.
    re.compile(
        r"\b(?:is|was)\s+what\b.{0,100}\b(?:said|told|asked|wrote)\b"
        r"|\b(?:is|was)\s+(?:an?\s+)?(?:example|quote|instruction)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:[aä]r|var)\s+vad\b.{0,100}\b(?:sa|sade|bad|skrev)\b"
        r"|\b(?:[aä]r|var)\s+(?:ett\s+)?(?:exempel|citat|instruktion)\b",
        re.IGNORECASE | re.DOTALL,
    ),
)


def is_explicit_slack_history_request(text: object) -> bool:
    """Return True only for a deterministic, explicit history request."""

    if not isinstance(text, str):
        return False
    normalized = " ".join(text.strip().split())
    if not normalized or len(normalized) > 4_000:
        return False
    if any(pattern.search(normalized) for pattern in _SLACK_HISTORY_NEGATED_PATTERNS):
        return False
    return any(pattern.search(normalized) for pattern in _SLACK_HISTORY_EXPLICIT_PATTERNS)


def consume_slack_history_authorization() -> bool:
    """Atomically consume this turn's single Slack-history read budget."""

    budget = _SESSION_SLACK_HISTORY_BUDGET.get()
    if budget is _UNSET or not isinstance(budget, _SlackHistoryTurnBudget):
        return False
    with budget.lock:
        if not budget.authorized or budget.consumed:
            return False
        budget.consumed = True
        return True


def slack_history_sensitive_context_active() -> bool:
    """Whether this turn has consumed private Slack history."""

    budget = _SESSION_SLACK_HISTORY_BUDGET.get()
    return bool(
        isinstance(budget, _SlackHistoryTurnBudget)
        and budget.authorized
        and budget.consumed
    )


def set_moa_turn_active(active: bool):
    """Bind whether this task-local turn fans out through MoA."""

    return _SESSION_MOA_ACTIVE.set(bool(active))


def reset_moa_turn_active(token) -> None:
    _SESSION_MOA_ACTIVE.reset(token)


def moa_turn_active() -> bool:
    return bool(_SESSION_MOA_ACTIVE.get())

_VAR_MAP = {
    "HERMES_SESSION_PLATFORM": _SESSION_PLATFORM,
    "HERMES_SESSION_SOURCE": _SESSION_SOURCE,
    "HERMES_SESSION_CHAT_ID": _SESSION_CHAT_ID,
    "HERMES_SESSION_CHAT_TYPE": _SESSION_CHAT_TYPE,
    "HERMES_SESSION_CHAT_NAME": _SESSION_CHAT_NAME,
    "HERMES_SESSION_THREAD_ID": _SESSION_THREAD_ID,
    "HERMES_SESSION_SCOPE_ID": _SESSION_SCOPE_ID,
    "HERMES_SESSION_USER_ID": _SESSION_USER_ID,
    "HERMES_SESSION_USER_NAME": _SESSION_USER_NAME,
    "HERMES_SESSION_KEY": _SESSION_KEY,
    "HERMES_SESSION_ID": _SESSION_ID,
    "HERMES_UI_SESSION_ID": _SESSION_UI_SESSION_ID,
    "HERMES_SESSION_MESSAGE_ID": _SESSION_MESSAGE_ID,
    "HERMES_SESSION_PROFILE": _SESSION_PROFILE,
    "HERMES_CRON_SESSION": _CRON_SESSION,
    "HERMES_CRON_AUTO_DELIVER_PLATFORM": _CRON_AUTO_DELIVER_PLATFORM,
    "HERMES_CRON_AUTO_DELIVER_CHAT_ID": _CRON_AUTO_DELIVER_CHAT_ID,
    "HERMES_CRON_AUTO_DELIVER_THREAD_ID": _CRON_AUTO_DELIVER_THREAD_ID,
}


def set_current_session_id(session_id: str) -> None:
    """Synchronize ``HERMES_SESSION_ID`` across ContextVar and ``os.environ``.

    Long-lived single-process entrypoints like the CLI can rotate sessions via
    ``/new``, ``/resume``, ``/branch``, or compression splits without
    reconstructing the entire agent. Tools still consult
    ``get_session_env("HERMES_SESSION_ID")`` with an ``os.environ`` fallback,
    so both storage paths must move together when the active session changes.

    Delegated subagent children are the exception: they are constructed inside
    the parent process within ``delegated_child_context()``, and their
    ``AIAgent.__init__`` calls this same helper. Writing a child's internal
    session id to ``os.environ`` (process-global) would clobber the parent's
    ``HERMES_SESSION_ID`` for the rest of the process — leaking the child id
    into parent tools and subprocesses spawned after the child was built. The
    ContextVar write below is task-local and safe for concurrent children; only
    the process-global ``os.environ`` mirror is suppressed for delegated
    children. Root agents (CLI, gateway, cron) keep both paths.
    """
    import os

    _SESSION_ID.set(session_id)

    # Skip the process-global os.environ write for delegated children. The
    # child's own tools and subprocesses still resolve their id through the
    # ContextVar (task-local), while the parent's process-wide env keeps the
    # parent's session identity. See HermesPRDelegationSessionContext task.
    try:
        from agent.delegation_context import is_delegated_child_context

        if is_delegated_child_context():
            return
    except Exception:
        pass

    os.environ["HERMES_SESSION_ID"] = session_id


@contextmanager
def scoped_current_session_id(session_id: str | None = None) -> Iterator[None]:
    """Bind a task-local session id and restore the prior value on exit.

    With ``session_id=None`` this acts as a save/restore boundary around code
    that may call :func:`set_current_session_id` itself (notably delegated
    ``AIAgent`` construction).  It intentionally never mutates ``os.environ``.
    """
    previous = _SESSION_ID.get()
    if session_id is not None:
        _SESSION_ID.set(session_id)
    try:
        yield
    finally:
        _SESSION_ID.set(previous)


def set_session_vars(
    platform: str = "",
    source: str = "",
    chat_id: str = "",
    chat_type: str = "",
    chat_name: str = "",
    thread_id: str = "",
    user_id: str = "",
    user_name: str = "",
    session_key: str = "",
    session_id: str = "",
    message_id: str = "",
    profile: str = "",
    cwd: str = "",
    async_delivery: bool = True,
    ui_session_id: str = "",
    cron_session: Any = _UNSET,
    scope_id: str = "",
    transport_adapter: Any = None,
    slack_history_authorized: bool = False,
) -> list:
    """Set all session context variables and return reset tokens.

    Call ``clear_session_vars(tokens)`` in a ``finally`` block when the handler
    exits. Note ``clear_session_vars`` resets every var to ``""`` (to suppress
    the ``os.environ`` fallback) rather than restoring prior values — these
    helpers are not nestable/stack-safe, and the returned tokens are accepted
    only for API compatibility.

    ``cwd`` pins the logical working directory for this context.

    ``async_delivery`` declares whether this session's channel can route a
    background completion back to the agent after the turn ends (see
    ``_SESSION_ASYNC_DELIVERY`` / ``async_delivery_supported``). Stateless
    request/response adapters (the API server) pass ``False``.

    ``cron_session`` is tri-state: ``_UNSET`` preserves legacy
    ``os.environ["HERMES_CRON_SESSION"]`` fallback, ``"1"`` marks a cron job,
    and ``""`` explicitly marks a non-cron session while masking leaked env.
    """
    # Mark the session-context machinery engaged for this process. The
    # subprocess-env bridge uses this to switch from "os.environ fallback" to
    # "ContextVar-authoritative, strip on _UNSET" — see session_context_engaged.
    global _session_context_engaged
    _session_context_engaged = True
    tokens = [
        _SESSION_PLATFORM.set(platform),
        _SESSION_SOURCE.set(source),
        _SESSION_CHAT_ID.set(chat_id),
        _SESSION_CHAT_TYPE.set(chat_type),
        _SESSION_CHAT_NAME.set(chat_name),
        _SESSION_THREAD_ID.set(thread_id),
        _SESSION_SCOPE_ID.set(scope_id),
        _SESSION_TRANSPORT_ADAPTER.set(transport_adapter),
        _SESSION_SLACK_HISTORY_BUDGET.set(
            _SlackHistoryTurnBudget(slack_history_authorized)
        ),
        _SESSION_USER_ID.set(user_id),
        _SESSION_USER_NAME.set(user_name),
        _SESSION_KEY.set(session_key),
        _SESSION_ID.set(session_id),
        _SESSION_UI_SESSION_ID.set(ui_session_id),
        _SESSION_MESSAGE_ID.set(message_id),
        _SESSION_PROFILE.set(profile),
        _CRON_SESSION.set(cron_session),
        _SESSION_ASYNC_DELIVERY.set(bool(async_delivery)),
    ]
    try:
        from agent.runtime_cwd import set_session_cwd

        set_session_cwd(cwd)
    except Exception:
        pass
    return tokens


def clear_session_vars(tokens: list) -> None:
    """Mark session context variables as explicitly cleared.

    Sets all variables to ``""`` so that ``get_session_env`` returns an empty
    string instead of falling back to (potentially stale) ``os.environ``
    values.  The *tokens* argument is accepted for API compatibility with
    callers that saved the return value of ``set_session_vars``, but the
    actual clearing uses ``var.set("")`` rather than ``var.reset(token)``
    to ensure the "explicitly cleared" state is distinguishable from
    "never set" (which holds the ``_UNSET`` sentinel).
    """
    for var in (
        _SESSION_PLATFORM,
        _SESSION_SOURCE,
        _SESSION_CHAT_ID,
        _SESSION_CHAT_TYPE,
        _SESSION_CHAT_NAME,
        _SESSION_THREAD_ID,
        _SESSION_SCOPE_ID,
        _SESSION_TRANSPORT_ADAPTER,
        _SESSION_SLACK_HISTORY_BUDGET,
        _SESSION_USER_ID,
        _SESSION_USER_NAME,
        _SESSION_KEY,
        _SESSION_ID,
        _SESSION_UI_SESSION_ID,
        _SESSION_MESSAGE_ID,
        _SESSION_PROFILE,
        _CRON_SESSION,
    ):
        var.set("")
    # Reset async-delivery capability to the "never set" sentinel rather than a
    # falsy value: a cleared context should fall back to the default-supported
    # behavior (CLI / unaware paths), not be mistaken for an opted-out
    # stateless adapter.
    _SESSION_ASYNC_DELIVERY.set(_UNSET)
    try:
        from agent.runtime_cwd import clear_session_cwd

        clear_session_cwd()
    except Exception:
        pass


def reset_session_vars() -> None:
    """Reset every session context variable to ``_UNSET`` for THIS context.

    Distinct from :func:`clear_session_vars`, which sets the vars to ``""``
    ("explicitly cleared" — suppresses the os.environ fallback and is used when
    a handler *finishes*).  This helper restores the ``_UNSET`` sentinel
    ("never bound in this context"), which is what a freshly-spawned task should
    look like *before* it binds its own session.

    🔴 Why this exists — the cross-session ContextVar inheritance leak.
    Each gateway message is processed in its own ``asyncio`` task, created via
    ``create_task`` (which snapshots the *current* context with
    ``copy_context``).  When message B's task is spawned from a context where a
    concurrent message A had already called :func:`set_session_vars`, B inherits
    A's **set** ContextVars.  Until B calls its own ``set_session_vars`` there is
    a window where any subprocess B spawns (e.g. a tool shelling out) reads
    *A's* ``HERMES_SESSION_*`` identity via the subprocess-env bridge.  The
    bridge's ``_UNSET``-strip guard cannot help: the vars are not ``_UNSET``,
    they are set-to-A.  Calling ``reset_session_vars`` at the top of the
    per-message handler drops the inherited identity so the window strips safe
    (no session) instead of leaking the foreign one; the handler then binds its
    own via ``set_session_vars`` a few steps later.  See
    tests/tools/test_local_env_session_leak.py and
    tests/gateway/test_session_context_inheritance.py.

    Note ``_SESSION_ASYNC_DELIVERY`` lives outside ``_VAR_MAP`` (it is a bool
    capability flag read via :func:`async_delivery_supported`, not a string
    ``HERMES_SESSION_*`` env var read via :func:`get_session_env`), so it is
    reset explicitly below. Without it, a task spawned from a context where a
    sibling adapter bound ``async_delivery=False`` (the stateless API server)
    inherits that ``False`` through the pre-bind window, and
    ``async_delivery_supported`` wrongly reports the new turn's channel as
    unable to route a background completion until ``set_session_vars`` runs.
    """
    for var in _VAR_MAP.values():
        var.set(_UNSET)
    _SESSION_TRANSPORT_ADAPTER.set(_UNSET)
    _SESSION_SLACK_HISTORY_BUDGET.set(_UNSET)
    # Reset the async-delivery capability to "never bound here" (_UNSET) for the
    # same inheritance-leak reason as the mapped vars above — see clear_session_vars,
    # which resets this var on the handler-exit path for the symmetric concern.
    _SESSION_ASYNC_DELIVERY.set(_UNSET)
    try:
        from agent.runtime_cwd import clear_session_cwd

        clear_session_cwd()
    except Exception:
        pass


def get_session_env(name: str, default: str = "") -> str:
    """Read a session context variable by its legacy ``HERMES_SESSION_*`` name.

    Drop-in replacement for ``os.getenv("HERMES_SESSION_*", default)``.

    Resolution order:
    1. Context variable (set by the gateway for concurrency-safe access).
       If the variable was explicitly set (even to ``""``) via
       ``set_session_vars`` or ``clear_session_vars``, that value is
       returned — **no fallback to os.environ**.
    2. ``os.environ`` (only when the context variable was never set in
       this context — i.e. CLI, cron scheduler, and test processes that
       don't use ``set_session_vars`` at all).
    3. *default*
    """
    import os

    var = _VAR_MAP.get(name)
    if var is not None:
        value = var.get()
        if value is not _UNSET:
            return value
    # Fall back to os.environ for CLI, cron, and test compatibility
    return os.getenv(name, default)


def get_session_transport_adapter() -> Any | None:
    """Return the exact in-process adapter that received the active turn."""

    adapter = _SESSION_TRANSPORT_ADAPTER.get()
    return None if adapter is _UNSET or adapter == "" else adapter


# Surfaces that are not a human chat channel. The gateway binds a platform
# value (``telegram``) to HERMES_SESSION_PLATFORM, while the CLI, TUI, and
# desktop bind HERMES_SESSION_SOURCE (``cli``, ``tui``, ``desktop``) and leave
# the platform empty — so both have to be consulted. ``local``, ``api_server``,
# ``webhook``, and ``msgraph_webhook`` are real Platform values that reach
# HERMES_SESSION_PLATFORM but have no attachment channel behind them.
# Default-deny: an unrecognized identity counts as messaging so a newly added
# chat platform is never treated as a private surface before this set is
# updated. Mirrors LOCAL_SESSION_SOURCE_IDS in
# apps/desktop/src/lib/session-source.ts; keep roughly in sync when adding a
# local or programmatic surface.
NON_MESSAGING_SESSION_SURFACES = frozenset(
    {
        "",
        "api_server",
        "cli",
        "codex",
        "desktop",
        "gateway",
        "kanban",
        "local",
        "msgraph_webhook",
        "tool",
        "tui",
        "webhook",
    }
)


def session_is_messaging_surface() -> bool:
    """Whether this turn is delivered over a human messaging channel.

    Callers use this to decide anything that differs between "the user is
    reading a chat message" and "the user is at a machine they own": whether
    to emit a delivery tag, whether a file has to land somewhere the gateway
    is allowed to send from, whether narration would read as chat noise.

    Resolves ``HERMES_PLATFORM``, then the session platform, then the session
    source, and reports messaging when any of them names a surface outside
    :data:`NON_MESSAGING_SESSION_SURFACES`.
    """
    import os

    platform = os.getenv("HERMES_PLATFORM") or get_session_env("HERMES_SESSION_PLATFORM", "")
    source = get_session_env("HERMES_SESSION_SOURCE", "")
    for identity in (platform, source):
        identity = str(identity or "").strip().lower()
        if identity and identity not in NON_MESSAGING_SESSION_SURFACES:
            return True
    return False


def declare_stateless_channel() -> None:
    """Declare that this session cannot receive an async background completion.

    Binds only the delivery capability, leaving every other session var unset.
    Use this instead of ``set_session_vars(async_delivery=False)`` on a pure
    single-process runner: ``set_session_vars`` also latches
    ``_session_context_engaged`` (see above), which switches the subprocess
    env bridge from "os.environ fallback" to "ContextVar-authoritative, strip on
    _UNSET" in ``tools/environments/local.py``. A one-shot CLI that never engages
    the session-context system must not flip that latch as a side effect of
    declaring a capability.

    Callers that already build a full session context (cron's ``run_job``) get
    the same state by passing ``async_delivery=False`` to ``set_session_vars``.

    A session that cannot take a late completion makes ``delegate_task`` fall
    through to its existing inline/synchronous path, so subagent results are
    returned within the turn instead of being dispatched to a channel that will
    never deliver them.

    See NousResearch/hermes-agent#53027 and #63142.
    """
    _SESSION_ASYNC_DELIVERY.set(False)


def async_delivery_supported() -> bool:
    """Whether the current session can deliver a background completion later.

    Returns ``False`` for finite runtimes that can end before a detached result
    is delivered: sessions explicitly bound by a stateless channel — an adapter
    that cannot route a notification back after the turn ends (the API server),
    or a one-shot runner that exits after its final response (``hermes -z``,
    cron — see :func:`declare_stateless_channel`) — and dispatcher-spawned
    Kanban workers (identified by ``HERMES_KANBAN_TASK``), which are one-shot
    ``chat -q`` subprocesses. The real gateway platforms, the interactive CLI,
    and any other path that never bound the contextvar return ``True``.

    Tools that promise async delivery (``terminal`` notify_on_complete /
    watch_patterns, ``delegate_task`` background=True) consult this before
    registering a watcher / dispatching a detached child, so they can refuse a
    promise the channel can't keep instead of silently no-op'ing.
    """
    import os

    # A Kanban worker is a one-shot subprocess. Its parent session and process
    # disappear after the quiet turn returns, so a completion queued later has
    # no durable consumer even though an ordinary CLI session can drain that
    # queue. Force tools onto their existing synchronous/polling fallbacks.
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False

    value = _SESSION_ASYNC_DELIVERY.get()
    if value is _UNSET:
        return True
    return bool(value)
