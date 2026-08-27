"""Registry authority and deferred-effect fencing for gateway sessions.

Extracted from ``tui_gateway/server.py`` so the session-ownership rules live in
one focused module instead of growing the decomposition-owned godfile
(#78630).  ``server.py`` re-exports every name below under its original
underscore-prefixed spelling, which keeps the handler modules' rebound-globals
seam (see ``method_ctx.py``) and the existing monkeypatch targets working
unchanged.

Two rules live here.

**1. Who owns a registry id.**  A session record is authoritative only while
``server._sessions[sid]`` still *is* that exact object and it is neither
``_closing`` nor ``_finalized``.  ``_pop_session_by_id()`` sets ``_closing``
under the registry lock before the record leaves the dict, so both halves of
that predicate matter.

Why a typed bind outcome instead of a bool: a rebind can fail for two reasons
that are NOT interchangeable for the caller.

* ``TRANSPORT_DEAD`` — the record is still the authoritative one, but the
  request socket died.  Work already admitted for this session may finish; the
  response simply cannot be delivered on that socket.
* ``STALE_RECORD`` — the record lost registration authority.  Nothing new may
  be queued, mutated, or started against it.

Collapsing both into ``False`` is what let ``prompt.submit`` and the queued
drain keep mutating a popped record after the ownership gate had already
proved it was no longer authoritative.

**2. When a deferred worker may publish its result.**  ``session.resume``
hydrates history and builds an agent on a worker thread, long after the RPC
answered.  Every such side effect goes through ``run_live_build_effect()`` /
the ``commit_*`` helpers, which take the per-runtime lifecycle lock so a
teardown cannot finalize halfway through, and re-prove authority before they
write anything into the record.
"""

from __future__ import annotations

import contextvars
import enum
import threading
import time
from collections import OrderedDict
from typing import Callable

# ---------------------------------------------------------------------------
# Retired registry ids.
# ---------------------------------------------------------------------------
# A bounded LRU of ids that were popped from the registry.  `write_json` uses
# it to drop events addressed to a runtime that no longer exists instead of
# broadcasting them to whichever transport is current.
_RETIRED_SESSION_ID_LIMIT = 4096
_retired_session_ids: OrderedDict[str, None] = OrderedDict()
_retired_session_ids_lock = threading.Lock()

# The session a deferred build effect is currently running for.  `write_json`
# reads it to keep a late worker's events from leaking onto a DIFFERENT
# session that happens to hold the same id now.
deferred_build_effect_session: contextvars.ContextVar[dict | None] = (
    contextvars.ContextVar("hermes_gateway_deferred_build_effect_session", default=None)
)


def remember_retired_session_id(sid: str) -> None:
    sid = str(sid or "")
    if not sid:
        return
    with _retired_session_ids_lock:
        _retired_session_ids.pop(sid, None)
        _retired_session_ids[sid] = None
        while len(_retired_session_ids) > _RETIRED_SESSION_ID_LIMIT:
            _retired_session_ids.popitem(last=False)


def forget_retired_session_id(sid: str) -> None:
    sid = str(sid or "")
    if not sid:
        return
    with _retired_session_ids_lock:
        _retired_session_ids.pop(sid, None)


def was_retired_session_id(sid: str) -> bool:
    sid = str(sid or "")
    if not sid:
        return False
    with _retired_session_ids_lock:
        return sid in _retired_session_ids


# ---------------------------------------------------------------------------
# Registry authority.
# ---------------------------------------------------------------------------


class SessionBindOutcome(enum.Enum):
    """Result of claiming a live client transport for a session record."""

    BOUND = "bound"
    TRANSPORT_DEAD = "transport_dead"
    STALE_RECORD = "stale_record"

    @property
    def is_bound(self) -> bool:
        return self is SessionBindOutcome.BOUND

    @property
    def is_stale_record(self) -> bool:
        """True when the record may no longer own any mutation.

        Callers that are about to touch history, the prompt queue, or the
        ``running`` flag must treat this as terminal.
        """
        return self is SessionBindOutcome.STALE_RECORD


def _server():
    # Late import: server.py imports this module at load time.  Resolving
    # through the module object (rather than binding the globals here) also
    # keeps the existing monkeypatch seams in the gateway tests working.
    from tui_gateway import server

    return server


def _record_is_authoritative_locked(server, sid: str, session: dict) -> bool:
    """Registry-authority predicate; caller holds ``_sessions_lock``."""
    return (
        server._sessions.get(sid) is session
        and not session.get("_closing")
        and not session.get("_finalized")
    )


def session_record_is_authoritative(sid: str, session: dict) -> bool:
    """Whether ``session`` still owns ``sid`` and may be mutated.

    Takes the same ``_session_resume_lock -> _sessions_lock`` order as the
    disconnect claim, so a caller that checks this cannot interleave with a
    teardown that is mid-claim.
    """
    server = _server()
    with server._session_resume_lock:
        with server._sessions_lock:
            return _record_is_authoritative_locked(server, sid, session)


# Historical spelling kept for the deferred-build call sites.
session_is_live_for_commit = session_record_is_authoritative


def bind_live_session_transport(
    sid: str, session: dict, transport
) -> SessionBindOutcome:
    """Bind ``session`` to a live client transport as one ownership claim.

    Disconnect teardown snapshots session ids before it can act on them, so a
    later transport assignment must use the same resume lock and confirm that
    the exact session record is still registered.  The liveness check also
    handles a slow ``session.resume`` whose WebSocket closed while it was
    building history or an agent: such a worker must not publish a dead
    transport after the disconnect cleanup has already completed.

    Registry authority is checked BEFORE transport liveness so the two failure
    reasons stay distinguishable — a caller needs to know that its record was
    popped even when the socket it wanted to bind is also gone.
    """
    server = _server()
    with server._session_resume_lock:
        with server._sessions_lock:
            if not _record_is_authoritative_locked(server, sid, session):
                return SessionBindOutcome.STALE_RECORD
            # The transport can close while this worker waits for the locks.
            if transport is None or server._transport_is_dead(transport):
                return SessionBindOutcome.TRANSPORT_DEAD
            session["transport"] = transport
            session.setdefault("viewers", {})[transport] = time.time()
            # Keep timer cancellation in the same critical section as the
            # rebind.  Otherwise a disconnect could park and schedule a reap
            # between the bind and a later cancellation.
            server._cancel_ws_orphan_reap(sid)
    return SessionBindOutcome.BOUND


# ---------------------------------------------------------------------------
# Deferred-build fencing.
# ---------------------------------------------------------------------------


def session_lifecycle_lock(session: dict) -> threading.RLock:
    """Return the per-runtime lock shared by builders and teardown."""
    return session.setdefault("_lifecycle_lock", threading.RLock())


def run_live_build_effect(
    sid: str, session: dict, effect: Callable[[], object]
) -> bool:
    """Run a deferred-build side effect before teardown can finalize."""
    with session_lifecycle_lock(session):
        if not session_record_is_authoritative(sid, session):
            return False
        token = deferred_build_effect_session.set(session)
        try:
            result = effect()
        finally:
            deferred_build_effect_session.reset(token)
    return True if result is None else bool(result)


def commit_agent_build(sid: str, session: dict, agent, config_model_seen) -> bool:
    """Publish a built agent only while its runtime still owns the registry id."""
    server = _server()
    with session_lifecycle_lock(session):
        with server._session_resume_lock:
            with server._sessions_lock:
                if not _record_is_authoritative_locked(server, sid, session):
                    return False
                session["agent"] = agent
                session["config_model_seen"] = config_model_seen
    return True


def commit_resume_hydration(
    sid: str,
    session: dict,
    history: list,
    display_history_prefix: list,
    message_count: int,
) -> bool:
    """Publish deferred resume history only for the current live runtime."""
    server = _server()
    with session_lifecycle_lock(session):
        with server._session_resume_lock:
            with server._sessions_lock:
                if not _record_is_authoritative_locked(server, sid, session):
                    return False
                with session["history_lock"]:
                    session["history"] = history
                    session["display_history_prefix"] = display_history_prefix
                    session["resume_hydrating"] = False
                    session["resume_message_count"] = message_count
    return True


def commit_resume_failure_state(sid: str, session: dict, message: str) -> bool:
    """Publish deferred-resume failure state before the runtime is retired."""
    server = _server()
    with session_lifecycle_lock(session):
        with server._session_resume_lock:
            with server._sessions_lock:
                if not _record_is_authoritative_locked(server, sid, session):
                    return False
                session["resume_hydrating"] = False
                session["resume_history_error"] = message
                session["agent_error"] = message
                session["resume_history_ready"].set()
                session["agent_ready"].set()
    return True
