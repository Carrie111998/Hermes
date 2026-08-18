"""File-backed store for Telegram-tap model reroute overrides.

Phase 1 (events/rate_limit_signal.py) detects a rate limit and alerts
Mission Control. Phase 2 lets a Telegram tap divert traffic off a
rate-limited model for a bounded window; this module is the store that
records "route calls to (provider, model) at (replacement_provider,
replacement_model) until expires_at" and is read by everything downstream
(the divert-into-a-wall check, the adapter, the CLI).

State is FILE-backed for the same reason as rate_limit_signal.py: every
cron is a fresh process, the gateway is long-lived, and an in-memory store
would give each of them a different, wrong view of the same override.

Cache coherence rule
---------------------
Mirrors events/rate_limit_signal.py's rule verbatim -- read that module's
docstring for the full reasoning. Summary:

  * The cache is anchored to the file's identity marker
    ``(st_mtime_ns, st_size)`` as of the last moment this process was in
    sync with the file.
  * Marker matches the anchor -> the cache IS the truth. This is what keeps
    a FAILED WRITE from being silently discarded: re-reading the file after
    a failed write would hand back the pre-mutation state and make the
    caller think the write never happened even though it locally applied.
  * Marker has moved -> a foreign process (a different cron, the gateway)
    wrote the file; reload so its override becomes visible here.
  * A FAILED READ must NOT anchor the marker. Anchoring on a transient
    failure (Windows AV/indexer sharing violation, or a read racing another
    process's atomic_replace) would wedge this process at "no overrides"
    forever and then let its next write silently delete a foreign override.
    Instead the marker is left mismatched so the next call retries, bounded
    by _READ_FAILURE_RETRY_SECONDS so a persistently broken file is not
    retried on every single call.

Fail-open contract: ``get_override`` returns ``None`` for missing,
malformed, unreadable, or expired input and NEVER raises -- an override
changes which model answers a call, so a store that cannot be trusted must
behave exactly like "no override", never block or crash the call.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Hard ceiling on how long an override can live. No permanent override is
# expressible through this API -- permanence stays a deliberate config.yaml
# edit, never a Telegram tap. Set_override caps silently rather than
# rejecting an over-long request.
MAX_TTL_SECONDS = 24 * 3600

# Bound on how often a FAILED read is retried once the marker has been left
# deliberately unanchored. Mirrors rate_limit_signal.py's
# _READ_FAILURE_RETRY_SECONDS -- see that module's docstring for the full
# reasoning (transient failures recover within one cycle; a persistently
# broken file is retried at a low rate instead of once per call).
_READ_FAILURE_RETRY_SECONDS = 30

_store_cache: Optional[Dict[str, Any]] = None
# (st_mtime_ns, st_size) of the store file as of the last time this process
# was in sync with it. None means "the file did not exist / could not be
# stat'd".
_store_stat: Optional[Tuple[int, int]] = None
# Whether _store_cache reflects a confirmed read (or a legitimately-missing
# file), as opposed to a fail-open {} from a read this process could not
# complete. set_override()/clear_override() must not run a destructive
# whole-file write while this is False -- see _store_reliable().
_store_read_ok: bool = True
# monotonic() deadline before which a failed, still-unanchored read will not
# be retried. None when there is nothing to back off from.
_store_read_retry_at: Optional[float] = None


def reset_cache() -> None:
    """Test hook: drop the in-process cache and its file anchor."""
    global _store_cache, _store_stat, _store_read_ok, _store_read_retry_at
    _store_cache = None
    _store_stat = None
    _store_read_ok = True
    _store_read_retry_at = None


def _store_path() -> Path:
    from events.paths import model_overrides_path
    return model_overrides_path()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _override_key(provider: str, model: str) -> str:
    return f"{(provider or '').strip().lower()}/{(model or '').strip()}"


def _stat_marker() -> Optional[Tuple[int, int]]:
    """Identity of the store file right now, or None if it is not there."""
    try:
        st = os.stat(str(_store_path()))
        return (st.st_mtime_ns, st.st_size)
    except Exception:
        return None


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or None."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_expired(record: Any, now: datetime) -> bool:
    """Whether a record should be dropped on load."""
    if not isinstance(record, dict):
        return True
    expires_at = _parse_iso(record.get("expires_at"))
    if expires_at is None:
        # Unparseable/missing expiry cannot be bounded. Dropping it is the
        # fail-open choice: keeping an unbounded record risks a PERMANENT
        # override, which is far worse than losing one that should have
        # been renewed.
        return True
    return expires_at <= now


def _reap_expired(store: Dict[str, Any]) -> Dict[str, Any]:
    """Drop expired records. Returns ``store`` itself when nothing expired."""
    now = _now()
    live = {k: v for k, v in store.items() if not _is_expired(v, now)}
    return store if len(live) == len(store) else live


def _store_reliable() -> bool:
    """Whether the current cache reflects a confirmed read (or a
    legitimately-missing file), rather than a fail-open placeholder from a
    read this process could not complete."""
    return _store_read_ok


def _load_store() -> Dict[str, Any]:
    """Return the (reaped) override map. Fails open to {} on any error.

    Mirrors events/rate_limit_signal.py::_load_state -- see that module's
    docstring for the full cache-coherence reasoning.
    """
    global _store_cache, _store_stat, _store_read_ok, _store_read_retry_at
    marker = _stat_marker()

    if _store_cache is None:
        attempt_read = True
    elif marker == _store_stat:
        attempt_read = False
    elif not _store_read_ok and _store_read_retry_at is not None \
            and time.monotonic() < _store_read_retry_at:
        attempt_read = False
    else:
        attempt_read = True

    if attempt_read:
        try:
            with open(str(_store_path()), "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            _store_cache = data
            _store_stat = marker
            _store_read_ok = True
            _store_read_retry_at = None
        except FileNotFoundError:
            # Legitimately absent file: normal empty state, anchor it like a
            # successful read.
            _store_cache = {}
            _store_stat = marker
            _store_read_ok = True
            _store_read_retry_at = None
        except Exception:
            # The file exists but could not be read, or the read itself
            # blew up (permission error, sharing violation, decode error,
            # malformed JSON, or -- in tests -- a patched-out open()). We
            # never confirmed the file's true contents, so the marker must
            # NOT be anchored to it. Fail open to {} for this call, but
            # leave the marker mismatched so the next call retries, bounded
            # by the backoff above.
            logger.debug(
                "model_override: store read failed (swallowed)", exc_info=True
            )
            _store_cache = {}
            _store_read_ok = False
            _store_read_retry_at = time.monotonic() + _READ_FAILURE_RETRY_SECONDS

    reaped = _reap_expired(_store_cache)
    if reaped is not _store_cache:
        # The file still carries the expired entries; the next successful
        # write is what prunes them from disk. Leave the marker alone -- the
        # FILE did not change, only our view of which entries still count.
        _store_cache = reaped
    return _store_cache


def _publish_unsaved(store: Dict[str, Any]) -> None:
    """Adopt a mutation the disk rejected, WITHOUT re-anchoring the marker.

    See events/rate_limit_signal.py::_publish_unsaved -- same anti-flood
    reasoning: the write failed, the file (and its marker) are unchanged,
    so the next _load_store() would otherwise hand back the pre-mutation
    state and silently discard this call's effect.
    """
    global _store_cache
    _store_cache = store


def _save_store(store: Dict[str, Any]) -> bool:
    """Atomically persist the override map. Returns True on success."""
    global _store_cache, _store_stat
    try:
        path = _store_path()
        store_dir = os.path.dirname(str(path))
        os.makedirs(store_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=store_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(store, f)
            from utils import atomic_replace
            atomic_replace(tmp_path, str(path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        _store_cache = store
        _store_stat = _stat_marker()
        return True
    except Exception:
        logger.debug("model_override: store write failed (swallowed)", exc_info=True)
        return False


def _emit_audit(payload: Dict[str, Any], bus: Any) -> None:
    """Emit MODEL_OVERRIDE_SET. Best-effort: never raises, never blocks the
    override write it documents -- mirrors events/rate_limit_signal.py's
    ``_emit`` (lazy bus import/construction, blanket try/except degrading to
    a debug log). Spec §Containment: "each write emits an event, so
    audit.jsonl records who diverted what, when."
    """
    try:
        from events.schema import EventType, Priority
        active_bus = bus
        if active_bus is None:
            from events.bus import EventBus
            active_bus = EventBus()
        active_bus.emit(
            event_type=EventType.MODEL_OVERRIDE_SET,
            source="model_override",
            payload=payload,
            priority=Priority.NORMAL,
        )
    except Exception:
        logger.debug("model_override: audit emit failed (swallowed)", exc_info=True)


def get_override(provider: str, model: str) -> Optional[Dict[str, Any]]:
    """Return the active override record for (provider, model), or None.

    Never raises. Returns None for a missing, malformed, unreadable, or
    expired record -- fail open, always: an unreadable override file must
    behave exactly like "no override exists", never block a model call.
    """
    try:
        store = _load_store()
        record = store.get(_override_key(provider, model))
        if not isinstance(record, dict):
            return None
        return dict(record)
    except Exception:
        logger.debug("model_override.get_override failed (swallowed)", exc_info=True)
        return None


def set_override(
    *,
    provider: str,
    model: str,
    replacement_provider: str,
    replacement_model: str,
    ttl_seconds: int,
    set_by: str,
    bus: Any = None,
) -> Tuple[bool, str]:
    """Record an override routing (provider, model) to a replacement.

    Returns (ok, reason) rather than a bare bool so a Telegram tap can tell
    the user WHY a request failed instead of a button that silently does
    nothing.

    ttl_seconds is capped (not rejected) at MAX_TTL_SECONDS: no permanent
    override is expressible through this API. A self-target (replacement ==
    original) is rejected outright -- it would be a routing loop.

    Emits MODEL_OVERRIDE_SET on success only -- a rejected write (self-
    target, divert-into-a-wall) leaves nothing behind, so it must not leave
    an audit trail either. ``bus`` is test-injectable, mirroring
    events/rate_limit_signal.py.
    """
    try:
        if _override_key(provider, model) == _override_key(
            replacement_provider, replacement_model
        ):
            return False, "replacement is the same model as the original — that's a routing loop"

        # Reject a divert-into-a-wall: an override whose replacement target
        # already has its own open rate-limit episode would make the tap
        # LOOK like it worked while actually routing traffic onto another
        # dead model. Spec Sec:Containment.
        #
        # Fail-open direction is INVERTED here relative to every other
        # fail-open in this module (and in rate_limit_signal.py itself).
        # Everywhere else, "can't read the state" degrades to "act as if
        # there is no state" because the state IS the thing being acted on.
        # Here the state being read is telemetry (episode state) and the
        # action being gated is the OPERATOR's deliberate control action
        # (a Telegram tap explicitly diverting traffic). A telemetry read
        # must never veto an explicit operator instruction, so if
        # _load_state() raises or returns something we can't use, treat it
        # as "no open episodes" and ALLOW the write -- do not block it.
        # This looks backwards next to get_override()'s fail-open-to-None,
        # but the two are gating opposite things: that one fails open
        # because an unreadable override must never block a live model
        # call; this one fails open because an unreadable episode log must
        # never block an operator's explicit fix attempt.
        try:
            from events.rate_limit_signal import _episode_key, _load_state
            episode_state = _load_state()
            target_key = _episode_key(replacement_provider, replacement_model)
            target_has_open_episode = bool(
                isinstance(episode_state, dict) and target_key in episode_state
            )
        except Exception:
            logger.debug(
                "model_override.set_override: episode-state read failed "
                "(swallowed, fail-open to allowing the override)",
                exc_info=True,
            )
            target_has_open_episode = False

        if target_has_open_episode:
            return (
                False,
                f"{replacement_provider}/{replacement_model} is itself rate "
                "limited (open episode) — routing there would divert into "
                "a wall",
            )

        capped_ttl = min(int(ttl_seconds), MAX_TTL_SECONDS)
        now = _now()
        expires_at = now + timedelta(seconds=capped_ttl)

        record = {
            "provider": provider,
            "model": model,
            "replacement_provider": replacement_provider,
            "replacement_model": replacement_model,
            "expires_at": expires_at.isoformat(timespec="seconds"),
            "set_by": set_by,
            "set_at": _now_iso(),
        }

        # Deep copy: _load_store() returns the same cached dict object on
        # every call, and this record replaces one entry in it. Mutating in
        # place before we know the save succeeded would corrupt the shared
        # cache regardless of outcome -- same reasoning as
        # rate_limit_signal.record()'s deep copy.
        store = copy.deepcopy(_load_store())
        store[_override_key(provider, model)] = record

        saved = _save_store(store) if _store_reliable() else False
        if not saved:
            # See _publish_unsaved(): adopt the mutation locally even though
            # the disk write failed (or was skipped because the last read
            # was unreliable), so this process's own next read reflects the
            # override it just believes it set, instead of silently
            # discarding it.
            _publish_unsaved(store)

        # A non-positive ttl_seconds writes a record whose expires_at is
        # already <= now; _reap_expired() drops it on the very next load, so
        # get_override() correctly reports "no override" for it.
        _emit_audit(
            {
                "provider": provider,
                "model": model,
                "replacement_provider": replacement_provider,
                "replacement_model": replacement_model,
                "expires_at": record["expires_at"],
                "set_by": set_by,
                "action": "set",
            },
            bus,
        )
        return True, "ok"
    except Exception:
        logger.debug("model_override.set_override failed (swallowed)", exc_info=True)
        return False, "internal error setting override"


def clear_override(*, provider: str, model: str, bus: Any = None) -> bool:
    """Remove an active override. Returns True only if something was removed.

    Emits MODEL_OVERRIDE_SET only when a record was actually removed -- a
    no-op clear (nothing to remove) leaves nothing behind, so it must not
    leave an audit trail either. ``bus`` is test-injectable, mirroring
    events/rate_limit_signal.py.
    """
    try:
        key = _override_key(provider, model)
        store = dict(_load_store())
        removed = store.get(key)
        if removed is None:
            return False
        del store[key]

        saved = _save_store(store) if _store_reliable() else False
        if not saved:
            _publish_unsaved(store)

        payload = {
            "provider": provider,
            "model": model,
            "replacement_provider": (
                removed.get("replacement_provider", "")
                if isinstance(removed, dict) else ""
            ),
            "replacement_model": (
                removed.get("replacement_model", "")
                if isinstance(removed, dict) else ""
            ),
            "expires_at": (
                removed.get("expires_at", "") if isinstance(removed, dict) else ""
            ),
            "set_by": (
                removed.get("set_by", "") if isinstance(removed, dict) else ""
            ),
            "action": "cleared",
        }
        _emit_audit(payload, bus)
        return True
    except Exception:
        logger.debug("model_override.clear_override failed (swallowed)", exc_info=True)
        return False


def list_overrides() -> List[Dict[str, Any]]:
    """Return all currently-active (non-expired) override records."""
    try:
        store = _load_store()
        return [dict(v) for v in store.values() if isinstance(v, dict)]
    except Exception:
        logger.debug("model_override.list_overrides failed (swallowed)", exc_info=True)
        return []
