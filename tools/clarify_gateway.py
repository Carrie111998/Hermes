"""Gateway-side clarify primitive (blocking event-based queue).

The ``clarify`` tool needs to ask the user a question and block the agent
thread until they respond.  In CLI mode this is trivial — ``input()`` is
synchronous.  In gateway mode the agent runs on a worker thread while the
event loop handles the user's reply, so we need a thread-safe primitive
that:

  * stores a pending clarify request (with a generated ``clarify_id``),
  * blocks the agent thread on an ``Event``,
  * resolves the wait when the gateway's button-callback or text-intercept
    fires ``resolve_gateway_clarify(clarify_id, response)``,
  * supports timeouts so a user who never responds does NOT hang the agent
    thread forever (which would also pin the gateway's running-agent guard).

State is module-level (same shape as ``tools.approval``) so platform
adapters can call ``resolve_gateway_clarify`` without holding a back-
reference to the ``GatewayRunner`` instance.

Two delivery paths from the adapter:

  1. **Button UI** — adapters override ``send_clarify`` to render inline
     buttons (e.g. Telegram ``InlineKeyboardMarkup``).  The button
     callback resolves with the chosen string.  A final "Other (type
     answer)" button enters text-capture mode for free-form responses.

  2. **Text fallback** — adapters without rich UI render a numbered list.
     The user replies with a number ("2") or with free text; the gateway's
     ``_handle_message`` intercepts the reply and resolves directly.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_STATE_PENDING = "pending"
_STATE_ANSWERED = "answered"
_STATE_CANCELLED = "cancelled"
_STATE_TIMED_OUT = "timed_out"
_STATE_DELIVERY_FAILED = "delivery_failed"


# =========================================================================
# Module-level state
# =========================================================================

@dataclass
class _ClarifyEntry:
    """One pending clarify request inside a gateway session."""
    clarify_id: str
    session_key: str
    question: str
    choices: Optional[List[str]]
    multi_select: bool = False
    generation: Optional[int] = None
    responder_id: Optional[str] = None
    identity_v1: bool = False
    event: threading.Event = field(default_factory=threading.Event)
    response: Optional[str] = None
    awaiting_text: bool = False  # set when user picked "Other" or clarify is open-ended
    state: str = _STATE_PENDING

    def signature(self) -> Dict[str, object]:
        return {
            "clarify_id": self.clarify_id,
            "session_key": self.session_key,
            "question": self.question,
            "choices": list(self.choices) if self.choices else None,
            "multi_select": bool(self.multi_select),
        }


@dataclass(frozen=True)
class ClarifyTerminalResult:
    """One exact terminal outcome consumed by a non-waiting caller."""

    state: str
    response: Optional[str]
    transitioned: bool

    @property
    def answered(self) -> bool:
        return self.state == _STATE_ANSWERED


_lock = threading.RLock()
# clarify_id → _ClarifyEntry  (primary lookup for button callbacks)
_entries: Dict[str, _ClarifyEntry] = {}
# session_key → list[clarify_id]  (FIFO; for text-fallback intercept and session cleanup)
_session_index: Dict[str, List[str]] = {}
# session_key → authoritative gateway run generation.  This lives under the
# same lock as the entries so a session boundary and a late callback cannot
# cross between two separately-protected states.
_current_generations: Dict[str, int] = {}
# Process-wide authority sequence.  One scalar keeps generation allocation
# bounded even when an API listener sees an unbounded number of distinct
# conversation scopes.  Session retirement removes the per-scope authority;
# a later incarnation still receives a token that no stale closure can reuse.
_generation_sequence = 0


# =========================================================================
# Public API — agent-thread side
# =========================================================================

def register(
    clarify_id: str,
    session_key: str,
    question: str,
    choices: Optional[List[str]],
    multi_select: bool = False,
    *,
    generation: Optional[int] = None,
    responder_id: Optional[str] = None,
    identity_v1: bool = False,
) -> _ClarifyEntry:
    """Register a pending clarify request and return the entry.

    The caller (gateway clarify_callback) will then send the prompt to the
    user and block on ``wait_for_response(clarify_id, timeout)``.
    """
    normalized_session = str(session_key or "")
    normalized_generation = int(generation) if generation is not None else None
    if identity_v1 and (not normalized_session or normalized_generation is None):
        raise ValueError("clarify identity v1 requires session_key and generation")

    entry = _ClarifyEntry(
        clarify_id=clarify_id,
        session_key=normalized_session,
        question=question,
        choices=list(choices) if choices else None,
        multi_select=bool(multi_select) and bool(choices),
        generation=normalized_generation,
        responder_id=str(responder_id) if responder_id is not None else None,
        identity_v1=bool(identity_v1),
        # Open-ended (no choices) → next message IS the response, no buttons needed.
        awaiting_text=not bool(choices),
    )
    with _lock:
        if clarify_id in _entries:
            raise ValueError(f"clarify_id is already registered: {clarify_id}")
        if entry.identity_v1:
            current = _current_generations.get(entry.session_key)
            if current != entry.generation:
                raise ValueError(
                    "stale clarify generation (unpublished or superseded): "
                    f"session={entry.session_key!r} current={current} "
                    f"requested={entry.generation}"
                )
        _entries[clarify_id] = entry
        _session_index.setdefault(entry.session_key, []).append(clarify_id)
    return entry


def _update_session_generation_locked(
    normalized_session: str,
    normalized_generation: int,
) -> int:
    """Publish a generation while ``_lock`` is held."""

    current = _current_generations.get(normalized_session)
    live_identity_entries = [
        entry
        for clarify_id in list(_session_index.get(normalized_session, []))
        if (entry := _entries.get(clarify_id)) is not None
        and entry.identity_v1
        and entry.state == _STATE_PENDING
    ]
    # Generations are monotonic authority.  Never roll back merely because
    # the previous prompt has already completed: doing so would let an old
    # publisher revive its stale buttons after the newer entry disappeared.
    if current is not None and normalized_generation < current:
        raise ValueError("clarify generation cannot move backwards")

    _current_generations[normalized_session] = normalized_generation
    cancelled = 0
    for entry in live_identity_entries:
        if entry.generation == normalized_generation:
            continue
        if _transition_locked(
            entry,
            _STATE_CANCELLED,
            "",
            remove=True,
        ):
            cancelled += 1
    return cancelled


def update_session_generation(session_key: str, generation: int) -> int:
    """Publish one session's authoritative run generation atomically.

    Any identity-v1 prompt from an older generation is cancelled and removed
    while holding the same lock.  A worker from that older run therefore
    cannot register a new prompt after the boundary, and its old buttons can
    no longer resolve merely because their stored entry also has an old token.

    Returns the number of pending prompts cancelled by the transition.
    """
    normalized_session = str(session_key or "")
    if not normalized_session:
        raise ValueError("session_key is required")
    normalized_generation = int(generation)

    global _generation_sequence
    with _lock:
        _generation_sequence = max(
            _generation_sequence,
            normalized_generation,
        )
        return _update_session_generation_locked(
            normalized_session,
            normalized_generation,
        )


def claim_session_generation(session_key: str) -> int:
    """Allocate and publish a globally unique generation for one session.

    The allocator is a single scalar, so high-cardinality API session churn
    does not create a second per-session retention map.  Publication and old
    prompt cancellation share the clarify registry lock.
    """

    normalized_session = str(session_key or "")
    if not normalized_session:
        raise ValueError("session_key is required")

    global _generation_sequence
    with _lock:
        _generation_sequence += 1
        generation = _generation_sequence
        _update_session_generation_locked(normalized_session, generation)
        return generation


def retire_session_generation(
    session_key: str,
    expected_generation: int,
) -> bool:
    """Retire one exact session authority after its worker is quiescent.

    Retirement never cancels entries and never affects a newer authority.  A
    caller must first end/cancel the exact generation's pending work, then call
    this method at a structurally proven worker lifecycle boundary.
    """

    normalized_session = str(session_key or "")
    if not normalized_session:
        raise ValueError("session_key is required")
    normalized_generation = int(expected_generation)

    with _lock:
        if _current_generations.get(normalized_session) != normalized_generation:
            return False
        for clarify_id in _session_index.get(normalized_session, []):
            entry = _entries.get(clarify_id)
            if (
                entry is not None
                and entry.identity_v1
                and entry.generation == normalized_generation
            ):
                return False
        _current_generations.pop(normalized_session, None)
        return True


def session_generation_retained(
    session_key: str,
    generation: int,
) -> bool:
    """Return whether one exact generation still occupies core state.

    This is a retry decision primitive for lifecycle owners.  It deliberately
    reports both the authoritative session slot and any exact identity entry,
    under the same lock, without exposing prompt content or mutable entries.
    """

    normalized_session = str(session_key or "")
    if not normalized_session:
        raise ValueError("session_key is required")
    normalized_generation = int(generation)

    with _lock:
        if _current_generations.get(normalized_session) == normalized_generation:
            return True
        return any(
            (entry := _entries.get(clarify_id)) is not None
            and entry.identity_v1
            and entry.generation == normalized_generation
            for clarify_id in _session_index.get(normalized_session, [])
        )


def _remove_from_indices_locked(entry: _ClarifyEntry) -> None:
    """Remove ``entry`` from both indices while ``_lock`` is held."""
    if _entries.get(entry.clarify_id) is entry:
        _entries.pop(entry.clarify_id, None)
    ids = _session_index.get(entry.session_key)
    if ids and entry.clarify_id in ids:
        ids.remove(entry.clarify_id)
        if not ids:
            _session_index.pop(entry.session_key, None)


def _identity_matches_locked(
    entry: _ClarifyEntry,
    *,
    session_key: Optional[str],
    generation: Optional[int],
    responder_id: Optional[str],
    require_responder: bool = True,
) -> bool:
    """Validate only exact request identity fields; never inspect prompt text."""
    if entry.identity_v1:
        if session_key is None or str(session_key) != entry.session_key:
            return False
        if generation is None:
            return False
        try:
            normalized_generation = int(generation)
        except (TypeError, ValueError):
            return False
        if normalized_generation != entry.generation:
            return False
        if _current_generations.get(entry.session_key) != entry.generation:
            return False
    else:
        if session_key is not None and str(session_key) != entry.session_key:
            return False
        if entry.generation is not None and generation != entry.generation:
            return False
    if (
        require_responder
        and entry.responder_id is not None
        and (responder_id is None or str(responder_id) != entry.responder_id)
    ):
        return False
    return True


def _transition_locked(
    entry: _ClarifyEntry,
    state: str,
    response: Optional[str],
    *,
    remove: bool = False,
) -> bool:
    """Apply the first terminal transition for ``entry`` atomically."""
    if entry.state != _STATE_PENDING:
        return False
    entry.state = state
    entry.response = response
    if remove:
        _remove_from_indices_locked(entry)
    entry.event.set()
    return True


def wait_for_response(
    clarify_id: str,
    timeout: float,
    *,
    session_key: Optional[str] = None,
    generation: Optional[int] = None,
) -> Optional[str]:
    """Block on the entry's event until resolved or timeout fires.

    Polls in 1-second slices so the agent's inactivity heartbeat keeps
    firing — without this, ``Event.wait(timeout=600)`` blocks the thread
    for 10 minutes with zero activity touches and the gateway's inactivity
    watchdog kills the agent while the user is still typing.

    ``timeout <= 0`` means an unlimited wait (never auto-skip mid-think); the
    heartbeat still fires each slice so inactivity watchdogs don't kill a live
    prompt.

    Returns the resolved response string, or ``None`` on timeout.
    """
    with _lock:
        entry = _entries.get(clarify_id)
        if (
            entry is not None
            and entry.identity_v1
            and not _identity_matches_locked(
                entry,
                session_key=session_key,
                generation=generation,
                responder_id=None,
                require_responder=False,
            )
        ):
            entry = None
    if entry is None:
        return None

    try:
        from tools.environments.base import touch_activity_if_due
    except Exception:  # pragma: no cover - optional
        touch_activity_if_due = None

    # 0 / negative → unlimited: no deadline, poll forever in 1s slices.
    unlimited = timeout is None or float(timeout) <= 0.0
    deadline = None if unlimited else time.monotonic() + float(timeout)
    activity_state = {"last_touch": time.monotonic(), "start": time.monotonic()}
    while True:
        if deadline is None:
            slice_s = 1.0
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            slice_s = min(1.0, remaining)
        if entry.event.wait(timeout=slice_s):
            break
        if touch_activity_if_due is not None:
            touch_activity_if_due(activity_state, "waiting for user clarify response")

    with _lock:
        if entry.state == _STATE_PENDING:
            _transition_locked(entry, _STATE_TIMED_OUT, None)
        _remove_from_indices_locked(entry)
        if entry.state == _STATE_ANSWERED:
            return entry.response
        if entry.state in {_STATE_CANCELLED, _STATE_DELIVERY_FAILED}:
            return ""
        return None


# =========================================================================
# Public API — gateway / adapter side
# =========================================================================

def resolve_gateway_clarify(
    clarify_id: str,
    response: str,
    *,
    session_key: Optional[str] = None,
    generation: Optional[int] = None,
    responder_id: Optional[str] = None,
) -> bool:
    """Unblock the agent thread waiting on ``clarify_id``.

    Returns True if an entry was found and resolved, False otherwise
    (already resolved, expired, or never existed).
    """
    with _lock:
        entry = _entries.get(clarify_id)
        if entry is None or not _identity_matches_locked(
            entry,
            session_key=session_key,
            generation=generation,
            responder_id=responder_id,
        ):
            return False
        return _transition_locked(
            entry,
            _STATE_ANSWERED,
            str(response) if response is not None else "",
        )


def cancel_request(
    clarify_id: str,
    *,
    session_key: Optional[str] = None,
    generation: Optional[int] = None,
    responder_id: Optional[str] = None,
    delivery_failed: bool = False,
) -> bool:
    """Cancel one exact pending clarify request and wake its waiter.

    The first terminal transition wins.  A second cancellation, a late
    response, or an identity mismatch returns ``False``.  The entry is
    removed immediately so cancellation cannot affect a sibling prompt.
    """
    with _lock:
        entry = _entries.get(clarify_id)
        if entry is None or not _identity_matches_locked(
            entry,
            session_key=session_key,
            generation=generation,
            responder_id=responder_id,
        ):
            return False
        state = _STATE_DELIVERY_FAILED if delivery_failed else _STATE_CANCELLED
        return _transition_locked(entry, state, "", remove=True)


def complete_failed_delivery(
    clarify_id: str,
    *,
    session_key: Optional[str] = None,
    generation: Optional[int] = None,
    responder_id: Optional[str] = None,
) -> Optional[ClarifyTerminalResult]:
    """Atomically fail delivery or consume the terminal result that won.

    Prompt delivery and an interactive response can cross in flight: an
    adapter may resolve the request just before its send future reports a
    timeout or unsuccessful result.  This operation closes that exact race
    under the state lock.  If the request is still pending, delivery failure
    becomes its terminal state.  If another terminal transition already won,
    that result is returned unchanged.  Either way the exact entry is removed
    from both indices, because the caller will not enter ``wait_for_response``.
    """
    with _lock:
        entry = _entries.get(clarify_id)
        if entry is None or not _identity_matches_locked(
            entry,
            session_key=session_key,
            generation=generation,
            responder_id=responder_id,
        ):
            return None
        transitioned = _transition_locked(
            entry,
            _STATE_DELIVERY_FAILED,
            "",
        )
        result = ClarifyTerminalResult(
            state=entry.state,
            response=entry.response,
            transitioned=transitioned,
        )
        _remove_from_indices_locked(entry)
        return result


def get_pending_for_session(
    session_key: str,
    *,
    include_choice_prompts: bool = False,
    generation: Optional[int] = None,
    responder_id: Optional[str] = None,
) -> Optional[_ClarifyEntry]:
    """Return the oldest pending clarify entry for a session, or None.

    By default this only returns entries awaiting free-form text (open-ended
    clarifies, or a multi-choice clarify after the user picked ``Other``).
    Gateways may pass ``include_choice_prompts=True`` when the user has typed
    directly in response to an active multi-choice prompt; in that case the
    oldest unresolved clarify is returned so the text can resolve it instead
    of being queued as an unrelated follow-up turn.
    """
    with _lock:
        ids = _session_index.get(session_key) or []
        for cid in ids:
            entry = _entries.get(cid)
            if entry is None or entry.state != _STATE_PENDING:
                continue
            if not _identity_matches_locked(
                entry,
                session_key=session_key,
                generation=generation,
                responder_id=responder_id,
            ):
                continue
            if include_choice_prompts or entry.awaiting_text:
                return entry
        return None


def _coerce_text_response(entry: _ClarifyEntry, response: str) -> Optional[str]:
    """Map typed choice replies to canonical choice text, otherwise keep or reject custom text.

    For native interactive multi-choice clarifies (button UI, awaiting_text=False):
      - Accept numeric selections ("2" → choice[1])
      - Accept exact choice label matches (case-insensitive)
      - Reject arbitrary prose (return None) so the message continues as a normal turn

    For multi-select clarifies (entry.multi_select=True):
      - Accept several numbers separated by commas and/or spaces ("1,3" / "1 3")
      - Accept exact choice label matches (single or comma-separated)
      - Out-of-range numbers reject the whole reply (return None) so the user
        can retry instead of silently getting a partial selection
      - Selections are returned as a JSON array string, which the clarify
        tool's ``_parse_multi_select_response`` decodes back into a list

    For text fallback or awaiting_text mode:
      - Accept any text (numeric/label/custom) after passing through coercion

    For open-ended clarifies (no choices):
      - Accept any text

    Returns None when the response should be rejected (arbitrary prose for native multi-choice).
    """
    text = str(response).strip()

    if not entry.choices:
        # Open-ended: accept any text
        return text

    if entry.multi_select:
        coerced = _coerce_multi_select_text(entry, text)
        if coerced is not None:
            return coerced
        # Not a parseable selection — accept as custom text only in
        # awaiting_text mode (the "Other" path); otherwise reject.
        return text if entry.awaiting_text else None

    # Try numeric selection first (always valid for multi-choice)
    try:
        idx = int(text) - 1
    except ValueError:
        idx = -1

    if 0 <= idx < len(entry.choices):
        return entry.choices[idx]

    # Try exact choice label match (always valid for multi-choice)
    for choice in entry.choices:
        if text.casefold() == str(choice).strip().casefold():
            return str(choice).strip()

    # For text fallback or awaiting_text mode, accept custom text
    # For native interactive multi-choice mode, reject arbitrary prose
    if entry.awaiting_text:
        return text

    return None


def _coerce_multi_select_text(entry: _ClarifyEntry, text: str) -> Optional[str]:
    """Parse a typed multi-select reply into a JSON array of choice labels.

    Accepts numbers and/or exact labels separated by commas (and, for
    all-numeric replies, bare spaces): "1,3", "1 3", "staging, prod".
    Returns ``None`` when any token is out of range or unrecognised so the
    caller can reject the reply cleanly instead of resolving a partial or
    wrong selection.
    """
    import json as _json

    if not text:
        return None
    choices = entry.choices or []

    # Split on commas first; if no commas and every whitespace-separated
    # token is numeric, treat spaces as separators too ("1 3").
    if "," in text:
        tokens = [t.strip() for t in text.split(",") if t.strip()]
    else:
        parts = text.split()
        if len(parts) > 1 and all(p.strip().isdigit() for p in parts):
            tokens = [p.strip() for p in parts]
        else:
            tokens = [text]

    selected: List[str] = []
    for token in tokens:
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(choices):
                label = str(choices[idx]).strip()
                if label not in selected:
                    selected.append(label)
                continue
            return None  # out-of-range number → reject whole reply
        # Exact label match (case-insensitive)
        matched = None
        for choice in choices:
            if token.casefold() == str(choice).strip().casefold():
                matched = str(choice).strip()
                break
        if matched is None:
            return None
        if matched not in selected:
            selected.append(matched)

    if not selected:
        return None
    return _json.dumps(selected, ensure_ascii=False)


def resolve_text_response_for_session(
    session_key: str,
    response: str,
    *,
    generation: Optional[int] = None,
    responder_id: Optional[str] = None,
) -> bool:
    """Resolve the oldest pending clarify in ``session_key`` from typed text.

    Returns False if no pending clarify exists or if the response was rejected
    (arbitrary prose for native interactive multi-choice clarifies).
    """
    entry = get_pending_for_session(
        session_key,
        include_choice_prompts=True,
        generation=generation,
        responder_id=responder_id,
    )
    if entry is None:
        return False

    coerced = _coerce_text_response(entry, response)
    if coerced is None:
        # Response rejected: message should continue as a normal turn
        return False

    return resolve_gateway_clarify(
        entry.clarify_id,
        coerced,
        session_key=session_key,
        generation=generation,
        responder_id=responder_id,
    )


def mark_awaiting_text(
    clarify_id: str,
    *,
    session_key: Optional[str] = None,
    generation: Optional[int] = None,
    responder_id: Optional[str] = None,
) -> bool:
    """Flip an entry into text-capture mode (user picked the 'Other' button).

    Returns True if the entry exists and was flipped, False otherwise.
    """
    with _lock:
        entry = _entries.get(clarify_id)
        if (
            entry is None
            or entry.state != _STATE_PENDING
            or not _identity_matches_locked(
                entry,
                session_key=session_key,
                generation=generation,
                responder_id=responder_id,
            )
        ):
            return False
        entry.awaiting_text = True
        return True


def has_pending(session_key: str) -> bool:
    """Return True when this session has at least one pending clarify entry."""
    with _lock:
        ids = _session_index.get(session_key) or []
        return any(
            (entry := _entries.get(cid)) is not None
            and entry.state == _STATE_PENDING
            for cid in ids
        )


def clear_session(session_key: str, *, generation: Optional[int] = None) -> int:
    """Resolve and drop every pending clarify for a session.

    Used by session-boundary cleanup (e.g. ``/new``, gateway shutdown,
    cached-agent eviction) so blocked agent threads don't hang past the
    end of their session.  Returns the number of entries cancelled.
    """
    with _lock:
        ids = list(_session_index.get(session_key, []) or [])
        cancelled = 0
        for clarify_id in ids:
            entry = _entries.get(clarify_id)
            if (
                entry is not None
                and (generation is None or entry.generation == generation)
                and _transition_locked(
                    entry,
                    _STATE_CANCELLED,
                    "",
                    remove=True,
                )
            ):
                cancelled += 1
        return cancelled


# =========================================================================
# Config
# =========================================================================

def resolve_clarify_timeout(config: dict) -> int:
    """Resolve the clarify timeout (seconds) from an already-loaded config dict.

    Single source of truth shared by every surface (messaging gateway, CLI,
    TUI/desktop) so the timeout can't drift between them.  Resolution order:

    1. legacy top-level ``clarify.timeout`` if a user explicitly set it,
    2. else the canonical ``agent.clarify_timeout``,
    3. else 3600 (1 hour).

    ``<= 0`` is preserved verbatim and means *unlimited* to callers (never
    auto-skip while the user is still deciding); the waiting loops translate
    that into a null deadline.  A non-numeric value falls back to 3600.
    """
    raw = (config.get("clarify") or {}).get("timeout")
    if raw is None:
        raw = (config.get("agent") or {}).get("clarify_timeout", 3600)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 3600


def get_clarify_timeout() -> int:
    """Read the clarify response timeout (seconds) from config.

    Defaults to 3600 (1 hour) — long enough that a user who steps away
    (meeting, AFK, slow to read) still finds a live entry when they tap
    the button, short enough that a genuinely abandoned prompt eventually
    unblocks the agent thread instead of pinning the running-agent guard
    forever.  The old 600s default evicted the entry mid-think, so a late
    tap landed on a dead entry and the agent hung on ``running: clarify``
    (#32762).

    Reads ``agent.clarify_timeout`` from config.yaml (see
    :func:`resolve_clarify_timeout` for the full resolution order).  Set to
    ``0`` (or negative) for an unlimited wait — never auto-skip while the user
    is still deciding.
    """
    try:
        from hermes_cli.config import load_config
        return resolve_clarify_timeout(load_config() or {})
    except Exception:
        return 3600


# =========================================================================
# Per-session notify hook (gateway → adapter bridge)
# =========================================================================
# Mirrors tools.approval's _gateway_notify_cbs: the gateway registers a
# per-session callback that sends the clarify prompt to the user.  The
# callback bridges sync→async (runs on the agent thread; schedules the
# adapter ``send_clarify`` call on the event loop).

_notify_cbs: Dict[str, Callable[[_ClarifyEntry], None]] = {}


def register_notify(session_key: str, cb: Callable[[_ClarifyEntry], None]) -> None:
    """Register a per-session notify callback used by ``clarify_callback``."""
    with _lock:
        _notify_cbs[session_key] = cb


def unregister_notify(session_key: str) -> None:
    """Drop the per-session notify callback and cancel any pending clarify entries."""
    with _lock:
        _notify_cbs.pop(session_key, None)
    # Cancel any pending entries so blocked threads unwind when the run
    # ends (interrupt, completion, gateway shutdown).
    clear_session(session_key)


def get_notify(session_key: str) -> Optional[Callable[[_ClarifyEntry], None]]:
    with _lock:
        return _notify_cbs.get(session_key)
