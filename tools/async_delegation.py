#!/usr/bin/env python3
"""
Async (background) delegation registry.

Backs ``delegate_task(background=true)``: the parent agent dispatches a
subagent that runs on a module-level daemon executor and returns a handle
immediately, so the user and the model can keep working while the child runs.

When the child finishes, a completion event is pushed onto the SHARED
``process_registry.completion_queue`` with ``type="async_delegation"``. The
CLI (``cli.py`` process_loop) and gateway (``_run_process_watcher`` /
``completion_queue`` drain) already poll that queue while the agent is idle
and forge a fresh user/internal turn from each event. We deliberately reuse
that rail rather than reaching into a running agent loop:

  - completions surface as a NEW turn when the agent is idle, never spliced
    between a tool result and an assistant message. That keeps strict
    message-role alternation legal and the prompt cache intact (hard
    invariant: never mutate past context).
  - we inherit the queue's de-dup, crash-recovery checkpoint, and the
    existing CLI + gateway drain wiring for free — no new drain loops in the
    two largest files in the repo.

The completion payload carries a RICH, self-contained task-source block (the
original goal, the context the parent supplied, toolsets, model, dispatch
time, status, and the full result summary). When the result re-enters the
conversation the parent may be deep in unrelated context and won't remember
why the subagent existed; the block lets it either use the result or
re-dispatch if the world has moved on.

This module owns ONLY the async lifecycle. The actual child build + run is
delegated back to ``delegate_tool._run_single_child`` via an injected
runner, so all the credential leasing, heartbeat, timeout, and result-shaping
logic stays in one place.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional

from agent.terminal_outcome import normalize_terminal_outcome
from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.thread_context import propagate_context_to_thread

logger = logging.getLogger(__name__)

# Back-compat alias — the daemon executor now lives in tools.daemon_pool so
# other subsystems (tool_executor, memory_manager, delegate_tool, skills_hub)
# can share it. Existing imports of ``_DaemonThreadPoolExecutor`` keep working.
_DaemonThreadPoolExecutor = DaemonThreadPoolExecutor


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
# A persistent daemon executor (NOT a `with ThreadPoolExecutor()` block, which
# would join on exit and defeat the whole point of async). Workers are daemon
# threads so a hard process exit doesn't hang on an in-flight child.
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_max_workers: int = 0

_records_lock = threading.Lock()
# delegation_id -> record dict. Kept for the lifetime of the run plus a short
# tail after completion so `list_async_delegations()` can show recent results.
_records: Dict[str, Dict[str, Any]] = {}

_DEFAULT_MAX_ASYNC_CHILDREN = 3
# How many delivered/dropped history records to retain for status queries.
_MAX_RETAINED_COMPLETED = 50
_DURABLE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DURABLE_PENDING = 1000
_PENDING_OVERFLOW_DISPOSITION = (
    "pending delivery obligation quarantined after durable pending cap was exceeded"
)
# A pending completion whose delivery keeps failing is retried across claim
# cycles (and across restarts via restore_undelivered_completions). Cap the
# attempts so an unroutable row converges to a terminal 'dropped' state
# instead of replaying on every restart forever.
_MAX_DELIVERY_ATTEMPTS = 8
_DELIVERY_CLAIM_LEASE_SECONDS = 300.0
_DELIVERY_RETRY_EPSILON_SECONDS = 0.05
# Final child results are more valuable than a transient SQLite error.  Keep
# the worker in the finalizing state while retrying the exact same payload,
# and publish only after one durable commit succeeds.
_FINALIZE_PERSIST_RETRY_DELAYS_SECONDS = (0.0, 0.05, 0.2)
_FINALIZE_SPOOL_SCHEMA = "hermes.async-finalize-spool.v1"
_FINALIZE_SPOOL_READ_LIMIT_BYTES = 64 * 1024 * 1024
_WAKE_RUNNING_STALE_SECONDS = 15 * 60.0
_WAKE_PROCESS_INSTANCE_ID = uuid.uuid4().hex
_ACTIVE_WAKE_CLAIMS_LOCK = threading.Lock()
_ACTIVE_WAKE_CLAIMS: set[str] = set()
_DB_LOCK = threading.Lock()
_LOST_DELIVERY_CLAIMS_LOCK = threading.Lock()
_LOST_DELIVERY_CLAIMS: set[tuple[str, str]] = set()

# Durable completion events share one process-global queue across every profile
# served by a gateway/desktop backend.  A bare delegation id is therefore not
# enough to find the authoritative row: identical ids can exist in independent
# profile state.db files, and raw worker/poller threads do not inherit the
# ContextVar that selected the originating HERMES_HOME.
#
# The queue payload carries only an opaque, process-local capability token.  The
# token resolves through the maps below to a validated store descriptor; an
# arbitrary string injected through persisted JSON cannot redirect a claim to a
# different path.  Live events are stamped only AFTER persistence, and restored
# events overwrite any same-named persisted field with a freshly issued token.
_EVENT_DELIVERY_STORE_KEY = "_hermes_delivery_store"
_EVENT_DELIVERY_PROFILE_GENERATION_KEY = (
    "_hermes_delivery_profile_generation"
)
_EVENT_DELIVERY_STORES_LOCK = threading.Lock()
_EVENT_DELIVERY_STORES_BY_TOKEN: Dict[str, "EventDeliveryStore"] = {}
_EVENT_DELIVERY_TOKENS_BY_STORE: Dict["EventDeliveryStore", str] = {}
_FROZEN_EVENT_DELIVERY_LOCK = threading.Lock()
_FROZEN_EVENT_DELIVERY_BY_PROFILE: Dict[str, "EventDeliveryStore"] = {}
_FROZEN_EVENT_DELIVERY_BY_HOME: Dict[str, "EventDeliveryStore"] = {}
_DELEGATION_ID_RE = re.compile(r"^deleg_[0-9a-f]{32}$")


@dataclass(frozen=True)
class EventDeliveryStore:
    """Trusted durable-store identity carried out-of-band on a queue event."""

    hermes_home: str
    source_home: str
    profile: Optional[str]
    profile_generation: str

    def __post_init__(self) -> None:
        if not self.hermes_home:
            raise ValueError("event delivery store home is required")
        if not self.source_home:
            raise ValueError("event delivery store source home is required")
        if not self.profile_generation:
            raise ValueError(
                "event delivery store profile generation is required"
            )


@dataclass(frozen=True, slots=True)
class DurableWakeClaim:
    """Result of the durable at-most-once execution CAS for one wake."""

    state: str
    claim_id: str = ""
    response: Optional[Dict[str, Any]] = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.state not in {
            "claimed",
            "in_progress",
            "completed",
            "uncertain",
        }:
            raise ValueError(f"invalid durable wake claim state: {self.state}")


def _canonical_home(path: str | Path) -> Path:
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("Hermes home cannot be empty")
    return Path(raw).expanduser().resolve()


def _capture_event_delivery_store(
    *,
    home: Path,
    profile: Optional[str],
    initialize_marker: bool = False,
) -> EventDeliveryStore:
    """Capture the directory generation that owns a durable event store."""

    from gateway.api_request_scope import capture_api_profile_identity

    identity = capture_api_profile_identity(
        profile or "default",
        home,
        initialize_marker=initialize_marker,
    )
    return EventDeliveryStore(
        hermes_home=identity.canonical_home,
        source_home=identity.source_home,
        profile=profile,
        profile_generation=identity.profile_generation,
    )


def event_delivery_store_from_profile_identity(
    identity,
) -> EventDeliveryStore:
    """Construct a store from one already-frozen host identity verbatim."""

    store = EventDeliveryStore(
        hermes_home=str(identity.canonical_home),
        source_home=str(identity.source_home),
        profile=str(identity.profile or "default"),
        profile_generation=str(identity.profile_generation),
    )
    _verify_event_delivery_store(store)
    return store


def register_frozen_event_delivery_inventory(identities) -> None:
    """Install one exact listener-owned inventory for detached dispatch.

    A process may host only one live gateway listener authority.  Repeating
    the exact same registration is harmless (several startup consumers share
    the inventory), but a second runner must not replace the first runner's
    profile generations while its adapters and detached workers are still
    alive.
    """

    stores = tuple(
        event_delivery_store_from_profile_identity(identity)
        for identity in identities
    )
    by_profile: Dict[str, EventDeliveryStore] = {}
    by_home: Dict[str, EventDeliveryStore] = {}
    for store in stores:
        profile = str(store.profile or "default")
        if profile in by_profile or store.hermes_home in by_home:
            raise ValueError(
                "frozen event-delivery inventory is not injective"
            )
        by_profile[profile] = store
        by_home[store.hermes_home] = store
    if not stores:
        raise ValueError("frozen event-delivery inventory cannot be empty")
    with _FROZEN_EVENT_DELIVERY_LOCK:
        if (
            _FROZEN_EVENT_DELIVERY_BY_PROFILE
            or _FROZEN_EVENT_DELIVERY_BY_HOME
        ):
            if (
                _FROZEN_EVENT_DELIVERY_BY_PROFILE == by_profile
                and _FROZEN_EVENT_DELIVERY_BY_HOME == by_home
            ):
                return
            raise ValueError(
                "a different frozen event-delivery inventory is already "
                "registered in this process"
            )
        _FROZEN_EVENT_DELIVERY_BY_PROFILE.update(by_profile)
        _FROZEN_EVENT_DELIVERY_BY_HOME.update(by_home)


def _verify_event_delivery_store(store: EventDeliveryStore) -> None:
    """Reject an event store whose directory was replaced in place."""

    from gateway.api_request_scope import (
        APIProfileIdentity,
        verify_api_profile_identity,
    )

    verify_api_profile_identity(
        APIProfileIdentity(
            profile=store.profile or "default",
            source_home=store.source_home,
            canonical_home=store.hermes_home,
            profile_generation=store.profile_generation,
        )
    )


def resolve_event_delivery_store(
    *,
    hermes_home: str | Path | None = None,
    profile: str | None = None,
) -> EventDeliveryStore:
    """Validate and canonicalize an explicit durable-completion store target.

    ``profile`` is optional for launch-profile and custom-HERMES_HOME callers.
    When supplied, it is validated against the canonical profile registry and
    the supplied home must be that profile's actual directory.  This keeps an
    external/persisted profile label from becoming arbitrary filesystem
    authority.
    """

    profile_name = str(profile or "").strip() or None
    expected_home: Path | None = None
    expected_source_home: Path | None = None
    if profile_name is not None:
        from hermes_cli.profiles import (
            get_profile_dir,
            profile_exists,
            validate_profile_name,
        )

        validate_profile_name(profile_name)
        if not profile_exists(profile_name):
            raise ValueError(f"Hermes profile does not exist: {profile_name!r}")
        expected_source_home = Path(
            get_profile_dir(profile_name)
        ).expanduser().absolute()
        expected_home = _canonical_home(expected_source_home)

    if hermes_home is None:
        source_home = (
            expected_source_home
            or Path(get_hermes_home()).expanduser().absolute()
        )
    else:
        source_home = Path(hermes_home).expanduser().absolute()
    home = _canonical_home(source_home)

    if expected_home is not None and home != expected_home:
        raise ValueError(
            f"Hermes profile {profile_name!r} resolves to {expected_home}, "
            f"not {home}"
        )
    return _capture_event_delivery_store(
        home=expected_source_home or source_home,
        profile=profile_name,
        # Explicit legacy/single-profile callers have no runner-owned
        # inventory.  They may initialize the marker, but never recreate the
        # profile directory itself.
        initialize_marker=True,
    )


def _register_event_delivery_store(store: EventDeliveryStore) -> str:
    """Return an opaque process-local token for one validated store."""

    _verify_event_delivery_store(store)
    with _EVENT_DELIVERY_STORES_LOCK:
        token = _EVENT_DELIVERY_TOKENS_BY_STORE.get(store)
        if token is None:
            token = uuid.uuid4().hex
            _EVENT_DELIVERY_TOKENS_BY_STORE[store] = token
            _EVENT_DELIVERY_STORES_BY_TOKEN[token] = store
        return token


def _stamp_event_delivery_store(
    evt: Dict[str, Any],
    store: EventDeliveryStore,
) -> None:
    """Overwrite any untrusted persisted stamp with a process-local capability."""

    evt[_EVENT_DELIVERY_STORE_KEY] = _register_event_delivery_store(store)


def get_event_delivery_store(
    evt: Dict[str, Any],
) -> Optional[EventDeliveryStore]:
    """Resolve an event's trusted store, or ``None`` for legacy/forged stamps."""

    token = evt.get(_EVENT_DELIVERY_STORE_KEY)
    if not isinstance(token, str) or not token:
        return None
    with _EVENT_DELIVERY_STORES_LOCK:
        return _EVENT_DELIVERY_STORES_BY_TOKEN.get(token)


def event_has_delivery_store_stamp(evt: Dict[str, Any]) -> bool:
    """Return whether an event claims the new store-scoped delivery contract.

    Callers pair this with :func:`get_event_delivery_store`: no stamp means a
    legacy event, while a present-but-unresolvable stamp is forged/stale and
    must fail closed.
    """

    return _EVENT_DELIVERY_STORE_KEY in evt


def _current_event_delivery_store() -> EventDeliveryStore:
    """Build the trusted store for a live completion in the current task."""

    source_home = Path(get_hermes_home()).expanduser().absolute()
    home = _canonical_home(source_home)
    candidate = ""
    try:
        from gateway.session_context import get_session_env

        candidate = str(
            get_session_env("HERMES_SESSION_PROFILE", "") or ""
        ).strip()
    except Exception:
        logger.debug(
            "Could not read async-delivery profile metadata",
            exc_info=True,
        )

    with _FROZEN_EVENT_DELIVERY_LOCK:
        frozen_by_profile = dict(_FROZEN_EVENT_DELIVERY_BY_PROFILE)
        frozen_by_home = dict(_FROZEN_EVENT_DELIVERY_BY_HOME)
    if frozen_by_profile:
        if candidate:
            store = frozen_by_profile.get(candidate)
            if store is None:
                raise ValueError(
                    "async-delivery profile metadata is not in the frozen "
                    f"runner inventory: {candidate!r}"
                )
            if store.hermes_home != str(home):
                raise ValueError(
                    "async-delivery profile metadata does not match the "
                    "current frozen profile home"
                )
        else:
            store = frozen_by_home.get(str(home))
            if store is None:
                raise ValueError(
                    "current Hermes home is not in the frozen runner "
                    "event-delivery inventory"
                )
        _verify_event_delivery_store(store)
        return store

    if candidate:
        # Legacy single-profile paths may enrich with a valid named profile,
        # but an invalid name/home binding is never downgraded to profile=None.
        return resolve_event_delivery_store(
            hermes_home=source_home,
            profile=candidate,
        )
    return _capture_event_delivery_store(
        home=source_home,
        profile=None,
        initialize_marker=True,
    )


def _record_event_delivery_store(record: Dict[str, Any]) -> EventDeliveryStore:
    """Return the dispatch-captured store; fall back only for legacy records."""

    store = record.get("_delivery_store")
    if isinstance(store, EventDeliveryStore):
        return store
    return _current_event_delivery_store()


@contextmanager
def _delivery_home_scope(home: str | Path) -> Iterator[None]:
    """Bind one trusted store's HERMES_HOME for ambient SQLite helpers."""

    token = set_hermes_home_override(_canonical_home(home))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


@contextmanager
def _optional_delivery_store_scope(
    store: EventDeliveryStore | None,
) -> Iterator[None]:
    if store is None:
        yield
        return
    with _delivery_home_scope(store.hermes_home):
        yield


@contextmanager
def _event_delivery_scope(evt: Dict[str, Any]) -> Iterator[None]:
    """Scope an event operation to its store; reject forged private stamps."""

    if _EVENT_DELIVERY_STORE_KEY not in evt:
        # Backward compatibility for same-process events created before the
        # durable-store contract: retain the historical ambient-home behavior.
        yield
        return
    store = get_event_delivery_store(evt)
    if store is None:
        raise ValueError("Untrusted async-delegation delivery-store stamp")
    try:
        _verify_event_delivery_store(store)
    except Exception as exc:
        raise ValueError(
            "Async-delegation delivery store changed after dispatch"
        ) from exc
    with _delivery_home_scope(store.hermes_home):
        yield


def _mark_delivery_claim_lost(delegation_id: str, claim_id: str) -> None:
    """Remember a locally observed lease loss until the consumer relinquishes it."""

    with _LOST_DELIVERY_CLAIMS_LOCK:
        _LOST_DELIVERY_CLAIMS.add((delegation_id, claim_id))


def _delivery_claim_was_lost(delegation_id: str, claim_id: str) -> bool:
    with _LOST_DELIVERY_CLAIMS_LOCK:
        return (delegation_id, claim_id) in _LOST_DELIVERY_CLAIMS


def _forget_lost_delivery_claim(delegation_id: str, claim_id: str) -> None:
    with _LOST_DELIVERY_CLAIMS_LOCK:
        _LOST_DELIVERY_CLAIMS.discard((delegation_id, claim_id))


class DeliveryClaimRenewal:
    """Callable renewal handle with an observable ownership-loss signal."""

    def __init__(
        self,
        *,
        delegation_id: str = "",
        claim_id: str = "",
        stopped: Optional[threading.Event] = None,
    ) -> None:
        self.delegation_id = delegation_id
        self.claim_id = claim_id
        self._stopped = stopped or threading.Event()
        self._ownership_lost = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def ownership_lost(self) -> bool:
        return self._ownership_lost.is_set()

    def mark_ownership_lost(self) -> None:
        self._ownership_lost.set()
        if self.delegation_id and self.claim_id:
            _mark_delivery_claim_lost(self.delegation_id, self.claim_id)

    def attach_thread(self, thread: threading.Thread) -> None:
        self._thread = thread

    def stop(self) -> None:
        self._stopped.set()
        thread = self._thread
        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=1.0)

    def __call__(self) -> None:
        self.stop()


def _normalize_optional_runtime_effect(
    value: Any,
) -> Optional[Dict[str, Any]]:
    """Validate host metadata at each async-registry boundary."""

    if value is None:
        return None
    from agent.runtime_effects import normalize_runtime_effect

    return normalize_runtime_effect(value)


def _normalize_optional_api_execution_context(
    value: Any,
) -> Optional[Dict[str, Any]]:
    """Validate the non-secret API wake envelope at durable boundaries."""

    if value is None:
        return None
    from gateway.api_execution_context import normalize_api_execution_context

    return normalize_api_execution_context(value, allow_none=False)


def _normalize_optional_durable_model(
    value: Any,
    *,
    field: str = "async_delegation.model",
) -> Optional[str]:
    """Validate model metadata before durable async persistence/replay."""

    if value is None:
        return None
    from gateway.api_execution_context import normalize_model_identifier

    normalized = normalize_model_identifier(
        value,
        field=field,
    )
    return normalized or None


def _scrub_terminal_model_metadata(value: Any) -> Any:
    """Copy a terminal payload while removing unsafe model annotations.

    The result summary/error remain user-visible content. Only exact
    ``model`` metadata fields are execution identifiers and therefore pass
    through the durable identifier guard.
    """

    if not isinstance(value, dict):
        return value
    cleaned = dict(value)
    if "model" in cleaned:
        try:
            cleaned["model"] = _normalize_optional_durable_model(
                cleaned.get("model"),
                field="async terminal model",
            )
        except ValueError:
            cleaned.pop("model", None)
            cleaned["model_metadata_rejected"] = True
    results = cleaned.get("results")
    if isinstance(results, list):
        cleaned["results"] = [
            _scrub_terminal_model_metadata(item)
            for item in results
        ]
    return cleaned


def _validate_api_execution_origin(
    context: Optional[Dict[str, Any]],
    origin_session_id: str,
) -> None:
    """Admit durable API metadata only from the bound API parent session."""

    if context is None:
        try:
            from gateway.session_context import get_session_env

            platform = str(
                get_session_env("HERMES_SESSION_PLATFORM", "") or ""
            ).strip()
        except Exception:
            platform = ""
        if platform == "api_server":
            raise ValueError(
                "api_server background delegation requires a durable API "
                "execution context"
            )
        return
    supplied_origin = str(origin_session_id or "").strip()
    trusted_origin = _current_origin_session_id()
    if not trusted_origin or supplied_origin != trusted_origin:
        raise ValueError(
            "API execution context requires the currently bound "
            "originating API session"
        )


# ---------------------------------------------------------------------------
# Stale-delegation detection (progress-based, on by default)
# ---------------------------------------------------------------------------
# A detached runner that wedges before returning (e.g. stuck inside its first
# model API call — #60203) never reaches its ``finally`` finalizer, so no
# completion event is ever published: the delegation shows "dispatched"
# forever and the owning session looks silent until a process restart. We do
# NOT fix this with a wall-clock timeout — legitimate heavy subagent work
# (deep reviews, research fan-outs, slow reasoning models) must never be
# killed for taking long (see delegate_tool.DEFAULT_CHILD_TIMEOUT rationale).
# Instead a single monitor thread watches per-dispatch PROGRESS (api-call
# count + current tool, via an injected ``progress_fn``): a child that is
# advancing is left alone forever; a child with NO progress past the stale
# threshold is interrupted, given a grace window to unwind and deliver its
# partial results through the normal finalize path, and only force-finalized
# with a terminal ``stalled`` event if it never returns.
#
# Thresholds mirror the sync-path heartbeat staleness monitor in
# delegate_tool: idle (not inside a tool) stays tight so a wedged first API
# call is caught quickly; in-tool is much higher so legitimately slow tools
# (long terminal commands, big fetches) get time to finish.
_STALE_CHECK_INTERVAL = 30.0  # seconds between monitor sweeps
_STALE_IDLE_SECONDS = 450.0  # no progress, no current tool → stalled
_STALE_IN_TOOL_SECONDS = 1200.0  # no progress while inside a tool → stalled
_STALL_GRACE_SECONDS = 120.0  # after interrupt, time for the runner to return

_monitor_lock = threading.Lock()
_monitor_thread: Optional[threading.Thread] = None
_monitor_stop = threading.Event()


def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        _initialize_schema(conn)
    except Exception:
        # A PRAGMA/DDL failure after a successful connect() must not leak the
        # just-opened connection back to the caller.
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="state.db (async_delegation)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            event_json TEXT,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            task_json TEXT,
            delivery_claim TEXT,
            delivery_claimed_at REAL,
            origin_session_id TEXT NOT NULL DEFAULT '',
            delivery_disposition_reason TEXT,
            wake_fingerprint TEXT,
            wake_state TEXT NOT NULL DEFAULT 'not_started',
            wake_claim_id TEXT,
            wake_owner_pid INTEGER,
            wake_owner_started_at INTEGER,
            wake_owner_instance TEXT,
            wake_claimed_at REAL,
            wake_response_json TEXT,
            wake_disposition_reason TEXT
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(async_delegations)")}
    for name, sql_type in (
        ("owner_pid", "INTEGER"),
        ("owner_started_at", "INTEGER"),
        ("task_json", "TEXT"),
        ("delivery_claim", "TEXT"),
        ("delivery_claimed_at", "REAL"),
        # Raw api_server session id (X-Hermes-Session-Id) of the ORIGINATING
        # request — the wake self-post target. Without persisting it,
        # completions recovered after a process restart are unroutable on
        # api_server (the in-memory record that carried it is gone).
        ("origin_session_id", "TEXT"),
        # Human-readable durable explanation for terminal delivery
        # dispositions such as an intentionally quarantined pending overflow.
        # The semantic result/event payloads stay untouched and queryable.
        ("delivery_disposition_reason", "TEXT"),
        ("wake_fingerprint", "TEXT"),
        ("wake_state", "TEXT NOT NULL DEFAULT 'not_started'"),
        ("wake_claim_id", "TEXT"),
        ("wake_owner_pid", "INTEGER"),
        ("wake_owner_started_at", "INTEGER"),
        ("wake_owner_instance", "TEXT"),
        ("wake_claimed_at", "REAL"),
        ("wake_response_json", "TEXT"),
        ("wake_disposition_reason", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE async_delegations ADD COLUMN {name} {sql_type}")


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, and ALWAYS close it.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back the
    transaction; they do not close the connection. Using ``with _connect()``
    alone therefore leaks a connection — and its WAL/SHM file descriptors — on
    every durable dispatch, completion, and delivery-claim, deferring the close
    to the garbage collector. On a long-running gateway that exhausts
    ``RLIMIT_NOFILE`` (the cron-ledger sibling of this bug was #69567 / PR #69594).
    """
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _persist_dispatch(record: Dict[str, Any]) -> None:
    now = time.time()
    store = _record_event_delivery_store(record)
    try:
        from gateway.status import get_process_start_time
        owner_started_at = get_process_start_time(__import__("os").getpid())
    except Exception:
        owner_started_at = None
    task_payload = {
        key: record.get(key)
        for key in (
            "goal",
            "goals",
            "context",
            "toolsets",
            "role",
            "model",
            "is_batch",
            "runtime_effect",
            "api_execution_context",
        )
        if key in record
    }
    if "runtime_effect" in task_payload:
        task_payload["runtime_effect"] = _normalize_optional_runtime_effect(
            task_payload["runtime_effect"]
        )
    if "model" in task_payload:
        task_payload["model"] = _normalize_optional_durable_model(
            task_payload["model"],
            field="async dispatch.model",
        )
    if "api_execution_context" in task_payload:
        task_payload["api_execution_context"] = (
            _normalize_optional_api_execution_context(
                task_payload["api_execution_context"]
            )
        )
    task_payload[_EVENT_DELIVERY_PROFILE_GENERATION_KEY] = (
        store.profile_generation
    )
    with _delivery_home_scope(store.hermes_home):
        with _DB_LOCK, _transaction() as conn:
            conn.execute(
                """INSERT INTO async_delegations
                   (delegation_id, origin_session, origin_ui_session_id,
                    parent_session_id, state, dispatched_at, updated_at,
                    delivery_state, delivery_attempts, owner_pid,
                    owner_started_at, task_json, origin_session_id)
                   VALUES (?, ?, ?, ?, 'running', ?, ?, 'pending', 0, ?, ?, ?, ?)""",
                (record["delegation_id"], record.get("session_key", ""),
                 record.get("origin_ui_session_id", ""), record.get("parent_session_id"),
                 record["dispatched_at"], now, __import__("os").getpid(),
                 owner_started_at, json.dumps(task_payload),
                 record.get("origin_session_id", "")),
            )
        try:
            _prune_durable_records()
        except Exception:
            # Retention maintenance is not part of dispatch admission.  The
            # INSERT above is already committed; surfacing a prune failure as
            # a dispatch failure would leave a durable running row without a
            # worker.
            logger.warning(
                "Async delegation retention prune failed after durable "
                "dispatch",
                exc_info=True,
            )


def _delete_durable_delegation(
    delegation_id: str,
    *,
    store: EventDeliveryStore | None = None,
) -> None:
    with _optional_delivery_store_scope(store):
        with _DB_LOCK, _transaction() as conn:
            conn.execute(
                "DELETE FROM async_delegations WHERE delegation_id=?",
                (delegation_id,),
            )


def _prune_durable_records() -> None:
    """Bound delivery history without silently deleting pending obligations.

    ``_MAX_RETAINED_COMPLETED`` applies only to completed *delivery history*
    (delivered/dropped), never to a completion that still has to reach its
    owner.  Pending obligations have their own cap.  When that cap is enforced,
    the oldest overflow is explicitly quarantined as ``dropped`` with a durable
    reason while retaining the original event/result for inspection.
    """
    now = time.time()
    cutoff = now - _DURABLE_RETENTION_SECONDS
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            "DELETE FROM async_delegations "
            "WHERE state NOT IN ('running','finalizing') "
            "AND delivery_state IN ('delivered','dropped') AND updated_at < ?",
            (cutoff,),
        )
        history_count = conn.execute(
            "SELECT COUNT(*) FROM async_delegations "
            "WHERE state NOT IN ('running','finalizing') "
            "AND delivery_state IN ('delivered','dropped')"
        ).fetchone()[0]
        excess = max(0, history_count - _MAX_RETAINED_COMPLETED)
        if excess:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing')
                       AND delivery_state IN ('delivered','dropped')
                     ORDER BY updated_at ASC, delegation_id ASC LIMIT ?
                   )""",
                (excess,),
            )
        pending_count = conn.execute(
            """SELECT COUNT(*) FROM async_delegations
               WHERE state NOT IN ('running','finalizing')
                 AND delivery_state='pending'"""
        ).fetchone()[0]
        overflow = max(0, pending_count - _MAX_DURABLE_PENDING)
        if overflow:
            quarantined = conn.execute(
                """UPDATE async_delegations
                   SET delivery_state='dropped',
                       delivery_disposition_reason=?,
                       delivery_claim=NULL,
                       delivery_claimed_at=NULL,
                       updated_at=?
                   WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing')
                       AND delivery_state='pending'
                       AND (delivery_claim IS NULL
                            OR delivery_claimed_at IS NULL
                            OR delivery_claimed_at < ?)
                     ORDER BY updated_at ASC, delegation_id ASC LIMIT ?
                   )""",
                (
                    _PENDING_OVERFLOW_DISPOSITION,
                    now,
                    now - _DELIVERY_CLAIM_LEASE_SECONDS,
                    overflow,
                ),
            )
            if quarantined.rowcount:
                logger.error(
                    "Quarantined %d async delegation completion(s) after the "
                    "durable pending-delivery cap (%d) was exceeded; semantic "
                    "event/result payloads remain queryable.",
                    quarantined.rowcount,
                    _MAX_DURABLE_PENDING,
                )
            if quarantined.rowcount < overflow:
                logger.warning(
                    "Deferred quarantine of %d pending async delegation "
                    "completion(s) because they hold a live delivery lease.",
                    overflow - quarantined.rowcount,
                )


def _persist_completion(
    event: Dict[str, Any],
    result: Dict[str, Any],
) -> None:
    now = time.time()
    # These are host/runtime annotations, never durable event content.  In
    # particular the delivery-store token is a process-local capability and
    # must not survive into SQLite (or a later process could replay it as
    # filesystem authority).  ``restored`` has always been documented as
    # in-memory-only.
    persisted_event = _scrub_terminal_model_metadata(event)
    persisted_result = _scrub_terminal_model_metadata(result)
    persisted_event.pop(_EVENT_DELIVERY_STORE_KEY, None)
    persisted_event.pop("restored", None)
    if not persisted_event.get(_EVENT_DELIVERY_PROFILE_GENERATION_KEY):
        persisted_event[_EVENT_DELIVERY_PROFILE_GENERATION_KEY] = (
            _current_event_delivery_store().profile_generation
        )
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET state=?, completed_at=?,
               updated_at=?, event_json=?, result_json=?,
               delivery_state='pending' WHERE delegation_id=?""",
            (
                event.get("status", "completed"),
                event.get("completed_at", now),
                now,
                json.dumps(persisted_event),
                json.dumps(persisted_result),
                event["delegation_id"],
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                "durable async delegation row is missing during finalization"
            )


def _canonical_wake_response(
    response: Dict[str, Any],
) -> tuple[str, Dict[str, Any]]:
    """Return strict canonical JSON plus the detached JSON-safe value."""

    if not isinstance(response, dict):
        raise ValueError("durable wake response must be a JSON object")
    try:
        encoded = json.dumps(
            response,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "durable wake response must contain only finite JSON values"
        ) from exc
    if not isinstance(decoded, dict):
        raise ValueError("durable wake response must be a JSON object")
    return encoded, decoded


def durable_wake_execution_fingerprint(
    *,
    delegation_id: str,
    destination: Mapping[str, Any],
    text: str,
    runtime_effect: Optional[Dict[str, Any]],
    execution_context: Optional[Dict[str, Any]],
    store: EventDeliveryStore,
) -> str:
    """Bind one durable wake execution to its exact semantic destination."""

    delegation_id = str(delegation_id or "").strip()
    if not delegation_id:
        raise ValueError("durable wake fingerprint requires a delegation id")
    if not isinstance(destination, Mapping) or not destination:
        raise ValueError("durable wake fingerprint requires a destination")
    if not isinstance(text, str):
        raise ValueError("durable wake fingerprint text must be a string")
    _verify_event_delivery_store(store)

    from agent.runtime_effects import normalize_optional_runtime_effect
    from gateway.api_execution_context import execution_context_digest

    try:
        canonical_destination = json.loads(
            json.dumps(
                dict(destination),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "durable wake destination must contain only finite JSON values"
        ) from exc
    if not isinstance(canonical_destination, dict):
        raise ValueError("durable wake destination must be a JSON object")
    canonical = json.dumps(
        {
            "schema": "hermes.durable-wake-execution.v1",
            "delegation_id": delegation_id,
            "profile_identity": {
                "profile": store.profile or "default",
                "source_home": store.source_home,
                "canonical_home": store.hermes_home,
                "profile_generation": store.profile_generation,
            },
            "destination": canonical_destination,
            "text_sha256": hashlib.sha256(
                text.encode("utf-8")
            ).hexdigest(),
            "runtime_effect": normalize_optional_runtime_effect(
                runtime_effect
            ),
            "execution_context_sha256": execution_context_digest(
                execution_context
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (
        "hermes-durable-wake-v1-"
        + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )


def _wake_owner_started_at(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time

        started = get_process_start_time(pid)
        return int(started) if started is not None else None
    except Exception:
        return None


def _wake_owner_is_stale(
    *,
    claim_id: str,
    owner_pid: Optional[int],
    owner_started_at: Optional[int],
    owner_instance: str,
    claimed_at: Optional[float],
    now: float,
) -> bool:
    """Conservatively identify a running owner that cannot still commit."""

    if owner_instance == _WAKE_PROCESS_INSTANCE_ID:
        with _ACTIVE_WAKE_CLAIMS_LOCK:
            if claim_id in _ACTIVE_WAKE_CLAIMS:
                return False
        return True
    if owner_pid:
        try:
            from gateway.status import _pid_exists, get_process_start_time

            if not _pid_exists(int(owner_pid)):
                return True
            if owner_started_at is not None:
                current_started = get_process_start_time(int(owner_pid))
                if current_started is not None:
                    if int(current_started) != int(owner_started_at):
                        # The numeric PID was reused by another process.
                        return True
                    # Positively identified live owner: elapsed wall time alone
                    # can never revoke its completion CAS authority.
                    return False
        except Exception:
            # Fall through to the age bound only when liveness could not be
            # established.  Unknown is not immediate proof of death.
            pass
    # Missing/unverifiable owner identity eventually becomes explicit
    # uncertainty.  It is never re-acquired for another execution.
    return bool(
        claimed_at is None
        or now - float(claimed_at) >= _WAKE_RUNNING_STALE_SECONDS
    )


def claim_durable_wake_execution(
    *,
    delegation_id: str,
    idempotency_key: str,
    store: EventDeliveryStore,
) -> DurableWakeClaim:
    """CAS one persisted completion into an at-most-once wake execution.

    ``uncertain`` is terminal for this request: callers MUST NOT run agent
    tools.  A later retry may observe ``completed`` if the original live owner
    commits its canonical response in the meantime.
    """

    delegation_id = str(delegation_id or "").strip()
    fingerprint = str(idempotency_key or "").strip()
    if not delegation_id or not fingerprint:
        return DurableWakeClaim(
            state="uncertain",
            reason="durable wake identity is incomplete",
        )
    _verify_event_delivery_store(store)
    now = time.time()
    pid = int(__import__("os").getpid())
    started_at = _wake_owner_started_at(pid)
    claim_id = uuid.uuid4().hex
    with _delivery_home_scope(store.hermes_home):
        with _DB_LOCK, _transaction() as conn:
            row = conn.execute(
                """SELECT state, event_json, result_json, wake_fingerprint,
                          wake_state, wake_claim_id, wake_owner_pid,
                          wake_owner_started_at, wake_owner_instance,
                          wake_claimed_at, wake_response_json,
                          wake_disposition_reason
                   FROM async_delegations WHERE delegation_id=?""",
                (delegation_id,),
            ).fetchone()
            if row is None:
                return DurableWakeClaim(
                    state="uncertain",
                    reason="durable delegation row is missing",
                )
            (
                delegation_state,
                event_json,
                result_json,
                saved_fingerprint,
                wake_state,
                saved_claim_id,
                owner_pid,
                owner_started_at,
                owner_instance,
                claimed_at,
                response_json,
                disposition_reason,
            ) = row
            wake_state = str(wake_state or "not_started")
            saved_fingerprint = str(saved_fingerprint or "")
            if delegation_state in {"running", "finalizing"} or not (
                event_json and result_json
            ):
                return DurableWakeClaim(
                    state="uncertain",
                    reason="durable delegation has no committed terminal result",
                )
            if wake_state == "completed":
                if saved_fingerprint != fingerprint:
                    return DurableWakeClaim(
                        state="uncertain",
                        reason="durable wake fingerprint mismatch",
                    )
                try:
                    response = json.loads(response_json or "")
                except (TypeError, ValueError):
                    response = None
                if not isinstance(response, dict):
                    return DurableWakeClaim(
                        state="uncertain",
                        reason="durable wake response is malformed",
                    )
                return DurableWakeClaim(
                    state="completed",
                    response=response,
                )
            if wake_state == "uncertain":
                return DurableWakeClaim(
                    state="uncertain",
                    reason=str(
                        disposition_reason
                        or "durable wake outcome is uncertain"
                    ),
                )
            if wake_state == "not_started":
                claimed = conn.execute(
                    """UPDATE async_delegations
                       SET wake_fingerprint=?, wake_state='running',
                           wake_claim_id=?, wake_owner_pid=?,
                           wake_owner_started_at=?, wake_owner_instance=?,
                           wake_claimed_at=?, wake_response_json=NULL,
                           wake_disposition_reason=NULL, updated_at=?
                       WHERE delegation_id=?
                         AND wake_state='not_started'""",
                    (
                        fingerprint,
                        claim_id,
                        pid,
                        started_at,
                        _WAKE_PROCESS_INSTANCE_ID,
                        now,
                        now,
                        delegation_id,
                    ),
                )
                if claimed.rowcount == 1:
                    with _ACTIVE_WAKE_CLAIMS_LOCK:
                        _ACTIVE_WAKE_CLAIMS.add(claim_id)
                    return DurableWakeClaim(
                        state="claimed",
                        claim_id=claim_id,
                    )
                return DurableWakeClaim(
                    state="in_progress",
                    reason="durable wake claim raced with another consumer",
                )
            if wake_state != "running":
                return DurableWakeClaim(
                    state="uncertain",
                    reason=f"invalid durable wake state: {wake_state}",
                )
            if saved_fingerprint != fingerprint:
                return DurableWakeClaim(
                    state="uncertain",
                    reason="durable wake fingerprint mismatch",
                )
            if not _wake_owner_is_stale(
                claim_id=str(saved_claim_id or ""),
                owner_pid=owner_pid,
                owner_started_at=owner_started_at,
                owner_instance=str(owner_instance or ""),
                claimed_at=claimed_at,
                now=now,
            ):
                return DurableWakeClaim(
                    state="in_progress",
                    reason="durable wake execution is already running",
                )
            reason = (
                "durable wake owner disappeared or became stale before "
                "recording a terminal response; outcome may include effects"
            )
            transitioned = conn.execute(
                """UPDATE async_delegations
                   SET wake_state='uncertain',
                       wake_disposition_reason=?, updated_at=?
                   WHERE delegation_id=? AND wake_state='running'
                     AND wake_claim_id=? AND wake_fingerprint=?""",
                (
                    reason,
                    now,
                    delegation_id,
                    str(saved_claim_id or ""),
                    fingerprint,
                ),
            ).rowcount
            if transitioned != 1:
                raced = conn.execute(
                    """SELECT wake_state, wake_fingerprint, wake_response_json,
                              wake_disposition_reason
                       FROM async_delegations WHERE delegation_id=?""",
                    (delegation_id,),
                ).fetchone()
                if raced and raced[0] == "completed":
                    if str(raced[1] or "") != fingerprint:
                        return DurableWakeClaim(
                            state="uncertain",
                            reason="durable wake fingerprint mismatch",
                        )
                    try:
                        response = json.loads(raced[2] or "")
                    except (TypeError, ValueError):
                        response = None
                    if isinstance(response, dict):
                        return DurableWakeClaim(
                            state="completed",
                            response=response,
                        )
                if raced and raced[0] == "running":
                    return DurableWakeClaim(
                        state="in_progress",
                        reason="durable wake execution is still running",
                    )
                return DurableWakeClaim(
                    state="uncertain",
                    reason=str(
                        (raced[3] if raced else "")
                        or "durable wake outcome is uncertain"
                    ),
                )
            return DurableWakeClaim(state="uncertain", reason=reason)


def complete_durable_wake_execution(
    *,
    delegation_id: str,
    idempotency_key: str,
    claim_id: str,
    response: Dict[str, Any],
    store: EventDeliveryStore,
) -> bool:
    """Commit the exact canonical response for the owner of a wake claim."""

    delegation_id = str(delegation_id or "").strip()
    fingerprint = str(idempotency_key or "").strip()
    claim_id = str(claim_id or "").strip()
    if not delegation_id or not fingerprint or not claim_id:
        return False
    encoded, _decoded = _canonical_wake_response(response)
    _verify_event_delivery_store(store)
    now = time.time()
    pid = int(__import__("os").getpid())
    started_at = _wake_owner_started_at(pid)
    with _delivery_home_scope(store.hermes_home):
        with _DB_LOCK, _transaction() as conn:
            completed = conn.execute(
                """UPDATE async_delegations
                   SET wake_state='completed', wake_response_json=?,
                       wake_disposition_reason=NULL, updated_at=?
                   WHERE delegation_id=? AND wake_state='running'
                     AND wake_fingerprint=? AND wake_claim_id=?
                     AND wake_owner_pid=?
                     AND wake_owner_instance=?
                     AND (
                         wake_owner_started_at=?
                         OR (wake_owner_started_at IS NULL AND ? IS NULL)
                     )""",
                (
                    encoded,
                    now,
                    delegation_id,
                    fingerprint,
                    claim_id,
                    pid,
                    _WAKE_PROCESS_INSTANCE_ID,
                    started_at,
                    started_at,
                ),
            ).rowcount == 1
            if not completed:
                row = conn.execute(
                    """SELECT wake_state, wake_fingerprint, wake_response_json
                       FROM async_delegations WHERE delegation_id=?""",
                    (delegation_id,),
                ).fetchone()
                completed = bool(
                    row
                    and row[0] == "completed"
                    and str(row[1] or "") == fingerprint
                    and str(row[2] or "") == encoded
                )
    if completed:
        with _ACTIVE_WAKE_CLAIMS_LOCK:
            _ACTIVE_WAKE_CLAIMS.discard(claim_id)
    else:
        # A failed owner CAS means this process no longer owns a path to
        # completion.  Do not leave the same-process liveness registry stuck.
        with _ACTIVE_WAKE_CLAIMS_LOCK:
            _ACTIVE_WAKE_CLAIMS.discard(claim_id)
    return completed


def release_durable_wake_execution(
    *,
    delegation_id: str,
    idempotency_key: str,
    claim_id: str,
    store: EventDeliveryStore,
) -> bool:
    """Return an unused owner claim to ``not_started``.

    This seam is intentionally narrower than abandonment: callers may use it
    only when model/tool execution provably never began (for example, an API
    capacity gate rejected the request after token admission).  The strict
    owner CAS prevents a stale caller from reopening another execution.
    """

    delegation_id = str(delegation_id or "").strip()
    fingerprint = str(idempotency_key or "").strip()
    claim_id = str(claim_id or "").strip()
    if not delegation_id or not fingerprint or not claim_id:
        with _ACTIVE_WAKE_CLAIMS_LOCK:
            _ACTIVE_WAKE_CLAIMS.discard(claim_id)
        return False
    try:
        _verify_event_delivery_store(store)
        now = time.time()
        pid = int(__import__("os").getpid())
        started_at = _wake_owner_started_at(pid)
        with _delivery_home_scope(store.hermes_home):
            with _DB_LOCK, _transaction() as conn:
                return (
                    conn.execute(
                        """UPDATE async_delegations
                           SET wake_fingerprint=NULL,
                               wake_state='not_started',
                               wake_claim_id=NULL,
                               wake_owner_pid=NULL,
                               wake_owner_started_at=NULL,
                               wake_owner_instance=NULL,
                               wake_claimed_at=NULL,
                               wake_response_json=NULL,
                               wake_disposition_reason=NULL,
                               updated_at=?
                           WHERE delegation_id=?
                             AND wake_state='running'
                             AND wake_fingerprint=?
                             AND wake_claim_id=?
                             AND wake_owner_pid=?
                             AND wake_owner_instance=?
                             AND (
                                 wake_owner_started_at=?
                                 OR (
                                     wake_owner_started_at IS NULL
                                     AND ? IS NULL
                                 )
                             )""",
                        (
                            now,
                            delegation_id,
                            fingerprint,
                            claim_id,
                            pid,
                            _WAKE_PROCESS_INSTANCE_ID,
                            started_at,
                            started_at,
                        ),
                    ).rowcount
                    == 1
                )
    finally:
        with _ACTIVE_WAKE_CLAIMS_LOCK:
            _ACTIVE_WAKE_CLAIMS.discard(claim_id)


def abandon_durable_wake_execution(
    *,
    delegation_id: str,
    idempotency_key: str,
    claim_id: str,
    reason: str,
    store: EventDeliveryStore,
) -> bool:
    """Owner-CAS a failed/cancelled wake to terminal explicit uncertainty."""

    delegation_id = str(delegation_id or "").strip()
    fingerprint = str(idempotency_key or "").strip()
    claim_id = str(claim_id or "").strip()
    reason = str(reason or "").strip()
    if not reason:
        reason = (
            "durable wake execution ended before its response was committed; "
            "outcome may include effects"
        )
    if not delegation_id or not fingerprint or not claim_id:
        with _ACTIVE_WAKE_CLAIMS_LOCK:
            _ACTIVE_WAKE_CLAIMS.discard(claim_id)
        return False
    try:
        _verify_event_delivery_store(store)
        now = time.time()
        pid = int(__import__("os").getpid())
        started_at = _wake_owner_started_at(pid)
        with _delivery_home_scope(store.hermes_home):
            with _DB_LOCK, _transaction() as conn:
                abandoned = conn.execute(
                    """UPDATE async_delegations
                       SET wake_state='uncertain',
                           wake_disposition_reason=?, updated_at=?
                       WHERE delegation_id=? AND wake_state='running'
                         AND wake_fingerprint=? AND wake_claim_id=?
                         AND wake_owner_pid=?
                         AND wake_owner_instance=?
                         AND (
                             wake_owner_started_at=?
                             OR (
                                 wake_owner_started_at IS NULL
                                 AND ? IS NULL
                             )
                         )""",
                    (
                        reason,
                        now,
                        delegation_id,
                        fingerprint,
                        claim_id,
                        pid,
                        _WAKE_PROCESS_INSTANCE_ID,
                        started_at,
                        started_at,
                    ),
                ).rowcount == 1
                if not abandoned:
                    row = conn.execute(
                        """SELECT wake_state, wake_fingerprint
                           FROM async_delegations WHERE delegation_id=?""",
                        (delegation_id,),
                    ).fetchone()
                    abandoned = bool(
                        row
                        and row[0] == "uncertain"
                        and str(row[1] or "") == fingerprint
                    )
                return abandoned
    finally:
        with _ACTIVE_WAKE_CLAIMS_LOCK:
            _ACTIVE_WAKE_CLAIMS.discard(claim_id)


def _note_delivery_attempt(delegation_id: str) -> None:
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET delivery_attempts=delivery_attempts+1, updated_at=? WHERE delegation_id=?",
            (time.time(), delegation_id),
        )


def recover_abandoned_delegations() -> int:
    """Classify records whose owning process disappeared as outcome unknown."""
    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        return 0
    current_store = _current_event_delivery_store()
    now = time.time()
    recovered = 0
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT delegation_id, origin_session, origin_ui_session_id,
                      parent_session_id, dispatched_at, owner_pid,
                      owner_started_at, task_json, origin_session_id
               FROM async_delegations WHERE state IN ('running','finalizing')"""
        ).fetchall()
        for row in rows:
            (delegation_id, session_key, origin_ui, parent_id, dispatched_at,
             pid, started, task_json, origin_session_id) = row
            live = False
            if pid:
                live = _pid_exists(int(pid))
                if live and started is not None:
                    live = get_process_start_time(int(pid)) == int(started)
            if live:
                continue
            try:
                task = json.loads(task_json or "{}")
                if not isinstance(task, dict):
                    raise ValueError("durable task payload must be a JSON object")
            except Exception:
                logger.error(
                    "Async delegation %s has malformed durable task JSON; "
                    "quarantining only this record",
                    delegation_id,
                    exc_info=True,
                )
                error = (
                    "Delegation owner exited with malformed durable task "
                    "metadata; recovery quarantined this record."
                )
                conn.execute(
                    """UPDATE async_delegations SET state='unknown',
                       completed_at=?, updated_at=?, result_json=?,
                       delivery_state='dropped', delivery_claim=NULL,
                       delivery_claimed_at=NULL
                       WHERE delegation_id=?""",
                    (
                        now,
                        now,
                        json.dumps(
                            {
                                "status": "unknown",
                                "summary": None,
                                "error": error,
                            }
                        ),
                        delegation_id,
                    ),
                )
                recovered += 1
                continue
            persisted_generation = str(
                task.get(_EVENT_DELIVERY_PROFILE_GENERATION_KEY) or ""
            )
            if persisted_generation != current_store.profile_generation:
                error = (
                    "Delegation owner exited, and its durable profile "
                    "generation is missing or no longer matches this profile."
                )
                logger.error(
                    "Async delegation %s profile generation mismatch; "
                    "refusing abandoned recovery",
                    delegation_id,
                )
                conn.execute(
                    """UPDATE async_delegations SET state='unknown',
                       completed_at=?, updated_at=?, result_json=?,
                       delivery_state='dropped', delivery_claim=NULL,
                       delivery_claimed_at=NULL,
                       delivery_disposition_reason=?
                       WHERE delegation_id=?""",
                    (
                        now,
                        now,
                        json.dumps(
                            {
                                "status": "unknown",
                                "summary": None,
                                "error": error,
                            }
                        ),
                        error,
                        delegation_id,
                    ),
                )
                recovered += 1
                continue
            try:
                recovered_runtime_effect = (
                    _normalize_optional_runtime_effect(
                        task.get("runtime_effect")
                    )
                )
                recovered_api_execution_context = (
                    _normalize_optional_api_execution_context(
                        task.get("api_execution_context")
                    )
                )
                recovered_model = _normalize_optional_durable_model(
                    task.get("model"),
                    field="recovered async dispatch.model",
                )
            except Exception:
                logger.error(
                    "Async delegation %s has malformed durable host "
                    "execution metadata; refusing recovered delivery",
                    delegation_id,
                    exc_info=True,
                )
                scrubbed_task = dict(task)
                scrubbed_task.pop("model", None)
                scrubbed_task.pop("runtime_effect", None)
                scrubbed_task.pop("api_execution_context", None)
                conn.execute(
                    """UPDATE async_delegations SET state='unknown',
                       completed_at=?, updated_at=?, delivery_state='dropped',
                       task_json=?
                       WHERE delegation_id=?""",
                    (
                        now,
                        now,
                        json.dumps(scrubbed_task),
                        delegation_id,
                    ),
                )
                recovered += 1
                continue
            event = {
                "type": "async_delegation", "delegation_id": delegation_id,
                "session_key": session_key, "origin_ui_session_id": origin_ui,
                # Restore the durable wake target so completions recovered
                # after a restart remain routable to api_server sessions.
                "origin_session_id": origin_session_id or "",
                "parent_session_id": parent_id, "goal": task.get("goal", ""),
                "goals": task.get("goals"), "context": task.get("context"),
                "toolsets": task.get("toolsets"), "role": task.get("role"),
                "model": recovered_model,
                "is_batch": bool(task.get("is_batch")),
                "runtime_effect": recovered_runtime_effect,
                "api_execution_context": recovered_api_execution_context,
                _EVENT_DELIVERY_PROFILE_GENERATION_KEY: (
                    persisted_generation
                ),
                "status": "unknown", "summary": None,
                "error": "Delegation owner exited before recording a terminal result; outcome unknown.",
                "dispatched_at": dispatched_at, "completed_at": now,
            }
            result = {"status": "unknown", "summary": None, "error": event["error"]}
            conn.execute(
                """UPDATE async_delegations SET state='unknown', completed_at=?,
                   updated_at=?, event_json=?, result_json=?, delivery_state='pending'
                   WHERE delegation_id=?""",
                (now, now, json.dumps(event), json.dumps(result), delegation_id),
            )
            recovered += 1
    return recovered


def live_foreign_delegation_ids(
    *,
    hermes_home: str | Path | None = None,
    profile: str | None = None,
    delivery_store: EventDeliveryStore | None = None,
) -> set[str]:
    """Return durable running rows still owned by another live process.

    This is the narrow signal used by long-lived hosts to decide whether a
    bounded rolling-restart rescan is necessary.  Current-process delegations
    are intentionally excluded: their normal finalizer already publishes the
    live completion, and rescanning them would risk a duplicate queue carrier.
    """

    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        return set()

    store = delivery_store or resolve_event_delivery_store(
        hermes_home=hermes_home,
        profile=profile,
    )
    _verify_event_delivery_store(store)
    current_pid = __import__("os").getpid()
    try:
        current_started = get_process_start_time(current_pid)
    except Exception:
        current_started = None

    with _delivery_home_scope(store.hermes_home):
        with _DB_LOCK, _transaction() as conn:
            rows = conn.execute(
                """SELECT delegation_id, owner_pid, owner_started_at
                   FROM async_delegations
                   WHERE state IN ('running','finalizing')"""
            ).fetchall()

    live_ids: set[str] = set()
    for delegation_id, pid, started in rows:
        if not pid:
            continue
        pid = int(pid)
        if pid == current_pid and (
            started is None
            or current_started is None
            or int(started) == int(current_started)
        ):
            continue
        live = _pid_exists(pid)
        if live and started is not None:
            live = get_process_start_time(pid) == int(started)
        if live:
            live_ids.add(str(delegation_id))
    return live_ids


def restore_undelivered_completions(
    target_queue,
    *,
    hermes_home: str | Path | None = None,
    profile: str | None = None,
    delivery_store: EventDeliveryStore | None = None,
    event_filter: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> int:
    """Enqueue durable pending completions as fresh turns after process start.

    Every restored event is stamped ``restored=True`` (in-memory only — the
    stamp is added after the durable payload is deserialized and is never
    persisted). Restored events originate from a *previous* process, so no
    consumer in THIS process implicitly owns them: drain paths that run
    without an ownership filter (the legacy single-session behavior) must
    leave them queued for a consumer that can positively prove ownership,
    otherwise a brand-new session adopts a dead session's delegation
    results seconds after boot (#64484).

    ``hermes_home``/``profile`` are an explicit, validated store target for
    multi-profile hosts.  ``event_filter`` runs after the SQLite transaction is
    closed, so an ownership predicate may safely consult SessionDB (including
    compression lineage) without nesting another connection under this
    module's delivery lock.
    """
    store = delivery_store or resolve_event_delivery_store(
        hermes_home=hermes_home,
        profile=profile,
    )
    _verify_event_delivery_store(store)
    candidates: List[Dict[str, Any]] = []
    with _delivery_home_scope(store.hermes_home):
        recovered_spools = _recover_finalize_spool(store)
        if recovered_spools:
            logger.info(
                "Recovered %d exact async finalization spool payload(s)",
                recovered_spools,
            )
        recover_abandoned_delegations()
        with _DB_LOCK, _transaction() as conn:
            rows = conn.execute(
                """SELECT delegation_id, event_json, result_json
                   FROM async_delegations
                   WHERE state != 'running' AND delivery_state='pending'
                     AND event_json IS NOT NULL
                   ORDER BY completed_at, delegation_id"""
            ).fetchall()
            for _delegation_id, payload, result_payload in rows:
                try:
                    evt = json.loads(payload)
                    if not isinstance(evt, dict):
                        raise ValueError(
                            "durable completion payload must be a JSON object"
                        )
                    restored_result = json.loads(result_payload or "{}")
                    if not isinstance(restored_result, dict):
                        raise ValueError(
                            "durable result payload must be a JSON object"
                        )
                except Exception:
                    logger.error(
                        "Async delegation %s has malformed persisted event JSON; "
                        "quarantining only this record",
                        _delegation_id,
                        exc_info=True,
                    )
                    conn.execute(
                        """UPDATE async_delegations SET delivery_state='dropped',
                           updated_at=?, delivery_claim=NULL,
                           delivery_claimed_at=NULL
                           WHERE delegation_id=?""",
                        (time.time(), _delegation_id),
                    )
                    continue
                scrubbed_evt = _scrub_terminal_model_metadata(evt)
                scrubbed_result = _scrub_terminal_model_metadata(
                    restored_result
                )
                if scrubbed_evt != evt or scrubbed_result != restored_result:
                    evt = scrubbed_evt
                    conn.execute(
                        """UPDATE async_delegations
                           SET event_json=?, result_json=?, updated_at=?
                           WHERE delegation_id=?""",
                        (
                            json.dumps(scrubbed_evt),
                            json.dumps(scrubbed_result),
                            time.time(),
                            _delegation_id,
                        ),
                    )
                persisted_generation = str(
                    evt.get(_EVENT_DELIVERY_PROFILE_GENERATION_KEY) or ""
                )
                if persisted_generation != store.profile_generation:
                    reason = (
                        "durable completion profile generation is missing or "
                        "does not match the current profile directory"
                    )
                    logger.error(
                        "Async delegation %s %s; refusing restore",
                        _delegation_id,
                        reason,
                    )
                    conn.execute(
                        """UPDATE async_delegations SET delivery_state='dropped',
                           updated_at=?, delivery_claim=NULL,
                           delivery_claimed_at=NULL,
                           delivery_disposition_reason=?
                           WHERE delegation_id=?""",
                        (time.time(), reason, _delegation_id),
                    )
                    continue
                try:
                    evt["runtime_effect"] = (
                        _normalize_optional_runtime_effect(
                            evt.get("runtime_effect")
                        )
                    )
                except Exception:
                    logger.error(
                        "Async delegation %s has malformed persisted completion "
                        "runtime effect; refusing restore",
                        _delegation_id,
                        exc_info=True,
                    )
                    conn.execute(
                        """UPDATE async_delegations SET delivery_state='dropped',
                           updated_at=? WHERE delegation_id=?""",
                        (time.time(), _delegation_id),
                    )
                    continue
                evt["restored"] = True
                # This assignment deliberately overwrites any same-named value
                # deserialized from event_json.  Only this process-local token
                # can resolve through get_event_delivery_store().
                _stamp_event_delivery_store(evt, store)
                candidates.append(evt)

    restored = 0
    for evt in candidates:
        if event_filter is not None:
            try:
                if not event_filter(evt):
                    continue
            except Exception:
                logger.warning(
                    "Async delegation %s restore ownership filter failed; "
                    "leaving the durable row pending",
                    evt.get("delegation_id", ""),
                    exc_info=True,
                )
                continue
        target_queue.put(evt)
        restored += 1
    return restored


def mark_completion_delivered(delegation_id: str) -> bool:
    """Atomically acknowledge successful injection of a durable completion."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered', delivered_at=?, updated_at=?
               WHERE delegation_id=? AND delivery_state!='delivered'""",
            (now, now, delegation_id),
        )
        return cur.rowcount == 1


def claim_completion_delivery(
    delegation_id: str,
    claim_id: str,
    *,
    require_row: bool = False,
) -> bool:
    """Claim one pending completion across competing consumers/processes.

    ``require_row=False`` preserves admission of legacy in-memory events that
    predate durable dispatch.  Trusted event-scoped callers pass
    ``require_row=True``: a store stamp asserts an authoritative DB exists, so a
    missing row is ownership failure rather than legacy success.
    """
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            return not require_row
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=?, delivery_claimed_at=?,
                      delivery_attempts=delivery_attempts+1, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND (delivery_claim IS NULL OR delivery_claimed_at IS NULL
                      OR delivery_claimed_at < ?)""",
            (
                claim_id,
                now,
                now,
                delegation_id,
                now - _DELIVERY_CLAIM_LEASE_SECONDS,
            ),
        )
        claimed = cur.rowcount == 1
    if claimed:
        _forget_lost_delivery_claim(delegation_id, claim_id)
    return claimed


def claim_event_delivery(evt: Dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable delegation event; non-durable events need no token."""
    if evt.get("type") != "async_delegation":
        return ""
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{__import__('os').getpid()}:{uuid.uuid4().hex}"
    try:
        with _event_delivery_scope(evt):
            claimed = claim_completion_delivery(
                delegation_id,
                claim_id,
                require_row=_EVENT_DELIVERY_STORE_KEY in evt,
            )
    except ValueError:
        logger.error(
            "Refusing async delegation %s claim with an untrusted "
            "delivery-store stamp",
            delegation_id,
        )
        return None
    return claim_id if claimed else None


def event_delivery_retry_delay(
    evt: Dict[str, Any],
    *,
    minimum_seconds: float = 0.25,
) -> Optional[float]:
    """Return when a pending durable event can safely regain a queue carrier.

    A completion may be restored while another process still owns a fresh
    delivery lease, or a local delivery turn may release its claim after the
    only in-memory queue item has already been removed.  Consumers use this
    query to schedule one bounded retry instead of either losing the carrier
    until restart or hot-spinning the shared queue.

    ``None`` means the authoritative row is missing or terminal and therefore
    must not be re-enqueued.  A forged/stale store stamp also fails closed.
    Storage errors intentionally propagate so the scheduler can make a
    conservative delayed retry rather than silently discarding the event.
    """

    if evt.get("type") != "async_delegation":
        return None
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        return None
    minimum = max(0.01, float(minimum_seconds))
    try:
        with _event_delivery_scope(evt):
            with _DB_LOCK, _transaction() as conn:
                row = conn.execute(
                    """SELECT delivery_state, delivery_claim,
                              delivery_claimed_at
                       FROM async_delegations WHERE delegation_id=?""",
                    (delegation_id,),
                ).fetchone()
    except ValueError:
        logger.error(
            "Refusing async delegation %s retry with an untrusted "
            "delivery-store stamp",
            delegation_id,
        )
        return None

    if row is None or row[0] != "pending":
        return None
    claim_id, claimed_at = row[1], row[2]
    if not claim_id or not isinstance(claimed_at, (int, float)):
        return minimum

    now = time.time()
    # Cap future-clock skew at one lease rather than allowing a malformed
    # timestamp to strand the completion for an unbounded interval.
    remaining = min(
        _DELIVERY_CLAIM_LEASE_SECONDS,
        max(
            0.0,
            float(claimed_at) + _DELIVERY_CLAIM_LEASE_SECONDS - now,
        ),
    )
    if remaining <= 0:
        return minimum
    return max(
        minimum,
        remaining + _DELIVERY_RETRY_EPSILON_SECONDS,
    )


def renew_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Renew one live delivery claim without consuming another attempt."""

    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claimed_at=?, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def renew_event_delivery(evt: Dict[str, Any], claim_id: str) -> bool:
    """Renew a durable event claim; non-durable events require no renewal."""

    if not claim_id or evt.get("type") != "async_delegation":
        return True
    delegation_id = str(evt.get("delegation_id") or "")
    try:
        with _event_delivery_scope(evt):
            return renew_completion_delivery(
                delegation_id,
                claim_id,
            )
    except ValueError:
        logger.error(
            "Refusing async delegation %s renewal with an untrusted "
            "delivery-store stamp",
            delegation_id,
        )
        return False


def begin_event_delivery_renewal(
    evt: Dict[str, Any],
    claim_id: str,
    *,
    interval_seconds: float = 60.0,
) -> DeliveryClaimRenewal:
    """Keep a claim live and expose any renewal failure as ownership loss."""

    if not claim_id or evt.get("type") != "async_delegation":
        return DeliveryClaimRenewal()
    delegation_id = str(evt.get("delegation_id") or "")
    stopped = threading.Event()
    handle = DeliveryClaimRenewal(
        delegation_id=delegation_id,
        claim_id=claim_id,
        stopped=stopped,
    )

    def _renew() -> None:
        while not stopped.wait(max(0.01, float(interval_seconds))):
            try:
                if not renew_event_delivery(evt, claim_id):
                    if stopped.is_set():
                        return
                    handle.mark_ownership_lost()
                    logger.warning(
                        "Async delegation %s delivery claim is no longer owned",
                        delegation_id,
                    )
                    return
            except Exception:
                if stopped.is_set():
                    return
                handle.mark_ownership_lost()
                logger.warning(
                    "Async delegation %s delivery-claim renewal failed; "
                    "ownership is lost",
                    delegation_id,
                    exc_info=True,
                )
                return

    thread = threading.Thread(
        target=_renew,
        name="async-delegation-delivery-renewal",
        daemon=True,
    )
    handle.attach_thread(thread)
    try:
        thread.start()
    except Exception:
        handle.mark_ownership_lost()
        logger.warning(
            "Async delegation %s delivery-claim renewal could not start; "
            "ownership is lost",
            delegation_id,
            exc_info=True,
        )
    return handle


def release_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Release a failed delivery claim so another consumer may retry.

    Attempts are counted at claim time, so a row that keeps being claimed and
    released has burned real delivery attempts. Once the budget is exhausted
    the row converges to a terminal ``dropped`` state instead of returning to
    ``pending`` — otherwise an undeliverable completion replays on every
    gateway restart forever (restore_undelivered_completions only restores
    pending rows).
    """
    now = time.time()
    try:
        with _DB_LOCK, _transaction() as conn:
            capped = conn.execute(
                """UPDATE async_delegations SET delivery_state='dropped',
                          delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
                   WHERE delegation_id=? AND delivery_state='pending'
                     AND delivery_claim=? AND delivery_attempts>=?""",
                (now, delegation_id, claim_id, _MAX_DELIVERY_ATTEMPTS),
            )
            if capped.rowcount == 1:
                logger.warning(
                    "Async delegation %s exhausted its %d delivery attempts; "
                    "marking terminally dropped (result remains queryable).",
                    delegation_id, _MAX_DELIVERY_ATTEMPTS,
                )
                return True
            cur = conn.execute(
                """UPDATE async_delegations SET delivery_claim=NULL,
                          delivery_claimed_at=NULL, updated_at=?
                   WHERE delegation_id=? AND delivery_state='pending'
                     AND delivery_claim=?""",
                (now, delegation_id, claim_id),
            )
            return cur.rowcount == 1
    finally:
        _forget_lost_delivery_claim(delegation_id, claim_id)


def release_completion_delivery_without_attempt(
    delegation_id: str,
    claim_id: str,
) -> bool:
    """Release a provably transient claim without consuming retry budget.

    This is deliberately separate from :func:`release_completion_delivery`.
    It is valid only when the downstream boundary explicitly proves either
    that execution never began or that another live durable owner is already
    executing.  The owner CAS both clears the lease and reverses exactly the
    claim-time increment; it never applies the bounded-attempt drop policy.
    """

    now = time.time()
    try:
        with _DB_LOCK, _transaction() as conn:
            cur = conn.execute(
                """UPDATE async_delegations
                   SET delivery_claim=NULL,
                       delivery_claimed_at=NULL,
                       delivery_attempts=CASE
                           WHEN delivery_attempts > 0
                           THEN delivery_attempts - 1
                           ELSE 0
                       END,
                       updated_at=?
                   WHERE delegation_id=?
                     AND delivery_state='pending'
                     AND delivery_claim=?""",
                (now, delegation_id, claim_id),
            )
            return cur.rowcount == 1
    finally:
        _forget_lost_delivery_claim(delegation_id, claim_id)


def drop_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Terminally drop a claimed completion that can never be delivered.

    Used when the delivery target is permanently gone — the spawning session
    ended at an explicit user boundary (/new, reset) rather than a compression
    rotation. Marking the row ``dropped`` (not ``delivered``) keeps the ack
    honest, and (not ``pending``) keeps restart recovery from replaying a
    completion that will be fail-closed dropped again every time.
    """
    now = time.time()
    try:
        with _DB_LOCK, _transaction() as conn:
            cur = conn.execute(
                """UPDATE async_delegations SET delivery_state='dropped',
                          updated_at=?, delivery_claim=NULL,
                          delivery_claimed_at=NULL
                   WHERE delegation_id=? AND delivery_state='pending'
                     AND delivery_claim=?""",
                (now, delegation_id, claim_id),
            )
            return cur.rowcount == 1
    finally:
        _forget_lost_delivery_claim(delegation_id, claim_id)


def complete_completion_delivery(
    delegation_id: str,
    claim_id: str,
    *,
    require_row: bool = False,
) -> bool:
    """Acknowledge acceptance for the consumer holding this claim.

    ``require_row`` mirrors :func:`claim_completion_delivery`: event-scoped
    consumers fail closed when their trusted store no longer contains the row,
    while direct legacy callers retain the historical no-row success.
    """
    if _delivery_claim_was_lost(delegation_id, claim_id):
        logger.warning(
            "Refusing to acknowledge async delegation %s after delivery "
            "claim ownership was lost",
            delegation_id,
        )
        release_completion_delivery(delegation_id, claim_id)
        return False
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered',
                      delivered_at=?, updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, now, delegation_id, claim_id),
        )
        completed = cur.rowcount == 1
        if not completed:
            # claim_completion_delivery() deliberately admits legacy
            # in-memory events that predate durable dispatch.  No row means
            # there is no authoritative state to CAS; an existing row with a
            # different claim remains a real ownership loss.
            completed = (
                not require_row
                and conn.execute(
                    """SELECT 1 FROM async_delegations
                       WHERE delegation_id=?""",
                    (delegation_id,),
                ).fetchone()
                is None
            )
    _forget_lost_delivery_claim(delegation_id, claim_id)
    return completed


def complete_event_delivery(evt: Dict[str, Any], claim_id: str) -> bool:
    if claim_id and evt.get("type") == "async_delegation":
        delegation_id = str(evt.get("delegation_id") or "")
        try:
            with _event_delivery_scope(evt):
                return complete_completion_delivery(
                    delegation_id,
                    claim_id,
                    require_row=_EVENT_DELIVERY_STORE_KEY in evt,
                )
        except ValueError:
            logger.error(
                "Refusing async delegation %s completion with an untrusted "
                "delivery-store stamp",
                delegation_id,
            )
            return False
    return True


def release_event_delivery(evt: Dict[str, Any], claim_id: str) -> bool:
    if claim_id and evt.get("type") == "async_delegation":
        delegation_id = str(evt.get("delegation_id") or "")
        try:
            with _event_delivery_scope(evt):
                return release_completion_delivery(delegation_id, claim_id)
        except ValueError:
            logger.error(
                "Refusing async delegation %s release with an untrusted "
                "delivery-store stamp",
                delegation_id,
            )
            return False
    return True


def release_event_delivery_without_attempt(
    evt: Dict[str, Any],
    claim_id: str,
) -> bool:
    """Release a trusted durable event without burning one delivery attempt."""

    if claim_id and evt.get("type") == "async_delegation":
        delegation_id = str(evt.get("delegation_id") or "")
        try:
            with _event_delivery_scope(evt):
                return release_completion_delivery_without_attempt(
                    delegation_id,
                    claim_id,
                )
        except ValueError:
            logger.error(
                "Refusing async delegation %s no-attempt release with an "
                "untrusted delivery-store stamp",
                delegation_id,
            )
            return False
    return True


def drop_event_delivery(evt: Dict[str, Any], claim_id: str) -> bool:
    """Terminally drop a claimed event in its authoritative durable store."""

    if claim_id and evt.get("type") == "async_delegation":
        delegation_id = str(evt.get("delegation_id") or "")
        try:
            with _event_delivery_scope(evt):
                return drop_completion_delivery(delegation_id, claim_id)
        except ValueError:
            logger.error(
                "Refusing async delegation %s drop with an untrusted "
                "delivery-store stamp",
                delegation_id,
            )
            return False
    return True


def get_durable_delegation(
    delegation_id: str,
    *,
    store: EventDeliveryStore | None = None,
) -> Optional[Dict[str, Any]]:
    """Read one durable delegation from an optional exact profile store.

    API callers in a multiplexed runner must pass the already-authorized
    :class:`EventDeliveryStore`; falling back to ambient ``HERMES_HOME`` is
    retained only for legacy single-profile/CLI callers.
    """

    if store is not None:
        _verify_event_delivery_store(store)
    with _optional_delivery_store_scope(store):
        with _DB_LOCK, _transaction() as conn:
            row = conn.execute(
                """SELECT origin_session, state, dispatched_at, completed_at,
                          result_json, delivery_state, delivery_attempts,
                          origin_session_id, event_json,
                          delivery_disposition_reason, wake_state,
                          wake_disposition_reason
                   FROM async_delegations WHERE delegation_id=?""",
                (delegation_id,),
            ).fetchone()
    if row is None:
        return None
    event = None
    result = None
    if row[8]:
        try:
            parsed_event = json.loads(row[8])
            event = _scrub_terminal_model_metadata(parsed_event)
        except (TypeError, ValueError):
            # A malformed durable event is deliberately quarantined rather than
            # deleted.  Status inspection must remain available for that row.
            event = None
    if row[4]:
        try:
            parsed_result = json.loads(row[4])
            result = _scrub_terminal_model_metadata(parsed_result)
        except (TypeError, ValueError):
            result = None
    return {
        "delegation_id": delegation_id, "origin_session": row[0], "state": row[1],
        "dispatched_at": row[2], "completed_at": row[3],
        "result": result,
        "delivery_state": row[5], "delivery_attempts": row[6],
        "origin_session_id": row[7] or "",
        "event": event,
        "event_json": json.dumps(event) if event is not None else None,
        "delivery_disposition_reason": row[9] or "",
        "wake_state": row[10] or "not_started",
        "wake_disposition_reason": row[11] or "",
    }


def _get_executor(max_workers: int) -> ThreadPoolExecutor:
    """Lazily create (or grow) the shared daemon executor.

    We never shrink — ThreadPoolExecutor can't resize — but if the configured
    cap grows between calls we rebuild a larger pool. Existing in-flight
    futures keep running on the old pool until it's garbage collected.
    """
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is None or max_workers > _executor_max_workers:
            # Daemon threads: thread_name_prefix aids debugging in stack dumps.
            _executor = _DaemonThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="async-delegate",
            )
            _executor_max_workers = max_workers
        return _executor


def active_count() -> int:
    """Number of async delegation UNITS currently running.

    A unit is one dispatch: a single subagent OR a whole fan-out batch. A batch
    counts as ONE here because it occupies one async-pool slot (the capacity
    semantics ``dispatch_async_delegation_batch`` relies on). For the count of
    actual concurrent child subagents (batch expanded), use
    ``active_task_count()``.
    """
    with _records_lock:
        return sum(
            1 for r in _records.values()
            if r.get("status")
            in {"running", "stalling", "finalizing", "finalize_failed"}
        )


def active_for_session(origin_ui_session_id: str) -> int:
    """Number of live async delegations owned by one UI session."""
    if not origin_ui_session_id:
        return 0
    with _records_lock:
        return sum(
            1
            for r in _records.values()
            if r.get("status") in {"running", "stalling", "finalizing"}
            and str(r.get("origin_ui_session_id") or "")
            == origin_ui_session_id
        )


def active_task_count() -> int:
    """Number of async delegation TASKS (child subagents) currently running.

    Unlike ``active_count()`` (units/slots), this expands a batch to its child
    count: a running batch of N tasks contributes N, a single subagent
    contributes 1. This is the truthful "how many subagents are actually
    working right now" figure for observability, where a 3-task batch shown as
    "1" undercounts real concurrent work. Falls back to counting a batch as 1
    if its goal list is missing.
    """
    with _records_lock:
        total = 0
        for r in _records.values():
            if r.get("status") not in {"running", "finalizing"}:
                continue
            if r.get("is_batch"):
                goals = r.get("goals")
                total += len(goals) if isinstance(goals, (list, tuple)) and goals else 1
            else:
                total += 1
        return total


def _matches_session_selectors(
    record: Dict[str, Any],
    *,
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
) -> bool:
    return (
        (origin_ui_session_id and str(record.get("origin_ui_session_id") or "") == origin_ui_session_id)
        or (session_key and str(record.get("session_key") or "") == session_key)
        or (parent_session_id and str(record.get("parent_session_id") or "") == parent_session_id)
    )


def has_live_for_session(
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
) -> bool:
    """Whether a session still owns any live async delegation.

    Live = running / stalling / finalizing — the same states the reapers'
    keepalive treats as active work.
    """
    if not session_key and not origin_ui_session_id and not parent_session_id:
        return False
    with _records_lock:
        return any(
            r.get("status") in {"running", "stalling", "finalizing"}
            and _matches_session_selectors(
                r,
                session_key=session_key,
                origin_ui_session_id=origin_ui_session_id,
                parent_session_id=parent_session_id,
            )
            for r in _records.values()
        )


def _new_delegation_id() -> str:
    # Full UUID4 entropy.  The previous eight-hex suffix was only 32 bits,
    # making a collision plausible in a long-lived/high-volume deployment.
    return f"deleg_{uuid.uuid4().hex}"


def _is_strong_delegation_id(value: object) -> bool:
    """Require the canonical 128-bit identifier shape for new dispatches."""

    return bool(_DELEGATION_ID_RE.fullmatch(str(value or "").strip()))


def _prune_completed_locked() -> None:
    """Drop the oldest completed records beyond the retention cap.

    Caller must hold ``_records_lock``.
    """
    completed = [
        (rid, r)
        for rid, r in _records.items()
        if r.get("status")
        not in {"running", "stalling", "finalizing", "finalize_failed"}
    ]
    if len(completed) <= _MAX_RETAINED_COMPLETED:
        return
    # Oldest-first by completion time (fall back to dispatch time).
    completed.sort(key=lambda kv: kv[1].get("completed_at") or kv[1].get("dispatched_at") or 0)
    for rid, _ in completed[: len(completed) - _MAX_RETAINED_COMPLETED]:
        _records.pop(rid, None)


def _current_origin_session_id() -> str:
    """Raw session id of the ORIGINATING api_server request, or ``""``.

    The obvious source — ``HERMES_SESSION_ID`` via ``get_session_env`` — is
    NOT safe to read at dispatch time: constructing a child agent
    (``agent/agent_init.py``) calls ``set_current_session_id(child.session_id)``,
    clobbering that ContextVar *and* ``os.environ`` with the subagent's
    internal ``{timestamp}_{uuid}`` id moments before the dispatch code reads
    it, so the completion wake would self-post into the subagent's own
    (unread) session instead of the spawner's.

    The request-scoped ``HERMES_SESSION_CHAT_ID`` binding survives child
    construction: ``_bind_api_server_session`` binds ``chat_id`` to the raw
    ``X-Hermes-Session-Id``, and its only writer is ``set_session_vars`` —
    ``set_current_session_id`` never touches it. Gate on the platform: on
    push platforms ``chat_id`` is a chat, not a session, so yield ``""``
    there.
    """
    try:
        from gateway.session_context import get_session_env

        if get_session_env("HERMES_SESSION_PLATFORM", "") != "api_server":
            return ""
        return get_session_env("HERMES_SESSION_CHAT_ID", "") or ""
    except Exception:
        return ""


def dispatch_async_delegation(
    *,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    origin_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    progress_fn: Optional[Callable[[], tuple]] = None,
    runtime_effect: Optional[Dict[str, Any]] = None,
    api_execution_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Spawn ``runner`` on the daemon executor and return a handle immediately.

    Parameters
    ----------
    goal, context, toolsets, role, model
        The dispatch-time task spec, captured verbatim for the rich
        completion block.
    session_key
        The gateway session_key (from ``tools.approval.get_current_session_key``)
        captured on the parent thread BEFORE dispatch, because the daemon
        worker thread won't carry the contextvar. Used to route the
        completion back to the originating session.
    parent_session_id
        The durable ``state.db`` session id of the parent agent that spawned
        the delegation. Carried on the completion event so the gateway can
        pin routing to the spawning session instead of recovering the latest
        ``ended_at IS NULL`` row for the peer tuple (#57498).
    runner
        Zero-arg callable that builds + runs the child and returns the same
        result dict ``_run_single_child`` produces. Runs on the worker thread.
    interrupt_fn
        Optional callable to signal the child to stop (used on shutdown /
        explicit cancel).
    progress_fn
        Optional zero-arg callable returning ``(token, in_tool)`` where
        ``token`` is any comparable snapshot of the child's progress (api
        call count + current tool) and ``in_tool`` says whether the child is
        currently inside a tool call. Sampled by the stale monitor; a frozen
        token past the stale threshold marks the delegation stuck (see the
        stale-detection block at the top of this module). When omitted, the
        delegation is not monitored.
    max_async_children
        Concurrency cap. When at capacity the dispatch is REJECTED (the caller
        should fall back to sync or tell the user) rather than queued, so a
        runaway model can't pile up unbounded background work.

    Returns
    -------
    dict
        ``{"status": "dispatched", "delegation_id": ...}`` on success, or
        ``{"status": "rejected", "error": ...}`` when at capacity.
    """
    runtime_effect = _normalize_optional_runtime_effect(runtime_effect)
    api_execution_context = _normalize_optional_api_execution_context(
        api_execution_context
    )
    try:
        model = _normalize_optional_durable_model(
            model,
            field="async dispatch.model",
        )
    except ValueError:
        return {
            "status": "rejected",
            "error": "Async delegation model metadata is unsafe",
        }
    _validate_api_execution_origin(
        api_execution_context,
        origin_session_id,
    )
    delivery_store = _current_event_delivery_store()
    delegation_id = _new_delegation_id()
    if not _is_strong_delegation_id(delegation_id):
        return {
            "status": "rejected",
            "error": (
                "Async delegation id must use the canonical deleg_<uuid4-hex> "
                "128-bit shape"
            ),
        }
    dispatched_at = time.time()
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": goal,
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "origin_session_id": origin_session_id,
        "parent_session_id": parent_session_id,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "progress_fn": progress_fn,
        "runtime_effect": runtime_effect,
        "api_execution_context": api_execution_context,
        # Host-only durable identity captured at dispatch.  The stale monitor
        # finalizes on its own raw thread, where the originating ContextVar is
        # absent, so every later persistence/publish step must use this record
        # field rather than re-reading ambient get_hermes_home().
        "_delivery_store": delivery_store,
        # Stale-monitor bookkeeping (see _stale_monitor_loop).
        "_progress_token": None,
        "_progress_ts": dispatched_at,
        "_interrupted_at": None,
    }
    # Capacity check and record insert under ONE lock hold — checking
    # active_count() separately would let two concurrent dispatches (e.g.
    # from different gateway sessions) both pass the check and exceed the cap.
    with _records_lock:
        running = sum(
            1 for r in _records.values()
            if r.get("status") in ("running", "stalling")
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or run this task synchronously "
                    f"(background=false). Raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background subagents."
                ),
            }
        if delegation_id in _records:
            return {
                "status": "rejected",
                "error": (
                    "Async delegation id already exists; refusing to "
                    "overwrite an active or retained result"
                ),
            }
        _records[delegation_id] = record

    try:
        _persist_dispatch(record)
    except Exception as exc:
        with _records_lock:
            if _records.get(delegation_id) is record:
                _records.pop(delegation_id, None)
        logger.error(
            "Async delegation %s durable dispatch failed; rejecting before "
            "the worker starts: %s",
            delegation_id,
            exc,
        )
        return {
            "status": "rejected",
            "error": f"Failed to persist async delegation: {exc}",
        }
    executor = _get_executor(max_async_children)

    def _worker() -> None:
        result: Dict[str, Any] = {}
        status = "error"
        try:
            result = runner() or {}
            status = _canonical_delegation_status(result)
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation %s crashed", delegation_id)
            result = {
                "status": "error",
                "summary": None,
                "error": f"{type(exc).__name__}: {exc}",
                "api_calls": 0,
                "duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize(delegation_id, result, status)

    try:
        # Propagate the dispatching profile so the detached child resolves
        # get_hermes_home() under the right profile.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover — pool submit failure is rare
        with _records_lock:
            _records.pop(delegation_id, None)
        _delete_durable_delegation(
            delegation_id,
            store=delivery_store,
        )
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation: {exc}",
        }
    if progress_fn is not None:
        _ensure_stale_monitor()

    logger.info(
        "Dispatched async delegation %s (session_key=%s): %s",
        delegation_id, session_key or "<cli>", (goal or "")[:80],
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize(delegation_id: str, result: Dict[str, Any], status: str) -> None:
    """Mark a record complete and push the completion event onto the queue."""
    claimed = _begin_finalization(delegation_id)
    if claimed is None:
        return
    event_record, _interrupt_fn = claimed

    if _push_completion_event(event_record, result, status):
        _finish_finalization(delegation_id, status)


def _begin_finalization(
    delegation_id: str,
) -> Optional[tuple[Dict[str, Any], Optional[Callable[[], None]]]]:
    """Atomically claim terminal delivery while keeping the record active."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("status") not in ("running", "stalling"):
            return
        # Stay active until durable persistence and queue publication finish;
        # otherwise process shutdown can kill this daemon worker in the narrow
        # gap after status flips but before SQLite is committed.
        record["status"] = "finalizing"
        record["completed_at"] = time.time()
        interrupt_fn = record.get("interrupt_fn")
        record["interrupt_fn"] = None  # drop the closure; child is done
        record["progress_fn"] = None  # stop stale-monitor sampling
        event_record = dict(record)

    return event_record, interrupt_fn


def _finish_finalization(delegation_id: str, status: str) -> None:
    with _records_lock:
        record = _records.get(delegation_id)
        if record is not None:
            record["status"] = status
        _prune_completed_locked()


def _finalize_spool_directory(store: EventDeliveryStore) -> Path:
    """Return the host-owned sidecar used when SQLite finalization is down."""

    _verify_event_delivery_store(store)
    path = (
        Path(store.hermes_home)
        / "cache"
        / "delegation"
        / "finalize-spool"
    )
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _finalize_spool_path(
    store: EventDeliveryStore,
    delegation_id: str,
) -> Path:
    digest = hashlib.sha256(
        str(delegation_id or "").encode("utf-8")
    ).hexdigest()
    return _finalize_spool_directory(store) / f"{digest}.json"


def _write_finalize_spool(
    *,
    event: Dict[str, Any],
    result: Dict[str, Any],
    terminal_status: str,
    store: EventDeliveryStore,
) -> Path:
    """Atomically preserve an exact terminal payload outside the failing DB."""

    delegation_id = str(event.get("delegation_id") or "").strip()
    if not delegation_id:
        raise ValueError("finalization spool requires a delegation id")
    event = _scrub_terminal_model_metadata(event)
    result = _scrub_terminal_model_metadata(result)
    payload = {
        "schema": _FINALIZE_SPOOL_SCHEMA,
        "delegation_id": delegation_id,
        "terminal_status": str(terminal_status or "error"),
        "profile_identity": {
            "profile": store.profile or "default",
            "source_home": store.source_home,
            "canonical_home": store.hermes_home,
            "profile_generation": store.profile_generation,
        },
        "event": event,
        "result": result,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    directory = _finalize_spool_directory(store)
    destination = _finalize_spool_path(store, delegation_id)
    temporary = directory / (
        f".{destination.stem}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while persisting finalization spool")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, destination)
        directory_fd = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def _remove_finalize_spool(path: object) -> None:
    if not path:
        return
    spool_path = Path(str(path))
    try:
        spool_path.unlink()
    except FileNotFoundError:
        return
    directory_fd = os.open(
        spool_path.parent,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _recover_finalize_spool(store: EventDeliveryStore) -> int:
    """Commit sidecar terminal payloads before abandoned-owner recovery."""

    directory = _finalize_spool_directory(store)
    recovered = 0
    expected_identity = {
        "profile": store.profile or "default",
        "source_home": store.source_home,
        "canonical_home": store.hermes_home,
        "profile_generation": store.profile_generation,
    }
    for path in sorted(directory.glob("*.json")):
        try:
            raw = path.read_bytes()
            if len(raw) > _FINALIZE_SPOOL_READ_LIMIT_BYTES:
                raise ValueError("finalization spool is oversized")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("finalization spool must be a JSON object")
            if payload.get("schema") != _FINALIZE_SPOOL_SCHEMA:
                raise ValueError("unknown finalization spool schema")
            if payload.get("profile_identity") != expected_identity:
                raise ValueError(
                    "finalization spool profile identity no longer matches"
                )
            event = payload.get("event")
            result = payload.get("result")
            delegation_id = str(payload.get("delegation_id") or "")
            if (
                not isinstance(event, dict)
                or not isinstance(result, dict)
                or str(event.get("delegation_id") or "") != delegation_id
                or path != _finalize_spool_path(store, delegation_id)
            ):
                raise ValueError("finalization spool payload is inconsistent")
            persisted_event = dict(event)
            persisted_event.pop(_EVENT_DELIVERY_STORE_KEY, None)
            persisted_event.pop("restored", None)
            if not persisted_event.get(
                _EVENT_DELIVERY_PROFILE_GENERATION_KEY
            ):
                persisted_event[
                    _EVENT_DELIVERY_PROFILE_GENERATION_KEY
                ] = store.profile_generation
            with _DB_LOCK, _transaction() as conn:
                row = conn.execute(
                    """SELECT event_json, result_json
                       FROM async_delegations WHERE delegation_id=?""",
                    (delegation_id,),
                ).fetchone()
            already_committed = False
            if row and row[0] and row[1]:
                try:
                    already_committed = (
                        json.loads(row[0]) == persisted_event
                        and json.loads(row[1]) == result
                    )
                except (TypeError, ValueError):
                    already_committed = False
            if not already_committed:
                _persist_completion(event, result)
            _remove_finalize_spool(path)
            recovered += 1
        except Exception:
            logger.error(
                "Could not recover async finalization spool %s; retaining it "
                "for an explicit later retry",
                path,
                exc_info=True,
            )
    return recovered


def _record_finalize_failure(
    delegation_id: str,
    *,
    event: Dict[str, Any],
    result: Dict[str, Any],
    terminal_status: str,
    error: BaseException,
) -> None:
    """Durably spool and retain one explicit failed finalization obligation."""

    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        store = _record_event_delivery_store(record)
    spool_path: Optional[Path] = None
    spool_error: Optional[BaseException] = None
    try:
        spool_path = _write_finalize_spool(
            event=event,
            result=result,
            terminal_status=terminal_status,
            store=store,
        )
    except Exception as exc:
        spool_error = exc
        logger.critical(
            "Async delegation %s could not persist either its SQLite terminal "
            "row or its exact finalization spool",
            delegation_id,
            exc_info=True,
        )

    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        record["status"] = "finalize_failed"
        record["finalize_terminal_status"] = terminal_status
        record["finalize_error"] = f"{type(error).__name__}: {error}"
        record["finalize_spooled"] = spool_path is not None
        if spool_error is not None:
            record["finalize_spool_error"] = (
                f"{type(spool_error).__name__}: {spool_error}"
            )
        else:
            record.pop("finalize_spool_error", None)
        # Private raw payloads remain available for an explicit later retry
        # without exposing arbitrary non-JSON values through the status API.
        record["_finalize_event"] = event
        record["_finalize_result"] = result
        record["_finalize_spool_path"] = (
            str(spool_path) if spool_path is not None else ""
        )
        _prune_completed_locked()


def _persist_terminal_event_with_retry(
    *,
    event: Dict[str, Any],
    result: Dict[str, Any],
    store: EventDeliveryStore,
) -> Optional[BaseException]:
    """Persist one exact terminal payload, retrying bounded transient errors."""

    last_error: Optional[BaseException] = None
    for attempt, delay in enumerate(
        _FINALIZE_PERSIST_RETRY_DELAYS_SECONDS,
        start=1,
    ):
        if delay:
            time.sleep(delay)
        try:
            _verify_event_delivery_store(store)
            with _delivery_home_scope(store.hermes_home):
                _persist_completion(event, result)
            return None
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Async delegation %s durable finalize attempt %d/%d failed: %s",
                event.get("delegation_id", ""),
                attempt,
                len(_FINALIZE_PERSIST_RETRY_DELAYS_SECONDS),
                exc,
            )
    return last_error or RuntimeError("durable finalization failed")


def _publish_persisted_terminal_event(
    event: Dict[str, Any],
    store: EventDeliveryStore,
) -> None:
    """Publish only an event whose durable terminal row already committed."""

    try:
        _stamp_event_delivery_store(event, store)
        from tools.process_registry import process_registry

        process_registry.completion_queue.put(event)
    except Exception:
        # The SQLite row remains pending and restart/rescan recovery will issue
        # a fresh process-local store stamp.  Never describe this as result
        # loss: durable commit already succeeded.
        logger.error(
            "Async delegation %s committed but immediate queue publication "
            "failed; the durable pending row remains recoverable",
            event.get("delegation_id", ""),
            exc_info=True,
        )


def retry_failed_finalization(delegation_id: str) -> bool:
    """Retry one retained ``finalize_failed`` obligation without rerunning work."""

    with _records_lock:
        record = _records.get(str(delegation_id or ""))
        if record is None or record.get("status") != "finalize_failed":
            return False
        event = record.get("_finalize_event")
        result = record.get("_finalize_result")
        terminal_status = str(
            record.get("finalize_terminal_status") or "error"
        )
        if not isinstance(event, dict) or not isinstance(result, dict):
            return False
        record["status"] = "finalizing"
        event = dict(event)
        result = dict(result)
        store = _record_event_delivery_store(record)
        spool_path = record.get("_finalize_spool_path")

    persist_error = _persist_terminal_event_with_retry(
        event=event,
        result=result,
        store=store,
    )
    if persist_error is not None:
        _record_finalize_failure(
            delegation_id,
            event=event,
            result=result,
            terminal_status=terminal_status,
            error=persist_error,
        )
        return False
    try:
        _remove_finalize_spool(spool_path)
    except Exception:
        # The DB now contains the exact payload.  Recovery compares the
        # sidecar with that row before deleting it, so a leftover file cannot
        # reset an already-delivered row to pending.
        logger.warning(
            "Async delegation %s committed but its redundant finalization "
            "spool could not be removed",
            delegation_id,
            exc_info=True,
        )
    _publish_persisted_terminal_event(event, store)
    _finish_finalization(delegation_id, terminal_status)
    return True


def _push_completion_event(
    record: Dict[str, Any], result: Dict[str, Any], status: str
) -> bool:
    """Durably commit, then publish, one single-child terminal event."""

    safe_result = _scrub_terminal_model_metadata(result)
    summary = safe_result.get("summary")
    error = safe_result.get("error")
    dispatched_at = record.get("dispatched_at") or time.time()
    completed_at = record.get("completed_at") or time.time()

    evt = {
        "type": "async_delegation",
        "delegation_id": record.get("delegation_id"),
        # session_key routes the completion back to the originating gateway
        # session; empty string => CLI (single-session) path.
        "session_key": record.get("session_key", ""),
        "origin_ui_session_id": record.get("origin_ui_session_id", ""),
        "origin_session_id": record.get("origin_session_id", ""),
        "parent_session_id": record.get("parent_session_id"),
        "goal": record.get("goal", ""),
        "context": record.get("context"),
        "toolsets": record.get("toolsets"),
        "role": record.get("role"),
        "model": safe_result.get("model") or record.get("model"),
        "status": status,
        "summary": summary,
        "error": error,
        "api_calls": safe_result.get("api_calls", 0),
        "duration_seconds": safe_result.get(
            "duration_seconds", round(completed_at - dispatched_at, 2)
        ),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
        "exit_reason": safe_result.get("exit_reason"),
        "runtime_effect": _normalize_optional_runtime_effect(
            record.get("runtime_effect")
        ),
        "api_execution_context": _normalize_optional_api_execution_context(
            record.get("api_execution_context")
        ),
    }
    if normalize_terminal_outcome(safe_result).contradictory:
        evt["terminal_outcome_contradictory"] = True
    # Structured stall metadata (#51690) — additive, present only on
    # stall-monitor finalizations.
    for _k in (
        "stalled_after_quiet_seconds",
        "stall_threshold_seconds",
        "stall_phase",
        "stall_grace_seconds",
    ):
        if _k in safe_result:
            evt[_k] = safe_result[_k]
    store = _record_event_delivery_store(record)
    evt[_EVENT_DELIVERY_PROFILE_GENERATION_KEY] = (
        store.profile_generation
    )
    persist_error = _persist_terminal_event_with_retry(
        event=evt,
        result=safe_result,
        store=store,
    )
    if persist_error is not None:
        _record_finalize_failure(
            str(record.get("delegation_id") or ""),
            event=evt,
            result=safe_result,
            terminal_status=status,
            error=persist_error,
        )
        logger.error(
            "Async delegation %s exhausted durable finalization retries; "
            "the exact result is retained in an explicit finalize_failed "
            "obligation and was not published",
            record.get("delegation_id"),
        )
        return False
    _publish_persisted_terminal_event(evt, store)
    return True


def dispatch_async_delegation_batch(
    *,
    goals: List[str],
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    origin_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    delegation_id: Optional[str] = None,
    progress_fn: Optional[Callable[[], tuple]] = None,
    runtime_effect: Optional[Dict[str, Any]] = None,
    api_execution_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dispatch a WHOLE fan-out batch as ONE background unit.

    Unlike ``dispatch_async_delegation`` (which backs a single subagent),
    ``runner`` here runs the entire batch — it builds and joins on every child
    in parallel and returns the combined ``{"results": [...],
    "total_duration_seconds": N}`` dict that the synchronous path would have
    returned. We occupy ONE async slot for the whole batch (the in-batch
    parallelism is bounded separately by ``max_concurrent_children``), so a
    single ``delegate_task`` fan-out never exhausts the async pool by itself.

    When the batch finishes, a SINGLE completion event is pushed onto the
    shared ``process_registry.completion_queue`` carrying the full per-task
    ``results`` list, so the consolidated summaries re-enter the conversation
    as one message once every child is done — the chat is never blocked while
    they run.

    Returns ``{"status": "dispatched", "delegation_id": ...}`` on success or
    ``{"status": "rejected", "error": ...}`` when the async pool is at
    capacity.
    """
    runtime_effect = _normalize_optional_runtime_effect(runtime_effect)
    api_execution_context = _normalize_optional_api_execution_context(
        api_execution_context
    )
    try:
        model = _normalize_optional_durable_model(
            model,
            field="async batch dispatch.model",
        )
    except ValueError:
        return {
            "status": "rejected",
            "error": "Async delegation model metadata is unsafe",
        }
    _validate_api_execution_origin(
        api_execution_context,
        origin_session_id,
    )
    delivery_store = _current_event_delivery_store()
    delegation_id = str(
        _new_delegation_id()
        if delegation_id is None
        else delegation_id
    ).strip()
    if not _is_strong_delegation_id(delegation_id):
        return {
            "status": "rejected",
            "error": (
                "Async delegation id must use the canonical deleg_<uuid4-hex> "
                "128-bit shape"
            ),
        }
    dispatched_at = time.time()
    n = len(goals)
    # A combined goal label for status listings / the completion header.
    combined_goal = (
        goals[0] if n == 1 else f"{n} parallel subagents: " + "; ".join(g[:40] for g in goals)
    )
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": combined_goal,
        "goals": list(goals),
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "origin_session_id": origin_session_id,
        "parent_session_id": parent_session_id,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "is_batch": True,
        "progress_fn": progress_fn,
        "runtime_effect": runtime_effect,
        "api_execution_context": api_execution_context,
        "_delivery_store": delivery_store,
        "_progress_token": None,
        "_progress_ts": dispatched_at,
        "_interrupted_at": None,
    }
    with _records_lock:
        running = sum(
            1 for r in _records.values()
            if r.get("status") in ("running", "stalling")
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background units."
                ),
            }
        if delegation_id in _records:
            return {
                "status": "rejected",
                "error": (
                    "Async delegation id already exists; refusing to "
                    "overwrite an active or retained result"
                ),
            }
        _records[delegation_id] = record

    try:
        _persist_dispatch(record)
    except Exception as exc:
        with _records_lock:
            if _records.get(delegation_id) is record:
                _records.pop(delegation_id, None)
        logger.error(
            "Async delegation batch %s durable dispatch failed; rejecting "
            "before the worker starts: %s",
            delegation_id,
            exc,
        )
        return {
            "status": "rejected",
            "error": f"Failed to persist async delegation batch: {exc}",
        }
    executor = _get_executor(max_async_children)

    def _worker() -> None:
        combined: Dict[str, Any] = {}
        status = "error"
        try:
            combined = runner() or {}
            status = _aggregate_batch_status(combined.get("results"))
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation batch %s crashed", delegation_id)
            combined = {
                "results": [],
                "error": f"{type(exc).__name__}: {exc}",
                "total_duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize_batch(delegation_id, combined, status)

    try:
        # Propagate the dispatching profile to the detached batch children.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover
        with _records_lock:
            _records.pop(delegation_id, None)
        _delete_durable_delegation(
            delegation_id,
            store=delivery_store,
        )
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation batch: {exc}",
        }
    if progress_fn is not None:
        _ensure_stale_monitor()

    logger.info(
        "Dispatched async delegation batch %s (%d task(s), session_key=%s)",
        delegation_id, n, session_key or "<cli>",
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _canonical_delegation_status(result: Any) -> str:
    """Map one child result through the shared terminal-outcome contract."""

    outcome = normalize_terminal_outcome(result)
    if outcome.failed:
        return "failed"
    if outcome.interrupted:
        return "interrupted"
    if outcome.partial:
        return "partial"
    return "completed"


def _canonical_batch_event_results(
    child_results: Any,
) -> tuple[List[Dict[str, Any]], bool]:
    """Canonicalize event-facing child status while retaining raw DB result."""

    if not isinstance(child_results, list):
        return [], False
    normalized_results: List[Dict[str, Any]] = []
    contradictory = False
    for result in child_results:
        outcome = normalize_terminal_outcome(result)
        if isinstance(result, dict):
            normalized = _scrub_terminal_model_metadata(result)
        else:
            normalized = {
                "summary": None,
                "error": "Invalid delegated child result",
            }
        normalized["status"] = _canonical_delegation_status(result)
        if outcome.contradictory:
            normalized["terminal_outcome_contradictory"] = True
            contradictory = True
        normalized_results.append(normalized)
    return normalized_results, contradictory


def _aggregate_batch_status(child_results: Any) -> str:
    """Classify a fan-out from every child outcome without inventing success."""
    if not isinstance(child_results, list) or not child_results:
        return "error"

    statuses = [
        _canonical_delegation_status(result)
        for result in child_results
    ]

    if all(status == "completed" for status in statuses):
        return "completed"
    if all(status == "interrupted" for status in statuses):
        return "interrupted"
    if any(status in {"completed", "partial"} for status in statuses):
        # At least one child produced useful work, but the complete fan-out
        # contract was not satisfied.
        return "partial"
    return "error"


def _finalize_batch(
    delegation_id: str, combined: Dict[str, Any], status: str
) -> None:
    """Mark a batch record complete and push ONE combined completion event."""
    claimed = _begin_finalization(delegation_id)
    if claimed is None:
        return
    event_record, _interrupt_fn = claimed

    if _push_batch_completion_event(event_record, combined, status):
        _finish_finalization(delegation_id, status)


def _push_batch_completion_event(
    event_record: Dict[str, Any], combined: Dict[str, Any], status: str
) -> bool:
    """Durably commit, then publish, one batch terminal event."""

    dispatched_at = event_record.get("dispatched_at") or time.time()
    completed_at = event_record.get("completed_at") or time.time()
    canonical_results, contradictory = _canonical_batch_event_results(
        combined.get("results")
    )
    safe_combined = _scrub_terminal_model_metadata(combined)
    evt = {
        "type": "async_delegation",
        "delegation_id": event_record.get("delegation_id"),
        "session_key": event_record.get("session_key", ""),
        "origin_ui_session_id": event_record.get("origin_ui_session_id", ""),
        "origin_session_id": event_record.get("origin_session_id", ""),
        "parent_session_id": event_record.get("parent_session_id"),
        "goal": event_record.get("goal", ""),
        "goals": event_record.get("goals"),
        "context": event_record.get("context"),
        "toolsets": event_record.get("toolsets"),
        "role": event_record.get("role"),
        "model": event_record.get("model"),
        "status": status,
        "is_batch": True,
        # The full per-task results list — the formatter renders a
        # consolidated multi-task block from this.
        "results": canonical_results,
        # Per-task live transcript log paths (cache/delegation/live/...).
        # They persist after completion and double as the full-fidelity
        # operational record of each child's run.
        "live_transcripts": combined.get("live_transcripts"),
        "error": combined.get("error"),
        "total_duration_seconds": combined.get("total_duration_seconds"),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
        "runtime_effect": _normalize_optional_runtime_effect(
            event_record.get("runtime_effect")
        ),
        "api_execution_context": _normalize_optional_api_execution_context(
            event_record.get("api_execution_context")
        ),
    }
    if contradictory:
        evt["terminal_outcome_contradictory"] = True
    # Structured stall metadata (#51690) — additive, present only on
    # stall-monitor finalizations.
    for _k in (
        "stalled_after_quiet_seconds",
        "stall_threshold_seconds",
        "stall_phase",
        "stall_grace_seconds",
    ):
        if _k in combined:
            evt[_k] = combined[_k]
    store = _record_event_delivery_store(event_record)
    evt[_EVENT_DELIVERY_PROFILE_GENERATION_KEY] = (
        store.profile_generation
    )
    persist_error = _persist_terminal_event_with_retry(
        event=evt,
        result=safe_combined,
        store=store,
    )
    if persist_error is not None:
        _record_finalize_failure(
            str(event_record.get("delegation_id") or ""),
            event=evt,
            result=safe_combined,
            terminal_status=status,
            error=persist_error,
        )
        logger.error(
            "Async delegation batch %s exhausted durable finalization retries; "
            "the exact result is retained in an explicit finalize_failed "
            "obligation and was not published",
            event_record.get("delegation_id"),
        )
        return False
    _publish_persisted_terminal_event(evt, store)
    return True


def _ensure_stale_monitor() -> None:
    """Start (once) the module-level stale-delegation monitor thread.

    One daemon thread serves every dispatch; it exits on its own when no
    monitorable records remain, and is restarted by the next dispatch that
    carries a ``progress_fn``.
    """
    global _monitor_thread
    with _monitor_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return
        _monitor_stop.clear()
        _monitor_thread = threading.Thread(
            target=_stale_monitor_loop,
            name="async-delegate-stale-monitor",
            daemon=True,
        )
        _monitor_thread.start()


def _stale_monitor_loop() -> None:
    """Sweep running delegations for stalled progress.

    Per sweep, for every running record with a ``progress_fn``:

    - Sample ``(token, in_tool)``. A changed token refreshes the record's
      progress timestamp — a child that keeps advancing is never touched, no
      matter how long it runs.
    - A frozen token past the idle/in-tool threshold marks the record
      ``stalling``: we call ``interrupt_fn`` so a responsive-but-slow child
      can unwind and deliver its (partial) result through the normal
      ``_finalize`` path with full fidelity.
    - A ``stalling`` record whose runner still hasn't returned after the
      grace window is force-finalized with one terminal ``stalled`` event so
      the owning session hears an outcome and the async slot frees. A late
      runner return after that is ignored by ``_begin_finalization``.
    """
    while not _monitor_stop.wait(_STALE_CHECK_INTERVAL):
        now = time.time()
        stalled: List[tuple] = []  # (delegation_id, is_batch, quiet_for, in_tool)
        expired: List[str] = []  # stalling past grace → force-finalize
        any_monitorable = False
        with _records_lock:
            for record in _records.values():
                status = record.get("status")
                if status == "stalling":
                    any_monitorable = True
                    interrupted_at = record.get("_interrupted_at") or now
                    if now - interrupted_at >= _STALL_GRACE_SECONDS:
                        expired.append(record["delegation_id"])
                    continue
                if status != "running":
                    continue
                progress_fn = record.get("progress_fn")
                if progress_fn is None:
                    continue
                any_monitorable = True
                try:
                    token, in_tool = progress_fn()
                except Exception:
                    # An unreadable child must not look permanently healthy —
                    # keep the last timestamp running instead of refreshing it.
                    token, in_tool = record.get("_progress_token"), False
                if token != record.get("_progress_token"):
                    record["_progress_token"] = token
                    record["_progress_ts"] = now
                    continue
                quiet_for = now - (record.get("_progress_ts") or now)
                limit = (
                    _STALE_IN_TOOL_SECONDS if in_tool else _STALE_IDLE_SECONDS
                )
                if quiet_for >= limit:
                    record["status"] = "stalling"
                    record["_interrupted_at"] = now
                    # Structured stall context for the terminal event and
                    # status listings (#51690): how long progress was frozen,
                    # which threshold applied, and whether the child was
                    # inside a tool when it went quiet.
                    record["_stall_quiet_seconds"] = round(quiet_for, 2)
                    record["_stall_threshold_seconds"] = limit
                    record["_stall_in_tool"] = bool(in_tool)
                    stalled.append(
                        (
                            record["delegation_id"],
                            bool(record.get("is_batch")),
                            quiet_for,
                            in_tool,
                        )
                    )
        for delegation_id, _is_batch, quiet_for, in_tool in stalled:
            logger.warning(
                "Async delegation %s made no progress for %.0fs "
                "(in_tool=%s) — interrupting; grace window %.0fs",
                delegation_id, quiet_for, in_tool, _STALL_GRACE_SECONDS,
            )
            with _records_lock:
                record = _records.get(delegation_id)
                fn = record.get("interrupt_fn") if record else None
            if callable(fn):
                try:
                    fn()
                except Exception as exc:
                    logger.debug(
                        "Async delegation %s stall interrupt failed: %s",
                        delegation_id, exc,
                    )
        for delegation_id in expired:
            _finalize_stalled(delegation_id)
        if not any_monitorable:
            return


def _finalize_stalled(delegation_id: str) -> None:
    """Force-finalize a stalling delegation whose runner never returned."""
    claimed = _begin_finalization(delegation_id)
    if claimed is None:
        return
    event_record, _interrupt_fn = claimed

    completed_at = event_record.get("completed_at") or time.time()
    duration = round(
        completed_at - (event_record.get("dispatched_at") or completed_at),
        2,
    )
    quiet_seconds = event_record.get("_stall_quiet_seconds")
    threshold_seconds = event_record.get("_stall_threshold_seconds")
    stall_in_tool = event_record.get("_stall_in_tool")
    error = (
        f"Async delegation {delegation_id} stalled: the detached subagent "
        "stopped making progress (no new API calls, tool activity, or "
        "streamed tokens), did not respond to interruption, and never "
        "produced a completion event. The worker may be wedged inside a "
        "model API call — this is a known failure mode of long-lived "
        "gateway processes (#60203). Re-dispatch the task if it is still "
        "needed."
    )
    logger.error(
        "Async delegation %s force-finalized as stalled after %.0fs",
        delegation_id, duration,
    )
    # Structured stall metadata (#51690): lets parents and UIs distinguish
    # a stall-monitor kill from other failures without parsing the error
    # string, mirroring the sync path's timeout_seconds/timed_out_after_
    # seconds/timeout_phase fields.
    stall_meta = {
        "stalled_after_quiet_seconds": quiet_seconds,
        "stall_threshold_seconds": threshold_seconds,
        "stall_phase": (
            "in_tool" if stall_in_tool
            else "idle" if stall_in_tool is not None
            else None
        ),
        "stall_grace_seconds": _STALL_GRACE_SECONDS,
    }
    if event_record.get("is_batch"):
        persisted = _push_batch_completion_event(
            event_record,
            {
                "results": [],
                "error": error,
                "total_duration_seconds": duration,
                **stall_meta,
            },
            "stalled",
        )
    else:
        persisted = _push_completion_event(
            event_record,
            {
                "status": "stalled",
                "summary": None,
                "error": error,
                "api_calls": 0,
                "duration_seconds": duration,
                "exit_reason": "stalled",
                **stall_meta,
            },
            "stalled",
        )
    if persisted:
        _finish_finalization(delegation_id, "stalled")


def _children_activity_from_token(token: Any, now: float) -> Optional[List]:
    """Parse a progress token into per-child activity dicts (best-effort).

    delegate_tool's ``_batch_progress`` emits one ``(api_call_count,
    current_tool, last_activity_ts)`` tuple per child. Foreign token shapes
    (custom dispatchers) degrade to ``None`` entries rather than raising —
    the token contract is intentionally opaque to the registry.
    """
    try:
        parts = list(token)
    except TypeError:
        return None
    out: List[Optional[Dict[str, Any]]] = []
    for part in parts:
        if isinstance(part, (list, tuple)) and len(part) >= 2:
            entry: Dict[str, Any] = {
                "api_calls": part[0],
                "current_tool": part[1],
            }
            if len(part) >= 3 and isinstance(part[2], (int, float)):
                entry["seconds_since_activity"] = round(
                    max(0.0, now - float(part[2])), 1
                )
            out.append(entry)
        else:
            out.append(None)
    return out


def list_async_delegations() -> List[Dict[str, Any]]:
    """Snapshot of async delegations (running + recently completed).

    Safe to call from any thread. Excludes the non-serialisable callables
    and private monitor bookkeeping, but exposes computed live-status
    fields for UIs (#51690):

    - ``seconds_since_progress``: how long the stale monitor has seen a
      frozen progress token (running/stalling records).
    - ``children_activity``: per-child ``{api_calls, current_tool,
      seconds_since_activity}`` sampled live from the dispatch's
      ``progress_fn``.
    - ``stalled_after_quiet_seconds`` / ``stall_threshold_seconds`` /
      ``stall_in_tool``: stall context once the monitor has tripped.
    """
    now = time.time()
    samplers: Dict[str, Callable] = {}
    with _records_lock:
        items = []
        for r in _records.values():
            item = {
                k: v
                for k, v in r.items()
                if k not in {"interrupt_fn", "progress_fn"}
                and not k.startswith("_")
            }
            status = r.get("status")
            if status in ("running", "stalling"):
                ts = r.get("_progress_ts")
                if ts:
                    item["seconds_since_progress"] = round(now - ts, 1)
                fn = r.get("progress_fn")
                if callable(fn):
                    samplers[r["delegation_id"]] = fn
            if status in ("stalling", "stalled"):
                for src, dst in (
                    ("_stall_quiet_seconds", "stalled_after_quiet_seconds"),
                    ("_stall_threshold_seconds", "stall_threshold_seconds"),
                    ("_stall_in_tool", "stall_in_tool"),
                ):
                    if r.get(src) is not None:
                        item[dst] = r.get(src)
            items.append(item)

    # Sample live activity OUTSIDE the lock — progress_fn reads child-agent
    # attributes and must never run under _records_lock (a slow or broken
    # sampler would block every dispatch/finalize in the process).
    for item in items:
        fn = samplers.get(item.get("delegation_id"))
        if fn is None:
            continue
        try:
            token, in_tool = fn()
        except Exception:
            continue
        activity = _children_activity_from_token(token, now)
        if activity is not None:
            item["children_activity"] = activity
        item["in_tool"] = bool(in_tool)
    return items


def interrupt_all(reason: str = "shutdown") -> int:
    """Signal every running async delegation to stop. Returns how many.

    Used on ``/stop`` and gateway shutdown so a dangling background subagent
    can't keep burning tokens with no one listening. The child still emits a
    completion event (status='interrupted') via the normal finalize path.
    """
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values()
            if r.get("status") in ("running", "stalling")
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_all: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info("Interrupted %d async delegation(s) (%s)", count, reason)
    return count


def interrupt_for_session(
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
    reason: str = "session_end",
) -> int:
    """Signal running async delegations owned by ONE session to stop.

    A delegation's lifecycle is bound to the session that spawned it: when
    that session ends, its in-flight background subagents must end with it —
    a completed orphan would otherwise sit on the shared completion queue
    with no live owner, either leaking into another chat or burning tokens
    with no one listening (#55578).

    Selectors (any matching field claims the record):
    - ``origin_ui_session_id``: the live TUI tab/window that commissioned it.
    - ``session_key``: the durable routing key captured at dispatch.
    - ``parent_session_id``: the spawning agent's durable session-db id —
      the right selector for gateway chats, whose ``session_key`` (the
      platform conversation key) SURVIVES a ``/new`` reset while the
      session id rotates.

    Returns how many were interrupted.
    """
    if not session_key and not origin_ui_session_id and not parent_session_id:
        return 0
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values()
            if r.get("status") in ("running", "stalling")
            and _matches_session_selectors(
                r,
                session_key=session_key,
                origin_ui_session_id=origin_ui_session_id,
                parent_session_id=parent_session_id,
            )
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_for_session: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info(
            "Interrupted %d async delegation(s) for ending session (%s)",
            count, reason,
        )
    return count


def _reset_for_tests() -> None:
    """Test-only: clear all state and tear down the executor + monitor."""
    global _executor, _executor_max_workers, _monitor_thread
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _executor = None
        _executor_max_workers = 0
    _monitor_stop.set()
    with _monitor_lock:
        thread = _monitor_thread
        _monitor_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)
    with _records_lock:
        _records.clear()
    with _LOST_DELIVERY_CLAIMS_LOCK:
        _LOST_DELIVERY_CLAIMS.clear()
    with _EVENT_DELIVERY_STORES_LOCK:
        _EVENT_DELIVERY_STORES_BY_TOKEN.clear()
        _EVENT_DELIVERY_TOKENS_BY_STORE.clear()
    with _FROZEN_EVENT_DELIVERY_LOCK:
        _FROZEN_EVENT_DELIVERY_BY_PROFILE.clear()
        _FROZEN_EVENT_DELIVERY_BY_HOME.clear()
    with _ACTIVE_WAKE_CLAIMS_LOCK:
        _ACTIVE_WAKE_CLAIMS.clear()
