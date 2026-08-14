"""Canonical conversation ownership — one durable authority, every surface.

Hermes runs the same agent core behind a CLI, a messaging gateway, an HTTP API,
a TUI, an ACP server, the desktop app and cron. Each of those had its own idea
of "busy": ``gateway/turn_lease.py`` serialises turns per resolved session id
*inside one process*, the adapter's ``_active_sessions`` guards a routing key,
``compression_locks`` serialises rotation. None of them can see a second
*process* on the same conversation, and ``turn_lease``'s own docstring says so:

    A CLI process sharing the session via CLI-continuity is outside any
    in-process lock — that pair needs a DB-level lease (separate design).

This module is that design. It is deliberately small: identity, a grant, typed
conflicts, and a cancellation-safe context manager. The durable mechanics live
on :class:`hermes_state.SessionDB` beside the store they fence.

Three properties are load-bearing:

**The unit is the conversation root, not the session id.** Context compression
rotates ``session_id`` to a fresh child segment mid-turn; delegate subagents
hang off their parent. ``SessionDB.get_conversation_root`` already resolves a
lineage to one stable id, and that is what ownership keys on — otherwise a
rotation would silently hand the same conversation to a second owner.

**A grant pins the root it captured.** The root is structurally mutable, so a
grant that recomputed it on every write could orphan itself mid-turn. It pins
instead, fenced writes validate the pinned ``(root, holder, fence_token)``, and
destructive re-rooting paths refuse while a live grant covers the lineage.

**It fails closed.** ``try_acquire_compression_lock`` swallows ``sqlite3.Error``
and returns False because skipping compression is safe. Skipping *ownership* is
not — both racers would proceed. Every failure here raises a typed error for
the calling surface to project; adapters without a dedicated mapping currently
use their generic error contract. No surface fabricates a transcript message
to report it (that would break role alternation and the prompt cache).
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import socket
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

try:  # psutil is a hard dependency in practice; tolerate scaffold imports.
    import psutil  # type: ignore
except Exception:  # pragma: no cover - import-environment only
    psutil = None  # type: ignore

logger = logging.getLogger(__name__)

# How long a grant stays valid without a refresh. The refresher ticks well
# inside this, so the TTL only matters when a holder dies without releasing and
# its pid cannot be proven gone (a different host, or a probe we don't trust).
DEFAULT_OWNERSHIP_TTL_SECONDS = 300.0

# Ceiling on the refresh interval. A long turn refreshes roughly every minute.
_MAX_REFRESH_INTERVAL_SECONDS = 60.0

_HOLDER_RE = re.compile(
    r"host=(?P<host>[^:]*):pid=(?P<pid>\d+):start=(?P<start>-?[0-9.]+)"
)

_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")


# ── typed conflicts ────────────────────────────────────────────────────────


class ConversationOwnershipError(Exception):
    """Base for every refusal this authority can issue."""


class ConversationOwnershipConflict(ConversationOwnershipError):
    """Another live holder owns this conversation.

    Raised at *admission* — before any transcript load or write — so a
    rejected turn leaves the conversation byte-identical. Surfaces may project
    this into a dedicated idiom or their existing generic error contract; none
    may answer by appending a message to the transcript.
    """

    def __init__(
        self,
        conversation_root: str,
        *,
        holder: str,
        surface: str = "",
        session_id: str = "",
        fence_token: int = 0,
        expires_at: float = 0.0,
    ) -> None:
        self.conversation_root = conversation_root
        self.holder = holder
        self.surface = surface
        self.session_id = session_id
        self.fence_token = fence_token
        self.expires_at = expires_at
        # Public adapters often stringify exceptions generically. Keep the
        # message bounded and non-diagnostic; structured fields remain
        # available for trusted logs and explicit internal projections.
        super().__init__("conversation is currently active in another session")


class StaleConversationOwnershipError(ConversationOwnershipError):
    """The grant used for a fenced write is no longer the current owner.

    This is the fence doing its job: a writer that stalled through a handover
    is refused *before* its mutation runs, so it cannot publish, replace or
    delete history that a newer owner has since written.
    """

    def __init__(
        self,
        conversation_root: str,
        *,
        expected_fence_token: int,
        actual_fence_token: Optional[int] = None,
        actual_holder: Optional[str] = None,
    ) -> None:
        self.conversation_root = conversation_root
        self.expected_fence_token = expected_fence_token
        self.actual_fence_token = actual_fence_token
        self.actual_holder = actual_holder
        super().__init__("conversation ownership changed; retry the operation")


class ConversationOwnershipUnavailable(ConversationOwnershipError):
    """The authority itself could not be consulted.

    Fail closed: an unreachable authority is refused, never assumed granted.
    In practice a store that cannot serve this write cannot serve the
    transcript write either, so the turn was already lost.
    """

    def __init__(self, conversation_root: str, cause: BaseException) -> None:
        self.conversation_root = conversation_root
        self.cause = cause
        super().__init__("conversation ownership authority is temporarily unavailable")


# ── identity ───────────────────────────────────────────────────────────────


def _hostname() -> str:
    try:
        return _SAFE_TOKEN_RE.sub("_", socket.gethostname()) or "unknown"
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def _process_start_time(pid: int) -> float:
    """Process create time, or 0.0 when it cannot be read.

    Pairing pid with create time is what makes pid reuse safe: a recycled pid
    on a rebooted box must not look like a live holder.
    """
    if psutil is None:
        return 0.0
    try:
        return float(psutil.Process(pid).create_time())
    except Exception:
        return 0.0


def new_holder_id(*, surface: str = "") -> str:
    """Build a globally unique, self-describing holder id.

    ``host``/``pid``/``start`` make liveness decidable on this machine;
    ``tid``/``nonce`` keep two co-resident acquirers distinct so a release can
    never free somebody else's grant.
    """
    pid = os.getpid()
    parts = [
        f"host={_hostname()}",
        f"pid={pid}",
        f"start={_process_start_time(pid):.3f}",
        f"tid={threading.get_ident()}",
        f"nonce={uuid.uuid4().hex[:12]}",
    ]
    if surface:
        parts.append(f"surface={_SAFE_TOKEN_RE.sub('_', surface)}")
    return ":".join(parts)


def holder_process_is_dead(holder: str) -> bool:
    """True only when this host can PROVE the holder's process is gone.

    Conservative in every direction that matters: a holder from another host,
    an unparseable holder, our own pid, a create-time we cannot read, or any
    probe error all return False and wait for normal TTL expiry. Stealing a
    live grant corrupts a conversation; waiting out a TTL only delays one.
    """
    match = _HOLDER_RE.search(holder or "")
    if match is None:
        return False
    if match.group("host") != _hostname():
        # A different machine's pid says nothing about liveness here.
        return False
    try:
        pid = int(match.group("pid"))
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    if psutil is None:
        return False
    try:
        if not psutil.pid_exists(pid):
            return True
    except Exception:
        return False
    try:
        recorded_start = float(match.group("start"))
    except (TypeError, ValueError):
        return False
    if recorded_start <= 0:
        # Holder predates create-time stamping: pid existence is all we have,
        # and it exists — assume live.
        return False
    current_start = _process_start_time(pid)
    if current_start <= 0:
        return False
    # A live pid whose create time moved is a RECYCLED pid: the holder is gone.
    return abs(current_start - recorded_start) >= 1.0


# ── the grant ──────────────────────────────────────────────────────────────


@dataclass
class OwnershipGrant:
    """Proof that this process owns ``conversation_root`` right now.

    ``conversation_root`` is pinned at acquire and never recomputed — see the
    module docstring. ``fence_token`` is what makes a stale writer detectable
    after handover.
    """

    conversation_root: str
    holder: str
    fence_token: int
    surface: str = ""
    session_id: str = ""
    acquired_at: float = 0.0
    expires_at: float = 0.0
    ttl_seconds: float = DEFAULT_OWNERSHIP_TTL_SECONDS
    # True when this grant re-uses an outer grant held by the same thread; the
    # inner scope must not release it.
    nested: bool = False
    released: bool = False


# ── process-local re-entrancy ──────────────────────────────────────────────
#
# One turn legitimately re-enters the authority: the core agent holds the
# conversation, then compression rotates it, then a rewrite publishes. Those
# are the SAME owner and must not deadlock against themselves. Re-entrancy is
# scoped to the acquiring THREAD, so a second thread in the same process (a
# background fork, a gateway worker) still goes to the durable authority and
# still collides.

_LOCAL_GRANTS: Dict[Tuple[int, str], List["OwnershipGrant"]] = {}
_LOCAL_GRANTS_LOCK = threading.Lock()


def _local_key(db: Any, conversation_root: str) -> Tuple[int, str]:
    return (threading.get_ident(), f"{_db_identity(db)}::{conversation_root}")


def _db_identity(db: Any) -> str:
    path = getattr(db, "db_path", None)
    if path:
        return str(path)
    return f"obj:{id(db):x}"


def current_grant(db: Any, conversation_root: str) -> Optional[OwnershipGrant]:
    """The grant this thread already holds for ``conversation_root``, if any."""
    with _LOCAL_GRANTS_LOCK:
        stack = _LOCAL_GRANTS.get(_local_key(db, conversation_root))
        return stack[-1] if stack else None


def current_grant_for_session(db: Any, session_id: str) -> Optional[OwnershipGrant]:
    """Return this thread's grant covering ``session_id``, if one exists."""
    root = resolve_conversation_root(db, session_id)
    return current_grant(db, root) if root else None


def _push_local(db: Any, grant: OwnershipGrant) -> None:
    with _LOCAL_GRANTS_LOCK:
        _LOCAL_GRANTS.setdefault(_local_key(db, grant.conversation_root), []).append(
            grant
        )


def _pop_local(db: Any, grant: OwnershipGrant) -> None:
    key = _local_key(db, grant.conversation_root)
    with _LOCAL_GRANTS_LOCK:
        stack = _LOCAL_GRANTS.get(key)
        if not stack:
            return
        # Remove by identity so an out-of-order unwind cannot pop a sibling.
        for index in range(len(stack) - 1, -1, -1):
            if stack[index] is grant:
                del stack[index]
                break
        if not stack:
            _LOCAL_GRANTS.pop(key, None)


# ── lease refresher ────────────────────────────────────────────────────────


class OwnershipLeaseRefresher:
    """Keeps a grant alive across a long turn.

    Mirrors the compression lock's refresher: tolerate transient failures for
    at most one lease's worth of time, so a blip recovers on the next tick
    while the give-up window stays bounded by the TTL the acquirer set.
    """

    def __init__(
        self,
        db: Any,
        grant: OwnershipGrant,
        *,
        refresh_interval_seconds: Optional[float] = None,
    ) -> None:
        self._db = db
        self._grant = grant
        ttl = float(grant.ttl_seconds or DEFAULT_OWNERSHIP_TTL_SECONDS)
        if refresh_interval_seconds is None:
            refresh_interval_seconds = max(
                1.0, min(_MAX_REFRESH_INTERVAL_SECONDS, ttl / 3.0)
            )
        self._interval = max(0.05, float(refresh_interval_seconds))
        self._max_consecutive_failures = max(1, int(ttl / self._interval))
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="conversation-ownership-refresh",
            daemon=True,
        )

    def start(self) -> "OwnershipLeaseRefresher":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            # A late refresh on an already-released grant matches rowcount 0.
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        consecutive_failures = 0
        while not self._stop.wait(self._interval):
            try:
                ok = bool(self._db.refresh_conversation_ownership(self._grant))
            except Exception as exc:  # authority blip — bounded tolerance
                ok = False
                logger.debug(
                    "conversation ownership refresh raised for %s: %s",
                    self._grant.conversation_root,
                    exc,
                )
            if ok:
                consecutive_failures = 0
                continue
            consecutive_failures += 1
            if consecutive_failures >= self._max_consecutive_failures:
                logger.warning(
                    "conversation ownership refresh gave up for %s after %d "
                    "consecutive failures — the grant will expire on its TTL",
                    self._grant.conversation_root,
                    consecutive_failures,
                )
                return


# ── the admission point every surface calls ────────────────────────────────


def resolve_conversation_root(db: Any, session_id: str) -> str:
    """Canonical conversation identity for ``session_id``.

    A session with no row yet is its own root, which is exactly what
    ``get_conversation_root`` returns for it. An exception is different: the
    identity authority could not be consulted, so admission must fail closed.
    """
    if not session_id:
        return ""
    try:
        root = db.get_conversation_root(session_id)
    except Exception as exc:
        raise ConversationOwnershipUnavailable(str(session_id), exc) from exc
    return str(root or session_id)


def is_durable_store(db: Any) -> bool:
    """True only for a real :class:`hermes_state.SessionDB`.

    A stub, mock or duck-typed store has no ``conversation_ownership`` table to
    fence, so calling into it would both mislead the caller (a "grant" that
    guarantees nothing) and perturb the mock-call assertions those stubs exist
    to make. Imported lazily because ``hermes_state`` imports this module.
    """
    if db is None:
        return False
    try:
        from hermes_state import SessionDB
    except Exception:  # pragma: no cover - import-environment only
        return False
    return isinstance(db, SessionDB)


def should_own_conversation(agent: Any) -> bool:
    """Does this agent contend for its conversation?

    Three populations legitimately run *inside* a conversation somebody else
    owns, and must never take the grant:

    * **Persist-disabled forks** (background review) share the live session id
      for prompt-cache warmth but are hard-stopped from every transcript write.
      Contending would starve the real turn for nothing.
    * **Delegate subagents.** A subagent's conversation root resolves through
      ``_parent_session_id`` to the parent's root — by design, so a whole
      delegation tree tags as one conversation. They run concurrently with the
      parent turn that launched them; acquiring would deadlock delegation
      against itself.
    * **Store-less agents** (evals, scaffolds) and agents wired to a stub store
      have no durable transcript to protect.
    """
    if agent is None:
        return False
    if getattr(agent, "_persist_disabled", False):
        return False
    if not str(getattr(agent, "session_id", "") or ""):
        return False
    if not is_durable_store(getattr(agent, "_session_db", None)):
        return False
    if str(getattr(agent, "platform", "") or "") == "subagent":
        return False
    if str(getattr(agent, "_parent_session_id", "") or ""):
        return False
    return True


def ownership_admission_surface(agent: Any) -> str:
    """The surface name recorded on the grant and echoed in conflicts.

    The rejected surface is usually not the one squatting on the conversation,
    so this has to be something a human recognises ("telegram", "cli", "acp")
    rather than a pid.
    """
    return str(getattr(agent, "platform", "") or "") or "agent"


@contextlib.contextmanager
def own_conversation(
    db: Any,
    session_id: str,
    *,
    surface: str,
    ttl_seconds: float = DEFAULT_OWNERSHIP_TTL_SECONDS,
    enabled: bool = True,
    refresh_interval_seconds: Optional[float] = None,
):
    """Hold the conversation for the body, release on EVERY exit path.

    Yields the :class:`OwnershipGrant`, or ``None`` when ownership does not
    apply (no store, no session id, or an explicitly opted-out caller such as a
    persist-disabled background fork, which can never write the transcript and
    must not contend for it).

    Cancellation safety is the ``finally``: ``KeyboardInterrupt``,
    ``asyncio.CancelledError``, a generator close and an ordinary exception all
    unwind through it. Release is holder- and fence-scoped, so a late unwind
    can never free a newer owner's grant.
    """
    if not enabled or db is None or not session_id:
        yield None
        return

    root = resolve_conversation_root(db, session_id)
    if not root:
        yield None
        return

    existing = current_grant(db, root)
    if existing is not None:
        nested = OwnershipGrant(
            conversation_root=existing.conversation_root,
            holder=existing.holder,
            fence_token=existing.fence_token,
            surface=existing.surface,
            session_id=str(session_id),
            acquired_at=existing.acquired_at,
            expires_at=existing.expires_at,
            ttl_seconds=existing.ttl_seconds,
            nested=True,
        )
        yield nested
        return

    grant = db.try_acquire_conversation_ownership(
        root,
        new_holder_id(surface=surface),
        ttl_seconds=ttl_seconds,
        surface=surface,
        session_id=str(session_id),
    )
    _push_local(db, grant)
    refresher = OwnershipLeaseRefresher(
        db, grant, refresh_interval_seconds=refresh_interval_seconds
    ).start()
    try:
        yield grant
    finally:
        refresher.stop()
        _pop_local(db, grant)
        try:
            db.release_conversation_ownership(grant)
        except Exception as exc:  # release is best-effort; TTL is the backstop
            logger.warning(
                "conversation ownership release failed for %s: %s",
                grant.conversation_root,
                exc,
            )


def describe_conflict(exc: ConversationOwnershipConflict) -> str:
    """One user-facing sentence, shared by every surface's projection.

    Naming the holding surface is the difference between an actionable message
    and "try again later" — the surface that gets rejected is usually not the
    one squatting on the conversation.
    """
    surface = exc.surface or "another session"
    return (
        f"This conversation is currently owned by {surface}. "
        "Finish or stop that session before continuing here."
    )
