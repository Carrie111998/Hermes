"""Gateway lifecycle methods for ``GatewayRunner``.

Extracted from ``gateway/run.py`` as part of the god-file decomposition campaign
(Phase 3 mechanical mixin lifts). This mixin holds the runner lifecycle
cluster: construction, startup orchestration, supervised adapter spawn and
watchers, session restore, shutdown/drain/restart procedures, and inbound
message handling for the gateway controller.

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
``self.*`` calls resolve unchanged via the MRO. Neutral dependencies import at
module top; module-level helpers defined in ``gateway.run`` (``logger``,
``_hermes_home``, ``_load_gateway_config``, constants, ...) are imported
lazily inside the methods that use them (``from gateway.run import ...``
resolves at call time, when ``gateway.run`` is fully loaded) so this module
never imports ``gateway.run`` at import time -> no import cycle. The lazy
imports preserve the exact logger name (``"gateway.run"``) so log records are
unchanged.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import faulthandler
import inspect
import json
import logging
import os
import queue
import re
import shlex
import signal
import sys
import threading
import time
import weakref as _weakref
from collections import OrderedDict
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union, cast

from agent.async_utils import consume_detached_task_result, safe_schedule_threadsafe
from agent.i18n import t
from agent.interrupt_compat import request_hard_interrupt
from gateway.config import (
    GatewayConfig,
    Platform,
    _BUILTIN_PLATFORM_VALUES,
    platform_binds_port as _platform_binds_port,
)
from gateway.delivery import (
    DeliveryRouter,
    looks_like_telegram_private_chat_id,
    resolve_delivery_transport,
)
from gateway.platforms.base import (
    BasePlatformAdapter,
    EphemeralReply,
    MessageEvent,
    MessageType,
    _prefix_within_utf16_limit,
    build_auto_tts_output_path,
    merge_pending_message_event,
    utf16_len,
)
from gateway.restart import (
    DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT,
    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
    GATEWAY_FATAL_CONFIG_EXIT_CODE,
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    parse_restart_after_turn_timeout,
    parse_restart_drain_timeout,
)
from gateway.session import (
    AsyncSessionStore,
    SessionContext,
    SessionEntry,
    SessionSource,
    SessionStore,
    build_channel_continuity_note,
    build_session_context,
    build_session_context_prompt,
    build_session_key,
    is_shared_multi_user_session,
    neutralize_untrusted_inline_text,
)
from gateway.session_state import (
    SERVICE_TIER_UNSET as _SERVICE_TIER_UNSET,
    SessionState,
    legacy_dict_property,
    legacy_lease_token_property,
)
from gateway.shutdown_watchdog import (
    DEFAULT_HEARTBEAT_INTERVAL_S,
    _arm_loop_floor_timer,
    arm_shutdown_watchdog,
    loop_heartbeat_forever,
    resolve_shutdown_watchdog_delay,
    start_loop_liveness_watchdog,
)
from gateway.turn_context import TurnContext
from gateway.turn_lease import (
    DEFAULT_LEASE_WAIT,
    SessionTurnLeaseRegistry,
    TurnLeaseTimeoutError,
)
from hermes_cli.config import cfg_get
from hermes_cli.fallback_config import get_fallback_chain
from hermes_constants import get_hermes_home
from utils import is_truthy_value

# Sentinel for "value not provided" (kwdefaults are evaluated at class-definition
# time, so this must be bound here rather than late-imported; gateway/run.py
# re-exports it so ``run._UNSET is runner_lifecycle._UNSET`` stays true).
_UNSET = object()

_hermes_home = get_hermes_home()


class GatewayRunnerLifecycleMixin:
    """
    Main gateway controller.

    Manages the lifecycle of all platform adapters and routes
    messages to/from the agent.
    """

    # Class-level defaults so partial construction in tests doesn't
    # blow up on attribute access.
    _busy_input_mode: str = "interrupt"
    _busy_text_mode: str = "interrupt"
    _restart_drain_timeout: float = DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    _restart_after_turn_timeout: float = DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    _exit_code: Optional[int] = None
    _draining: bool = False
    _external_drain_active: bool = False
    _restart_requested: bool = False
    _restart_task_started: bool = False
    _restart_detached: bool = False
    _restart_via_service: bool = False
    _detached_restart_helper_started: bool = False
    _restart_command_source: Optional[SessionSource] = None
    _stop_task: Optional[asyncio.Task] = None
    _restart_task: Optional[asyncio.Task] = None
    _profile_failed_platforms: Optional[Dict[str, Dict[Platform, asyncio.Task]]] = None
    _systemd_watchdog: Optional[Any] = None
    _startup_restore_in_progress: bool = False

    # ------------------------------------------------------------------
    # Legacy per-session dict adapters.  All per-session state lives in
    # ``self._sessions`` (Dict[str, SessionState]); these properties expose
    # the pre-consolidation dict attributes as LIVE MutableMapping views so
    # the extensive test surface (and a few mixin/adapter call sites) that
    # read/write ``runner._running_agents`` etc. keeps working unchanged.
    # New production code should use ``self._session_state(key)`` directly.
    # ------------------------------------------------------------------
    _running_agents = legacy_dict_property("_running_agents")
    _running_agents_ts = legacy_dict_property("_running_agents_ts")
    _active_session_leases = legacy_dict_property("_active_session_leases")
    _busy_ack_ts = legacy_dict_property("_busy_ack_ts")
    _turn_lease_tokens = legacy_lease_token_property()
    _session_run_generation = legacy_dict_property("_session_run_generation")
    _session_model_overrides = legacy_dict_property("_session_model_overrides")
    _pending_one_turn_model_restores = legacy_dict_property(
        "_pending_one_turn_model_restores"
    )
    _session_reasoning_overrides = legacy_dict_property("_session_reasoning_overrides")
    _session_service_tier_overrides = legacy_dict_property(
        "_session_service_tier_overrides"
    )
    _last_resolved_model = legacy_dict_property("_last_resolved_model")
    _queued_events = legacy_dict_property("_queued_events")
    _pending_turn_sidecar_notes = legacy_dict_property("_pending_turn_sidecar_notes")
    _pending_messages = legacy_dict_property("_pending_messages")
    _pending_native_image_paths_by_session = legacy_dict_property(
        "_pending_native_image_paths_by_session"
    )
    _session_ephemeral_pin = legacy_dict_property("_session_ephemeral_pin")
    _session_vc_last = legacy_dict_property("_session_vc_last")
    _pending_approvals = legacy_dict_property("_pending_approvals")
    _update_prompt_pending = legacy_dict_property("_update_prompt_pending")

    # -- SessionState accessors -----------------------------------------
    def _sessions_map(self) -> Dict[str, "SessionState"]:
        """The per-session state map; lazily created so bare test runners
        built via ``object.__new__`` work without ``__init__``."""
        sessions = self.__dict__.get("_sessions")
        if sessions is None:
            sessions = {}
            self.__dict__["_sessions"] = sessions
        return sessions

    def _session_state(self, session_key: str) -> "SessionState":
        """Get-or-create the :class:`SessionState` for ``session_key``."""
        sessions = self._sessions_map()
        state = sessions.get(session_key)
        if state is None:
            state = SessionState()
            sessions[session_key] = state
        return state

    def _peek_session_state(self, session_key: str) -> Optional["SessionState"]:
        """Return the SessionState for ``session_key`` without creating one."""
        sessions = self.__dict__.get("_sessions")
        if not sessions:
            return None
        return sessions.get(session_key)

    def _is_session_running(self, session_key: str) -> bool:
        """True when the session holds a running-turn slot (agent or sentinel)."""
        state = self._peek_session_state(session_key)
        return state is not None and state.turn.agent is not None

    def _running_agent_items(self) -> List[tuple]:
        """(session_key, agent) pairs for sessions with a running turn
        (including pending sentinels), matching the old ``_running_agents``
        dict contents."""
        return [
            (key, state.turn.agent)
            for key, state in self._sessions_map().items()
            if state.turn.agent is not None
        ]
    # Loop-liveness heartbeat / watchdog handles (#66892, #69089). Class-level
    # defaults so partial construction in tests doesn't blow up on access; the
    # real values are set in __init__ / start() / stop().
    _loop_heartbeat_task: Optional["asyncio.Task"] = None
    _loop_floor_timer_handle: Optional[Any] = None
    _loop_liveness_watchdog: Optional[Any] = None
    _gateway_started_at: float = 0.0
    _shutdown_watchdog_done: Optional["threading.Event"] = None
    _platform_lock_takeover_on_start: bool = False
    _reconnect_watcher_task: Optional["asyncio.Task"] = None

    def __init__(self, config: Optional[GatewayConfig] = None):
        from gateway.run import load_gateway_config_for_runner, logger
        global _gateway_runner_ref
        # When multiplex_profiles is on, load under the default profile secret
        # scope so bot tokens in that profile's .env resolve the same way
        # secondary profiles do (#64674). Explicit config= injection (tests)
        # is left untouched.
        self.config = config if config is not None else load_gateway_config_for_runner()
        # Mark the process as a profile multiplexer when configured. This flips
        # agent.secret_scope.get_secret() to fail-closed on any unscoped
        # credential read, so a missed migration crashes loudly instead of
        # leaking a cross-profile value (Workstream A). Inert when off.
        try:
            from agent.secret_scope import set_multiplex_active
            set_multiplex_active(bool(getattr(self.config, "multiplex_profiles", False)))
        except Exception:
            logger.debug("could not set multiplex-active flag", exc_info=True)
        self.adapters: Dict[Platform, BasePlatformAdapter] = {}
        # Multi-profile multiplexing: adapters for NON-default profiles live
        # here, keyed by profile name then Platform. self.adapters stays the
        # default/active profile's map so the ~93 existing self.adapters[...]
        # sites are untouched when multiplexing is off (this dict is empty).
        # Populated by _start_secondary_profile_adapters().
        self._profile_adapters: Dict[str, Dict[Platform, BasePlatformAdapter]] = {}
        self._warn_if_docker_media_delivery_is_risky()
        _gateway_runner_ref = _weakref.ref(self)

        # Load ephemeral config from config.yaml / env vars.
        # Both are injected at API-call time only and never persisted.
        self._prefill_messages = self._load_prefill_messages()
        self._ephemeral_system_prompt = self._load_ephemeral_system_prompt()
        self._reasoning_config = self._load_reasoning_config()
        self._service_tier = self._load_service_tier()
        self._show_reasoning = self._load_show_reasoning()
        self._busy_input_mode = self._load_busy_input_mode()
        self._busy_text_mode = self._load_busy_text_mode()
        # Secondary-profile busy modes are snapshotted during multiplex
        # startup. Busy-message handlers consult these maps by routed source
        # without rereading config or mutating process-global environment.
        self._busy_input_modes_by_profile: Dict[str, str] = {}
        self._busy_text_modes_by_profile: Dict[str, str] = {}
        self._restart_drain_timeout = self._load_restart_drain_timeout()
        self._restart_after_turn_timeout = self._load_restart_after_turn_timeout()
        self._provider_routing = self._load_provider_routing()
        self._fallback_model = self._load_fallback_model()

        # Wire process registry into session store for reset protection.
        # A background process older than the configured threshold (default 24h,
        # session_reset.bg_process_max_age_hours) is treated as stale and no
        # longer blocks session idle / daily reset — see #29177. The process is
        # NOT killed, only ignored by the reset guard.
        from tools.process_registry import process_registry
        _bg_max_age_hours = getattr(
            self.config.default_reset_policy, "bg_process_max_age_hours", 24
        )
        _bg_max_age_seconds = (
            _bg_max_age_hours * 3600 if _bg_max_age_hours and _bg_max_age_hours > 0 else None
        )
        self.session_store = SessionStore(
            self.config.sessions_dir, self.config,
            has_active_processes_fn=lambda key: process_registry.has_active_for_session(
                key, max_active_age=_bg_max_age_seconds,
            ),
        )
        # One enforced loop-side boundary for the synchronous SessionStore.
        # Sync helpers keep using ``session_store`` directly; async gateway
        # handlers call this facade and await every operation.
        self._async_session_store = AsyncSessionStore(self.session_store)
        self.delivery_router = DeliveryRouter(self.config)
        self._running = False
        self._gateway_loop: Optional[asyncio.AbstractEventLoop] = None
        self._shutdown_event = asyncio.Event()
        self._exit_cleanly = False
        self._exit_with_failure = False
        self._exit_reason: Optional[str] = None
        self._exit_code: Optional[int] = None
        self._draining = False
        self._profile_failed_platforms: Dict[str, Dict[Platform, asyncio.Task]] = {}
        self._systemd_watchdog = None
        # External (NAS-driven) drain state — distinct from the shutdown
        # ``_draining`` flag above. Set by ``_drain_control_watcher`` when the
        # ``.drain_request.json`` marker is present: the gateway flips
        # ``gateway_state -> draining`` and refuses NEW turns, but the process
        # does NOT exit (the whole point — quiesce-without-restart, D4a). It is
        # fully reversible: removing the marker reverts to ``running`` and
        # re-accepts turns. ``_draining`` (shutdown) is one-way and ends in
        # process exit; this one is a steady state NAS polls during its
        # request -> poll -> proceed loop.
        self._external_drain_active = False
        self._restart_requested = False
        # Set by shutdown_signal_handler when a SIGTERM/SIGINT arrived
        # WITHOUT a planned-stop / takeover marker — i.e. an unexpected
        # external signal (container/s6 SIGTERM on `docker restart` or
        # image upgrade, OOM-killer, bare `kill`). Distinct from an
        # operator-requested stop, which writes a marker first. Used by
        # _stop_impl to decide whether to persist gateway_state=stopped
        # (see issue #42675): an unexpected signal must NOT persist
        # "stopped", or container_boot refuses to auto-start the gateway
        # on the next boot.
        self._signal_initiated_shutdown = False
        self._restart_task_started = False
        self._restart_detached = False
        self._restart_via_service = False
        self._detached_restart_helper_started = False
        self._restart_command_source: Optional[SessionSource] = None
        # Monotonic-ish wall clock of when this GatewayRunner was constructed.
        # Used by the /restart redelivery guard to bound the window in which a
        # missing dedup marker is treated as a stale redelivery.
        self._startup_time: float = time.time()
        # Set True at startup when this process booted as the result of a
        # chat-originated /restart (i.e. .restart_notify.json existed on boot).
        # A one-shot signal consumed by _is_stale_restart_redelivery so the
        # marker-missing fallback only suppresses a /restart when we KNOW we
        # just came out of a restart cycle — never on a genuine fresh boot.
        self._booted_from_restart: bool = False
        self._stop_task: Optional[asyncio.Task] = None
        self._restart_task: Optional[asyncio.Task] = None
        self._executor_lock = threading.Lock()
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        # Set on gateway stop so the recreate-on-shutdown path can't resurrect
        # the pool during a real shutdown.
        self._executor_closing = False
        # ALL per-session state (turn / conversation / persistent scopes)
        # lives in one container — see gateway/session_state.py.  Access via
        # self._session_state(key) (get-or-create) or
        # self._peek_session_state(key) (read-only).
        self._sessions: Dict[str, SessionState] = {}
        # Per-SESSION_ID turn lease (#64934): serializes the
        # [load history → run → flush] region when two ROUTING KEYS resolve
        # to one session_id (switch_session's many-to-one mapping). The
        # routing-key guards above cannot see that overlap. Acquired in
        # _handle_message_with_agent after session resolution is final,
        # released via _release_turn_lease in the same method's finally.
        self._turn_leases = SessionTurnLeaseRegistry()
        # Tokens for held turn leases, keyed by (routing key, run generation)
        # so release is granted per-turn and a stale unwind can never free a
        # newer turn's lease (#28686 ownership lesson).
        # Held turn-lease tokens live on SessionState.turn.lease_token /
        # .lease_generation (the old dict was keyed (routing key, generation)
        # so a stale unwind could never free a newer turn's lease — the
        # generation field preserves that ownership check, #28686).
        # Runner-level queued interrupt text lives on
        # SessionState.persistent.pending_command_text (NOTE: distinct from
        # the adapter-level _pending_messages Dict[str, MessageEvent] in
        # gateway/platforms/base.py, which shares the legacy name).
        # Last successfully-resolved (non-empty) model, keyed by session. Used
        # as a fallback when a fresh config read transiently returns an empty
        # model (e.g. an mtime-keyed config-cache miss during a post-interrupt
        # recovery turn). Without this, the agent is built with model="" and
        # every API call fails HTTP 400 "No models provided" — the session goes
        # silent until the user manually re-sends. See #35314. The ``"*"``
        # session entry holds a process-wide last-known-good for sessions
        # seen for the first time.  Lives on
        # SessionState.conversation.last_resolved_model.
        # Overflow buffer for explicit /queue commands.  The adapter-level
        # _pending_messages dict is a single slot per session (designed for
        # "next-turn" follow-ups where repeated sends collapse into one
        # event).  /queue has different semantics: each invocation must
        # produce its own full agent turn, in FIFO order, with no merging.
        # When the slot is occupied, additional /queue items land here and
        # are promoted one-at-a-time after each run's drain.  Cleared on
        # /new and /reset.  /model and other mid-session operations
        # preserve the queue.  Lives on SessionState.conversation.queued_events;
        # native image paths, busy-ack debounce timestamps and the monotonic
        # run-generation counter (#28686, NEVER reset) live on SessionState too.
        # Session keys that already received a stall notification for the
        # current stall episode (cleared when pending clears / activity resumes
        # / conversation boundary). See gateway.session_stall.
        self._session_stall_notified: Dict[str, bool] = {}
        # Startup restore gate: while restart-interrupted sessions are being
        # auto-resumed, real inbound messages are queued instead of competing
        # with the synthetic resume turns for the same session.  The queued
        # events drain only after all startup resume tasks have finished.
        self._startup_restore_in_progress = False
        # Set by start_gateway() only for an explicit ``--replace`` launch.
        # _connect_initial_adapter_with_timeout scopes it to each adapter's
        # cold-start connect and removes it before any reconnect can run.
        self._platform_lock_takeover_on_start = False
        self._startup_restore_queue: List[MessageEvent] = []
        self._startup_restore_tasks: List[asyncio.Task] = []
        # LRU cache of live SessionSources keyed by session_key. Used by
        # fallback routing paths (shutdown notifications, synthetic
        # background-process events) when the persisted origin is missing
        # and _parse_session_key can't recover thread_id. Capped so it
        # cannot grow unbounded over a long-running gateway lifetime.
        self._session_sources: "OrderedDict[str, SessionSource]" = OrderedDict()
        self._session_sources_max = 512
        # Completion delivery is intentionally lifecycle-scoped. This closes
        # duplicate queue/watcher races inside one gateway without pretending
        # the adapter call and a persistence write can be exactly-once across
        # a process crash. Any durable async-delegation replay state remains
        # owned by tools.async_delegation, not a parallel gateway ledger.
        self._completion_delivery_lock = threading.Lock()
        self._completion_deliveries_inflight: set[tuple[str, str, object]] = set()
        self._completion_deliveries_delivered: "OrderedDict[tuple[str, str, object], None]" = OrderedDict()
        self._completion_delivery_retention = 2048
        # Agent-triggered terminal completions from one conversation often land
        # in the same scheduler tick.  Hold them briefly so the agent receives
        # one synthetic turn instead of one turn per process (#70300).
        self._completion_notification_batches: dict[tuple[str, ...], list[tuple[str, dict, asyncio.Future]]] = {}
        self._completion_notification_batch_tasks: dict[tuple[str, ...], asyncio.Task] = {}
        self._completion_notification_batch_flush_tasks: set[asyncio.Task] = set()
        self._completion_notification_batch_window = 0.1
        self._completion_notification_batches_stopping = False

        # Cache AIAgent instances per session to preserve prompt caching.
        # Without this, a new AIAgent is created per message, rebuilding the
        # system prompt (including memory) every turn — breaking prefix cache
        # and costing ~10x more on providers with prompt caching (Anthropic).
        # Key: session_key, Value: (AIAgent, config_signature_str)
        #
        # OrderedDict so _enforce_agent_cache_cap() can pop the least-recently-
        # used entry (move_to_end() on cache hits, popitem(last=False) for
        # eviction).  Hard cap via _AGENT_CACHE_MAX_SIZE, idle TTL enforced
        # from _session_expiry_watcher().
        import threading as _threading
        self._agent_cache: "OrderedDict[str, tuple]" = OrderedDict()
        self._agent_cache_lock = _threading.Lock()

        # Conversation-scoped per-session state (/model, /model --once,
        # /reasoning, /fast overrides; per-turn sidecar notes; ephemeral
        # context pin; last-delivered voice-channel context) lives on
        # SessionState.conversation — see gateway/session_state.py.
        self._kanban_notifier_profile = self._active_profile_name()
        # Teams meeting pipeline runtime (bound later when msgraph_webhook adapter exists).
        self._teams_pipeline_runtime = None
        self._teams_pipeline_runtime_error: Optional[str] = None
        # Pending exec approvals live on SessionState.persistent.approvals.

        # Track platforms that failed to connect for background reconnection.
        # Key: Platform enum, Value: {"config": platform_config, "attempts": int, "next_retry": float}
        self._failed_platforms: Dict[Platform, Dict[str, Any]] = {}

        # Strong refs to detached fatal-error handler tasks (see
        # _handle_adapter_fatal_error) so the event loop can't GC them mid-run.
        self._fatal_handler_tasks: set = set()

        # Pending /update prompt flags live on
        # SessionState.persistent.update_prompt_pending.

        # Slash-confirm state lives in tools.slash_confirm (module-level),
        # so platform adapters can resolve callbacks without a backref to
        # this runner.  Keep a local counter for confirm_id generation so
        # IDs stay compact (button callback_data has a 64-byte cap on
        # some platforms).
        import itertools as _itertools
        self._slash_confirm_counter = _itertools.count(1)

        # Persistent Honcho managers keyed by gateway session key.
        # This preserves write_frequency="session" semantics across short-lived
        # per-message AIAgent instances.



        # Ensure tirith security scanner is available (downloads if needed)
        try:
            from tools.tirith_security import ensure_installed
            ensure_installed(log_failures=False)
        except Exception:
            pass  # Non-fatal — fail-open at scan time if unavailable

        # Startup heads-up (#30882): a gateway in manual approval mode with no
        # automated risk assessor (tirith disabled AND no auxiliary.approval
        # model) can only gate dangerous commands / execute_code scripts via
        # live in-chat approval. With approval routing fixed, those actions now
        # fail closed (block) rather than silently auto-running — surface that
        # so operators knowingly enable tirith or configure auxiliary.approval
        # for unattended gateways.
        try:
            from hermes_cli.config import load_config as _load_full_config
            _appr_cfg = _load_full_config()
            _appr_mode = str(
                cfg_get(_appr_cfg, "approvals", "mode", default="manual") or "manual"
            ).strip().lower()
            _tirith_on = bool(cfg_get(_appr_cfg, "security", "tirith_enabled", default=True))
            _aux_approval = cfg_get(_appr_cfg, "auxiliary", "approval", default=None)
            if _appr_mode == "manual" and not _tirith_on and not _aux_approval:
                logger.warning(
                    "Gateway approvals.mode=manual with no automated risk "
                    "assessor (security.tirith_enabled is false and "
                    "auxiliary.approval is unset): dangerous commands and "
                    "execute_code scripts will BLOCK until a human approves "
                    "them in chat. Enable security.tirith_enabled or configure "
                    "auxiliary.approval for unattended operation."
                )
        except Exception:
            logger.debug("approvals.mode startup check skipped", exc_info=True)

        # Initialize session database for session_search tool support
        self._session_db = None
        try:
            from hermes_state import AsyncSessionDB, SessionDB
            self._session_db = AsyncSessionDB(SessionDB())
        except Exception as e:
            # WARNING (not DEBUG) so the failure appears in errors.log — matches
            # cli.py's handling of the same init path.  Users hitting NFS-mounted
            # HERMES_HOME silently lost /resume, /title, /history, /branch, and
            # session search without this.  The underlying cause (usually
            # "locking protocol" from NFS) is now also captured by
            # hermes_state.get_last_init_error() for slash-command error strings.
            logger.warning("SQLite session store not available: %s", e)

        # Opportunistic state.db maintenance: prune ended sessions inactive
        # for sessions.retention_days + optional VACUUM. Tracks last-run
        # in state_meta so it only actually executes once per
        # sessions.min_interval_hours.  Gateway is long-lived so blocking
        # a few seconds once per day is acceptable; failures are logged
        # but never raised.
        if self._session_db is not None:
            try:
                from hermes_cli.config import load_config as _load_full_config
                _sess_cfg = (_load_full_config().get("sessions") or {})
                # Non-destructive stale-session archive, independent of prune.
                if _sess_cfg.get("auto_archive", False):
                    self._session_db._db.maybe_auto_archive(
                        idle_days=float(_sess_cfg.get("auto_archive_days", 3)),
                        min_interval_hours=int(_sess_cfg.get("min_interval_hours", 24)),
                    )
                if _sess_cfg.get("auto_prune", False):
                    # Construction-time, before the loop serves traffic; sync DB is fine.
                    self._session_db._db.maybe_auto_prune_and_vacuum(
                        retention_days=int(_sess_cfg.get("retention_days", 90)),
                        min_interval_hours=int(_sess_cfg.get("min_interval_hours", 24)),
                        min_vacuum_interval_days=int(
                            _sess_cfg.get("min_vacuum_interval_days", 30)
                        ),
                        vacuum=bool(_sess_cfg.get("vacuum_after_prune", True)),
                        sessions_dir=self.config.sessions_dir,
                    )
            except Exception as exc:
                logger.debug("state.db auto-maintenance skipped: %s", exc)

        # Opportunistic shadow-repo cleanup — deletes stale checkpoint repos
        # under ~/.hermes/checkpoints/.  Opt-in via checkpoints.auto_prune,
        # idempotent via .last_prune marker.
        try:
            from hermes_cli.config import load_config as _load_full_config
            _ckpt_cfg = (_load_full_config().get("checkpoints") or {})
            if _ckpt_cfg.get("auto_prune", False):
                from tools.checkpoint_manager import maybe_auto_prune_checkpoints
                # delete_orphans is intentionally never honoured here: a
                # missing workdir at startup is ambiguous (deleted project
                # vs. an unmounted external volume / network share / VPN
                # not yet up) and this sweep runs unattended. Orphan cleanup
                # is only ever done via the explicit `hermes checkpoints
                # prune` command, which the user has to invoke.
                maybe_auto_prune_checkpoints(
                    retention_days=int(_ckpt_cfg.get("retention_days", 7)),
                    min_interval_hours=int(_ckpt_cfg.get("min_interval_hours", 24)),
                    delete_orphans=False,
                    max_total_size_mb=int(_ckpt_cfg.get("max_total_size_mb", 500)),
                )
        except Exception as exc:
            logger.debug("checkpoint auto-maintenance skipped: %s", exc)

        # DM pairing store for code-based user authorization.
        # ``pairing_store`` stays as the global/default store for the
        # ``hermes pairing`` CLI and any caller without a profile context.
        # ``pairing_stores`` is the per-profile map used by
        # ``authz_mixin._is_user_authorized`` to route checks to the right
        # whitelist (one per profile in multiplex mode).
        from gateway.pairing import PairingStore
        self.pairing_store = PairingStore()
        self.pairing_stores: Dict[str, "PairingStore"] = {}
        
        # Event hook system
        from gateway.hooks import HookRegistry
        self.hooks = HookRegistry()

        # Per-chat voice reply mode: "off" | "voice_only" | "all"
        self._voice_mode: Dict[str, str] = self._load_voice_modes()
        # Recent voice transcripts per (guild,user) for duplicate suppression.
        # Protects against the same utterance being emitted twice by the voice
        # capture / STT pipeline, which otherwise produces a second delayed reply.
        self._recent_voice_transcripts: Dict[tuple[int, int], List[tuple[float, str]]] = {}

        # Track background tasks to prevent garbage collection mid-execution
        self._background_tasks: set = set()

        # Event-loop liveness heartbeat (#66892): rewritten every 30s while
        # the loop is dispatching. External supervisors use the file mtime /
        # updated_at to distinguish "process alive" from "loop frozen".
        self._gateway_started_at: float = time.time()
        self._loop_heartbeat_task: Optional[asyncio.Task] = None
        self._loop_floor_timer_handle = None
        self._loop_liveness_watchdog = None

        # scale-to-zero (Phase 0, F13): gateway-scoped "last inbound seen" clock.
        # There is no such clock today (only a per-agent _last_activity_ts), so the
        # idle predicate needs this. Stamped in _handle_message (the single inbound
        # chokepoint all adapters call); seeded to "now" so a fresh gateway isn't
        # considered idle from epoch. The scale-to-zero watcher (started only when
        # the instance is opted in + relay-only + has a wakeUrl) reads it.
        self._last_inbound_at: float = time.time()
        # Set after a wake (re-arm cooldown, 0.F) so we don't immediately re-go
        # dormant before the drained backlog has a chance to update the clock.
        self._scale_to_zero_cooldown_until: float = 0.0


    def _wire_teams_pipeline_runtime(self) -> None:
        """Bind the Teams meeting pipeline runtime to Graph webhook ingress.

        No-op when the msgraph_webhook adapter isn't running or the
        teams_pipeline plugin isn't enabled — lets the gateway start cleanly
        whether or not the user has opted into the pipeline.
        """
        from gateway.run import _teams_pipeline_plugin_enabled, logger
        if Platform.MSGRAPH_WEBHOOK not in self.adapters:
            return
        if not _teams_pipeline_plugin_enabled():
            logger.debug("Teams pipeline plugin is disabled; skipping runtime wiring")
            return
        try:
            from plugins.teams_pipeline.runtime import bind_gateway_runtime
        except Exception as exc:
            logger.warning("Teams pipeline runtime import failed: %s", exc)
            return
        try:
            bound = bind_gateway_runtime(self)
        except Exception as exc:
            logger.warning("Teams pipeline runtime wiring failed: %s", exc)
            return
        if bound:
            logger.info("Teams pipeline runtime bound to msgraph webhook ingress")
        elif self._teams_pipeline_runtime_error:
            logger.warning(
                "Teams pipeline runtime unavailable: %s",
                self._teams_pipeline_runtime_error,
            )


    def _warn_if_docker_media_delivery_is_risky(self) -> None:
        """Warn when Docker-backed gateways lack an explicit export mount.

        MEDIA delivery happens in the gateway process, so paths emitted by the model
        must be readable from the host. A plain container-local path like
        `/workspace/report.txt` or `/output/report.txt` often exists only inside
        Docker, so users commonly need a dedicated export mount such as
        `host-dir:/output`.
        """
        from gateway.run import _DOCKER_MEDIA_OUTPUT_CONTAINER_PATHS, _DOCKER_VOLUME_SPEC_RE, logger
        if os.getenv("TERMINAL_ENV", "").strip().lower() != "docker":
            return

        connected = self.config.get_connected_platforms()
        messaging_platforms = [p for p in connected if p not in {Platform.LOCAL, Platform.API_SERVER, Platform.WEBHOOK}]
        if not messaging_platforms:
            return

        raw_volumes = os.getenv("TERMINAL_DOCKER_VOLUMES", "").strip()
        volumes: List[str] = []
        if raw_volumes:
            try:
                parsed = json.loads(raw_volumes)
                if isinstance(parsed, list):
                    volumes = [str(v) for v in parsed if isinstance(v, str)]
            except Exception:
                logger.debug("Could not parse TERMINAL_DOCKER_VOLUMES for gateway media warning", exc_info=True)

        has_explicit_output_mount = False
        for spec in volumes:
            match = _DOCKER_VOLUME_SPEC_RE.match(spec)
            if not match:
                continue
            container_path = match.group("container")
            if container_path in _DOCKER_MEDIA_OUTPUT_CONTAINER_PATHS:
                has_explicit_output_mount = True
                break

        if has_explicit_output_mount:
            return

        logger.warning(
            "Docker backend is enabled for the messaging gateway but no explicit host-visible "
            "output mount (for example '/home/user/.hermes/cache/documents:/output') is configured. "
            "This is fine if the model already emits host-visible paths, but MEDIA file delivery can fail "
            "for container-local paths like '/workspace/...' or '/output/...'."
        )



    # -- Setup skill availability ----------------------------------------

    def _has_setup_skill(self) -> bool:
        """Check if the hermes-agent-setup skill is installed."""
        try:
            from tools.skill_manager_tool import _find_skill
            return _find_skill("hermes-agent-setup") is not None
        except Exception:
            return False

    # -- Voice mode persistence ------------------------------------------

    _VOICE_MODE_PATH = _hermes_home / "gateway_voice_mode.json"

    def _voice_key(self, platform: Platform, chat_id: str) -> str:
        """Return a platform-namespaced key for voice mode state."""
        return f"{platform.value}:{chat_id}"

    def _load_voice_modes(self) -> Dict[str, str]:
        from gateway.run import logger
        try:
            data = json.loads(self._VOICE_MODE_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

        if not isinstance(data, dict):
            return {}

        valid_modes = {"off", "voice_only", "all"}
        result = {}
        for chat_id, mode in data.items():
            if mode not in valid_modes:
                continue
            key = str(chat_id)
            # Skip legacy unprefixed keys (warn and skip)
            if ":" not in key:
                logger.warning(
                    "Skipping legacy unprefixed voice mode key %r during migration. "
                    "Re-enable voice mode on that chat to rebuild the prefixed key.",
                    key,
                )
                continue
            result[key] = mode
        return result

    def _save_voice_modes(self) -> None:
        from gateway.run import logger
        try:
            self._VOICE_MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._VOICE_MODE_PATH.write_text(
                json.dumps(self._voice_mode, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.warning("Failed to save voice modes: %s", e)

    def _set_adapter_auto_tts_disabled(self, adapter, chat_id: str, disabled: bool) -> None:
        """Update an adapter's in-memory auto-TTS suppression set if present."""
        disabled_chats = getattr(adapter, "_auto_tts_disabled_chats", None)
        if not isinstance(disabled_chats, set):
            return
        if disabled:
            disabled_chats.add(chat_id)
            # ``/voice off`` also clears any explicit enable — it's a hard override.
            enabled_chats = getattr(adapter, "_auto_tts_enabled_chats", None)
            if isinstance(enabled_chats, set):
                enabled_chats.discard(chat_id)
        else:
            disabled_chats.discard(chat_id)

    def _set_adapter_auto_tts_enabled(self, adapter, chat_id: str, enabled: bool) -> None:
        """Update an adapter's per-chat auto-TTS opt-in set if present.

        Used for ``/voice on``/``/voice tts`` where the user explicitly wants
        auto-TTS even when ``voice.auto_tts`` is False globally.
        """
        enabled_chats = getattr(adapter, "_auto_tts_enabled_chats", None)
        if not isinstance(enabled_chats, set):
            return
        if enabled:
            enabled_chats.add(chat_id)
            # An explicit opt-in clears any stale /voice off for this chat.
            disabled_chats = getattr(adapter, "_auto_tts_disabled_chats", None)
            if isinstance(disabled_chats, set):
                disabled_chats.discard(chat_id)
        else:
            enabled_chats.discard(chat_id)

    def _sync_voice_mode_state_to_adapter(self, adapter) -> None:
        """Restore persisted /voice state into a live platform adapter.

        Populates three fields from config + ``self._voice_mode``:
          - ``_auto_tts_default``: global default from ``voice.auto_tts``
          - ``_auto_tts_enabled_chats``: chats with mode ``voice_only``/``all``
          - ``_auto_tts_disabled_chats``: chats with mode ``off``
        """
        platform = getattr(adapter, "platform", None)
        if not isinstance(platform, Platform):
            return

        disabled_chats = getattr(adapter, "_auto_tts_disabled_chats", None)
        enabled_chats = getattr(adapter, "_auto_tts_enabled_chats", None)
        if not isinstance(disabled_chats, set) and not isinstance(enabled_chats, set):
            return

        # Push the global voice.auto_tts default (config.yaml) onto the adapter.
        # Lazy import to avoid adding a module-level dep from gateway → hermes_cli.
        try:
            from hermes_cli.config import load_config as _load_full_config
            _full_cfg = _load_full_config()
            _auto_tts_default = bool(
                (_full_cfg.get("voice") or {}).get("auto_tts", False)
            )
        except Exception:
            _auto_tts_default = False
        if hasattr(adapter, "_auto_tts_default"):
            adapter._auto_tts_default = _auto_tts_default

        prefix = f"{platform.value}:"
        if isinstance(disabled_chats, set):
            disabled_chats.clear()
            disabled_chats.update(
                key[len(prefix):] for key, mode in self._voice_mode.items()
                if mode == "off" and key.startswith(prefix)
            )
        if isinstance(enabled_chats, set):
            enabled_chats.clear()
            enabled_chats.update(
                key[len(prefix):] for key, mode in self._voice_mode.items()
                if mode in {"voice_only", "all"} and key.startswith(prefix)
            )

    async def _await_adapter_cleanup_with_timeout(
        self, awaitable: Awaitable[Any], timeout: float
    ) -> bool:
        """Wait for adapter cleanup without letting cancellation swallowing hang us.

        ``asyncio.wait_for`` cancels an overdue child but then waits for it to
        exit. An adapter close path that catches ``CancelledError`` can therefore
        block recovery forever. Keep ownership of the old task through its done
        callback, but release the runner at the deadline.
        """
        if timeout <= 0:
            await awaitable
            return True

        task = asyncio.ensure_future(awaitable)
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(consume_detached_task_result)
            raise
        if task in done:
            await task
            return True

        task.cancel()
        task.add_done_callback(consume_detached_task_result)
        return False

    async def _safe_adapter_disconnect(self, adapter, platform) -> None:
        """Call adapter.disconnect() defensively, swallowing any error.

        Used when adapter.connect() failed or raised — the adapter may
        have allocated partial resources (aiohttp.ClientSession, poll
        tasks, child subprocesses) that would otherwise leak and surface
        as "Unclosed client session" warnings at process exit.

        Must tolerate partial-init state and never raise, since callers
        use it inside error-handling blocks.
        """
        from gateway.run import logger
        timeout = self._adapter_disconnect_timeout_secs()
        try:
            completed = await self._await_adapter_cleanup_with_timeout(
                adapter.disconnect(), timeout
            )
            if not completed:
                logger.warning(
                    "Timed out after %.1fs while disconnecting %s adapter; continuing shutdown",
                    timeout,
                    platform.value if platform is not None else "adapter",
                )
        except Exception as e:
            logger.debug(
                "Defensive %s disconnect after failed connect raised: %s",
                platform.value if platform is not None else "adapter",
                e,
            )

    async def _bounded_adapter_teardown(
        self, adapter, platform, *, profile: Optional[str] = None
    ) -> None:
        """Tear down one adapter on the shutdown path with bounded awaits.

        Both ``cancel_background_tasks()`` and ``disconnect()`` can block
        indefinitely when a platform's network state is half-dead (e.g. a
        wedged Feishu/Lark WebSocket thread waiting on I/O). An unbounded
        await here stalls the entire shutdown sequence past systemd's
        ``TimeoutStopSec``; the resulting SIGKILL skips ``atexit`` PID-file
        cleanup, so the next start dies with "PID file race lost" (#14128).

        Each await uses the existing per-adapter timeout budget
        (``HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT``). On timeout the old
        task is cancelled and detached, then teardown forces forward progress;
        the loop never hangs even if an adapter swallows cancellation. Never
        raises.
        """
        from gateway.run import logger
        timeout = self._adapter_disconnect_timeout_secs()
        suffix = f" (profile: {profile})" if profile else ""
        started_at = time.monotonic()
        try:
            cancelled = await self._await_adapter_cleanup_with_timeout(
                adapter.cancel_background_tasks(), timeout
            )
            if not cancelled:
                logger.warning(
                    "✗ %s background-task cancel timed out after %.1fs - forcing continue%s",
                    platform.value, timeout, suffix,
                )
        except Exception as e:
            logger.debug("✗ %s background-task cancel error%s: %s", platform.value, suffix, e)
        try:
            disconnected = await self._await_adapter_cleanup_with_timeout(
                adapter.disconnect(), timeout
            )
            if disconnected:
                logger.info(
                    "✓ %s disconnected (%.2fs)%s",
                    platform.value, time.monotonic() - started_at, suffix,
                )
            else:
                logger.warning(
                    "✗ %s disconnect timed out after %.1fs - forcing continue%s",
                    platform.value, timeout, suffix,
                )
        except Exception as e:
            logger.error(
                "✗ %s disconnect error after %.2fs%s: %s",
                platform.value, time.monotonic() - started_at, suffix, e,
            )

    def _adapter_disconnect_timeout_secs(self) -> float:
        """Return the per-adapter disconnect timeout used during shutdown."""
        from gateway.run import _ADAPTER_DISCONNECT_TIMEOUT_SECS_DEFAULT, logger
        raw = os.getenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "").strip()
        if raw:
            try:
                timeout = float(raw)
            except ValueError:
                logger.warning(
                    "Ignoring invalid HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT=%r",
                    raw,
                )
            else:
                return max(0.0, timeout)
        return _ADAPTER_DISCONNECT_TIMEOUT_SECS_DEFAULT

    def _platform_connect_timeout_secs(self, platform=None) -> float:
        """Return the per-platform connect timeout used during startup/retry."""
        from gateway.run import _PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT, _TELEGRAM_CONNECT_TIMEOUT_SECS_DEFAULT, logger
        raw = os.getenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", "").strip()
        if raw:
            try:
                timeout = float(raw)
            except ValueError:
                logger.warning(
                    "Ignoring invalid HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT=%r",
                    raw,
                )
            else:
                return max(0.0, timeout)
        if platform == Platform.TELEGRAM:
            return _TELEGRAM_CONNECT_TIMEOUT_SECS_DEFAULT
        return _PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT

    async def _connect_adapter_with_timeout(
        self, adapter, platform, *, is_reconnect: bool = False
    ) -> bool:
        """Connect an adapter without allowing one platform to block others.

        ``is_reconnect`` is forwarded to ``adapter.connect()`` so platform
        adapters can distinguish a cold first boot (drop any stale
        server-side queue) from a watcher reconnect after a prolonged outage
        (preserve the queue so messages sent during the outage are delivered
        rather than silently dropped — #46621).
        """
        timeout = self._platform_connect_timeout_secs(platform)
        if timeout <= 0:
            return await adapter.connect(is_reconnect=is_reconnect)
        # Use the detach-on-timeout pattern instead of plain asyncio.wait_for:
        # asyncio.wait_for cancels the overdue task but then waits for it to
        # exit. An adapter connect() that catches CancelledError can therefore
        # block recovery forever (the watcher never reaches the next retry).
        # Keep ownership of the old task through its done callback, but
        # release the runner at the deadline (#70344).
        task = asyncio.ensure_future(
            adapter.connect(is_reconnect=is_reconnect)
        )
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(consume_detached_task_result)
            raise
        if task in done:
            result = await task
            return bool(result)
        task.cancel()
        task.add_done_callback(consume_detached_task_result)
        raise TimeoutError(
            f"{platform.value} connect timed out after {timeout:g}s"
        )

    async def _connect_initial_adapter_with_timeout(self, adapter, platform) -> bool:
        """Connect one cold-start adapter with tightly scoped replace intent.

        The capability is visible only while this initial connect is awaited.
        Reconnects call ``_connect_adapter_with_timeout`` directly and adapters
        also default to deny, so a later network recovery can never evict a
        healthy token holder.
        """
        adapter._platform_lock_takeover_allowed = bool(
            self._platform_lock_takeover_on_start
        )
        try:
            return await self._connect_adapter_with_timeout(adapter, platform)
        finally:
            adapter._platform_lock_takeover_allowed = False

    @property
    def should_exit_cleanly(self) -> bool:
        return self._exit_cleanly

    @property
    def should_exit_with_failure(self) -> bool:
        return self._exit_with_failure

    @property
    def exit_reason(self) -> Optional[str]:
        return self._exit_reason

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit_code

    def _session_key_for_source(self, source: SessionSource) -> str:
        """Resolve the current session key for a source, honoring gateway config when available."""
        if hasattr(self, "session_store") and self.session_store is not None:
            try:
                session_key = self.session_store._generate_session_key(source)
                if isinstance(session_key, str) and session_key:
                    return session_key
            except Exception:
                pass
        config = getattr(self, "config", None)
        # Mirror SessionStore._resolve_profile_for_key so this fallback path
        # produces the same namespace as the primary path: None (legacy
        # agent:main) unless multiplexing is on, then the active profile.
        _profile = None
        if getattr(config, "multiplex_profiles", False):
            if source.profile:
                _profile = source.profile
            else:
                try:
                    from hermes_cli.profiles import get_active_profile_name
                    _profile = get_active_profile_name() or "default"
                except Exception:
                    _profile = None
        return build_session_key(
            source,
            group_sessions_per_user=getattr(config, "group_sessions_per_user", True),
            thread_sessions_per_user=getattr(config, "thread_sessions_per_user", False),
            profile=_profile,
        )

    def _telegram_topic_mode_enabled(self, source: SessionSource) -> bool:
        """Return whether Telegram DM topic mode is active for this chat."""
        from gateway.run import logger
        if source.platform != Platform.TELEGRAM or source.chat_type != "dm":
            return False
        session_db = getattr(self, "_session_db", None)
        if session_db is None:
            return False
        # Runs off-loop (always via asyncio.to_thread); use the sync handle.
        session_db = getattr(session_db, "_db", session_db)
        try:
            raw = session_db.is_telegram_topic_mode_enabled(
                chat_id=str(source.chat_id),
                user_id=str(source.user_id),
            )
        except Exception:
            logger.debug("Failed to read Telegram topic mode state", exc_info=True)
            return False
        # Only honor a real True from the SessionDB. Any other value
        # (including MagicMock instances from test fixtures that didn't
        # opt into topic mode) means topic mode is off for this chat.
        return raw is True

    # Telegram's General (pinned top) topic in forum-enabled private chats.
    # Bot API behavior varies: some clients omit message_thread_id for
    # General, others send "1". Treat both as "root" for lobby/lane purposes.
    _TELEGRAM_GENERAL_TOPIC_IDS = frozenset({"", "1"})

    def _is_telegram_topic_root_lobby(self, source: SessionSource) -> bool:
        """True for the main Telegram DM (or General topic) when topic mode has made it a lobby."""
        if source.platform != Platform.TELEGRAM or source.chat_type != "dm":
            return False
        if not self._telegram_topic_mode_enabled(source):
            return False
        tid = str(source.thread_id or "")
        return tid in self._TELEGRAM_GENERAL_TOPIC_IDS

    def _is_telegram_topic_lane(self, source: SessionSource) -> bool:
        """True for a user-created Telegram private-chat topic lane."""
        if source.platform != Platform.TELEGRAM or source.chat_type != "dm":
            return False
        if not self._telegram_topic_mode_enabled(source):
            return False
        tid = str(source.thread_id or "")
        if not tid or tid in self._TELEGRAM_GENERAL_TOPIC_IDS:
            return False
        return True

    _TELEGRAM_LOBBY_REMINDER_COOLDOWN_S = 30.0

    def _should_send_telegram_lobby_reminder(self, source: SessionSource) -> bool:
        """Rate-limit root-DM lobby reminders to one message per cooldown window.

        A user who forgets multi-session mode is enabled and types several
        prompts in the root DM would otherwise get a reminder for every
        message. Cap it so the first one lands and the rest stay quiet.
        """
        if not hasattr(self, "_telegram_lobby_reminder_ts"):
            self._telegram_lobby_reminder_ts = {}
        chat_id = str(source.chat_id or "")
        if not chat_id:
            return True
        import time as _time
        now = _time.monotonic()
        last = self._telegram_lobby_reminder_ts.get(chat_id, 0.0)
        if now - last < self._TELEGRAM_LOBBY_REMINDER_COOLDOWN_S:
            return False
        self._telegram_lobby_reminder_ts[chat_id] = now
        return True

    def _telegram_topic_root_lobby_message(self) -> str:
        return (
            "This main chat is reserved for system commands.\n\n"
            "To start a new Hermes chat, open the All Messages topic at the top "
            "of this bot interface and send any message there. Telegram will "
            "create a new topic for that message; each topic works as an "
            "independent Hermes session."
        )

    def _telegram_topic_root_new_message(self) -> str:
        return (
            "To start a new parallel Hermes chat, open the All Messages topic "
            "at the top of this bot interface and send any message there. "
            "Telegram will create a new topic for it.\n\n"
            "Each topic is an independent Hermes session. Use /new inside an "
            "existing topic only if you want to replace that topic's current session."
        )

    def _telegram_topic_new_header(self, source: SessionSource) -> Optional[str]:
        if not self._is_telegram_topic_lane(source):
            return None
        return (
            "Started a new Hermes session in this topic.\n\n"
            "Tip: for parallel work, open All Messages and send a message there "
            "to create a separate topic instead of using /new here. /new replaces "
            "the session attached to the current topic."
        )

    def _record_telegram_topic_binding(
        self,
        source: SessionSource,
        session_entry,
    ) -> None:
        """Persist the Telegram topic -> Hermes session binding for topic lanes."""
        session_db = getattr(self, "_session_db", None)
        if session_db is None or not source.chat_id or not source.thread_id:
            return
        # Runs off-loop (always via asyncio.to_thread); use the sync handle.
        session_db = getattr(session_db, "_db", session_db)
        session_db.bind_telegram_topic(
            chat_id=str(source.chat_id),
            thread_id=str(source.thread_id),
            user_id=str(source.user_id or ""),
            session_key=session_entry.session_key,
            session_id=session_entry.session_id,
        )

    def _sync_telegram_topic_binding(
        self,
        source: SessionSource,
        session_entry,
        *,
        reason: str,
    ) -> None:
        """Update the topic binding to point at ``session_entry.session_id``.

        Telegram topic lanes persist a (chat_id, thread_id) -> session_id row
        so reopening a topic in a fresh process resumes the right Hermes
        session. When compression rotates ``session_entry.session_id`` mid-turn,
        the binding goes stale and the next inbound message in that topic
        reloads the oversized parent transcript instead of the compressed
        child, retriggering preflight compression — sometimes in a loop
        (#20470, #29712, #33414).
        """
        from gateway.run import logger
        if not self._is_telegram_topic_lane(source):
            return
        try:
            self._record_telegram_topic_binding(source, session_entry)
        except Exception:
            logger.debug(
                "telegram topic binding refresh failed (%s)", reason, exc_info=True,
            )

    def _recover_telegram_topic_thread_id(
        self,
        source: SessionSource,
    ) -> Optional[str]:
        """Pin DM-topic routing to the user's last-active topic.

        Telegram can omit ``message_thread_id`` or surface General (``1``)
        for some topic-mode DM replies. In those lobby-shaped cases, keep the
        conversation attached to the user's most-recent bound topic.

        Do not rewrite a non-lobby, previously-unbound thread id: a newly
        created Telegram DM topic is also "unknown" until the first inbound
        message is recorded, and rewriting it would send that brand-new topic's
        answer into an older lane. Returns None to leave the source alone.
        """
        from gateway.run import logger
        if (
            source.platform != Platform.TELEGRAM
            or source.chat_type != "dm"
            or not source.chat_id
            or not source.user_id
            or not self._telegram_topic_mode_enabled(source)
        ):
            return None
        inbound = str(source.thread_id or "")
        is_lobby = not inbound or inbound in self._TELEGRAM_GENERAL_TOPIC_IDS
        if not is_lobby:
            # A non-lobby, unknown thread_id is most likely the first message in
            # a brand-new Telegram DM topic. Preserve it so it can be recorded
            # as a new independent lane below instead of hijacking the latest
            # existing topic binding.
            return None
        session_db = getattr(self, "_session_db", None)
        if session_db is None:
            return None
        # Runs off-loop (always via asyncio.to_thread); use the sync handle.
        session_db = getattr(session_db, "_db", session_db)
        try:
            bindings = session_db.list_telegram_topic_bindings_for_chat(
                chat_id=str(source.chat_id),
            )
        except Exception:
            logger.debug("topic-recover: read failed", exc_info=True)
            return None
        if not bindings:
            return None
        user_id = str(source.user_id)
        for b in bindings:  # newest-first
            if str(b.get("user_id") or "") == user_id:
                recovered = str(b.get("thread_id") or "")
                if recovered and recovered != inbound:
                    return recovered
                return None
        return None

    def _normalize_source_for_session_key(
        self,
        source: SessionSource,
    ) -> SessionSource:
        """Apply Telegram DM topic recovery to a source for session-key purposes.

        ``_handle_message_with_agent`` rewrites ``source.thread_id`` via
        ``_recover_telegram_topic_thread_id`` *before* deriving the session
        key for a normal message turn (a lobby/stripped reply gets pinned to
        the user's last-active topic).  Session-scoped command handlers like
        ``/model`` and ``/reasoning`` derive their override key from the raw
        inbound ``event.source``, which skips that recovery — so the override
        is stored under a different key than the next message turn reads,
        and the override is silently dropped on Telegram forum topics and
        after compression session splits (#30479).

        Returns a recovery-normalized copy when a rewrite applies, otherwise
        the original source unchanged.  Always derive the override storage key
        from the result so storage and read use an identical key.
        """
        try:
            recovered = self._recover_telegram_topic_thread_id(source)
        except Exception:
            return source
        if recovered is None:
            return source
        return dataclasses.replace(source, thread_id=recovered)

    def _resolve_session_agent_runtime(
        self,
        *,
        source: Optional[SessionSource] = None,
        session_key: Optional[str] = None,
        user_config: Optional[dict] = None,
    ) -> tuple[str, dict]:
        """Resolve model/runtime for a session.

        Priority (highest first): session ``/model`` → ``channel_overrides`` →
        global config/env (``_resolve_gateway_model(user_config)`` and default
        provider resolution).
        """
        from gateway.run import _credential_pool_for_provider, _get_channel_override, _resolve_gateway_model, _resolve_runtime_agent_kwargs, _resolve_runtime_agent_kwargs_for_provider, logger
        resolved_session_key = session_key
        if not resolved_session_key and source is not None:
            try:
                resolved_session_key = self._session_key_for_source(source)
            except Exception:
                resolved_session_key = None

        model = _resolve_gateway_model(user_config)
        if resolved_session_key:
            self._rehydrate_session_model_override(resolved_session_key)
        _override_state = (
            self._peek_session_state(resolved_session_key)
            if resolved_session_key
            else None
        )
        override = (
            _override_state.conversation.model_override if _override_state else None
        )
        if override:
            override_model = override.get("model", model)
            override_runtime = {
                "provider": override.get("provider"),
                "api_key": override.get("api_key"),
                "base_url": override.get("base_url"),
                "api_mode": override.get("api_mode"),
                "max_tokens": override.get("max_tokens"),
                "credential_pool": override.get("credential_pool"),
            }
            if override_runtime.get("api_key"):
                if override_runtime.get("credential_pool") is None:
                    override_runtime["credential_pool"] = _credential_pool_for_provider(
                        override.get("provider")
                    )
                logger.debug(
                    "Session model override (fast): session=%s config_model=%s -> override_model=%s provider=%s",
                    resolved_session_key or "", model, override_model,
                    override_runtime.get("provider"),
                )
                return override_model, override_runtime
            # Override exists but has no api_key — fall through to env-based
            # resolution and apply model/provider from the override on top.
            logger.debug(
                "Session model override (no api_key, fallback): session=%s config_model=%s override_model=%s",
                resolved_session_key or "", model, override_model,
            )
        else:
            logger.debug(
                "No session model override: session=%s config_model=%s override_keys=%s",
                resolved_session_key or "", model,
                [
                    _key
                    for _key, _st in list(self._sessions_map().items())
                    if _st.conversation.model_override is not None
                ][:5] or "[]",
            )

        runtime_kwargs = _resolve_runtime_agent_kwargs()
        runtime_model = runtime_kwargs.pop("model", None)
        if runtime_model:
            logger.info(
                "Runtime provider supplied explicit model override: %s -> %s",
                model,
                runtime_model,
            )
            model = runtime_model

        cfg = getattr(self, "config", None)
        if cfg and source is not None:
            chat_id = str(source.chat_id) if source.chat_id else ""
            thread_id = (
                str(source.thread_id) if getattr(source, "thread_id", None) else None
            )
            parent_id = (
                str(source.parent_chat_id)
                if getattr(source, "parent_chat_id", None)
                else None
            )
            ch = _get_channel_override(
                cfg,
                source.platform,
                chat_id,
                thread_id=thread_id,
                parent_id=parent_id,
            )
            if ch:
                if ch.model:
                    model = ch.model
                if ch.provider:
                    runtime_kwargs = _resolve_runtime_agent_kwargs_for_provider(
                        ch.provider
                    )
                    ch_runtime_model = runtime_kwargs.pop("model", None)
                    # Only adopt the provider's bundled model when the override
                    # did not specify an explicit model.
                    if ch_runtime_model and not ch.model:
                        model = ch_runtime_model

        if override and resolved_session_key:
            model, runtime_kwargs = self._apply_session_model_override(
                resolved_session_key, model, runtime_kwargs
            )

        # When the config has no model.default but a provider was resolved
        # (e.g. user ran `hermes auth add openai-codex` without `hermes model`),
        # fall back to the provider's first catalog model so the API call
        # doesn't fail with "model must be a non-empty string".
        if not model and runtime_kwargs.get("provider"):
            try:
                from hermes_cli.models import get_default_model_for_provider
                model = get_default_model_for_provider(runtime_kwargs["provider"])
                if model:
                    logger.info(
                        "No model configured — defaulting to %s for provider %s",
                        model, runtime_kwargs["provider"],
                    )
            except Exception:
                pass

        # Final safety net (#35314): if resolution still produced an empty
        # model — e.g. a transient config-cache miss during a post-interrupt
        # recovery turn returned an empty user_config — reuse the last model we
        # successfully resolved for this session (or, failing that, the most
        # recent one resolved process-wide). Building an agent with model=""
        # makes every API call fail HTTP 400 "No models provided" and the
        # session goes silent until the user manually re-sends. ``getattr``
        # guards against bare test runners built via ``object.__new__``.
        if not model:
            _lr_state = (
                self._peek_session_state(resolved_session_key)
                if resolved_session_key
                else None
            )
            _lr_star = self._peek_session_state("*")
            _recovered = (
                (_lr_state.conversation.last_resolved_model if _lr_state else "")
                or (_lr_star.conversation.last_resolved_model if _lr_star else "")
            )
            if _recovered:
                logger.warning(
                    "Empty model resolved for session=%s — recovering "
                    "last-known-good model %s (config read likely returned "
                    "empty; see #35314)",
                    resolved_session_key or "", _recovered,
                )
                model = _recovered
        elif model:
            # Cache the good resolution for future recovery turns.
            if resolved_session_key:
                self._session_state(
                    resolved_session_key
                ).conversation.last_resolved_model = model
            self._session_state("*").conversation.last_resolved_model = model

        return model, runtime_kwargs

    def _resolve_turn_agent_config(self, user_message: str, model: str, runtime_kwargs: dict) -> dict:
        """Build the effective model/runtime config for a single turn.

        Always uses the session's primary model/provider.  If `/fast` is
        enabled and the model supports Priority Processing / Anthropic fast
        mode, attach `request_overrides` so the API call is marked
        accordingly.
        """
        from hermes_cli.models import resolve_fast_mode_overrides

        runtime = {
            "api_key": runtime_kwargs.get("api_key"),
            "base_url": runtime_kwargs.get("base_url"),
            "provider": runtime_kwargs.get("provider"),
            "requested_provider": runtime_kwargs.get("requested_provider"),
            "api_mode": runtime_kwargs.get("api_mode"),
            "command": runtime_kwargs.get("command"),
            "args": list(runtime_kwargs.get("args") or []),
            "credential_pool": runtime_kwargs.get("credential_pool"),
            "max_tokens": runtime_kwargs.get("max_tokens"),
        }
        route = {
            "model": model,
            "runtime": runtime,
            "signature": (
                model,
                runtime["provider"],
                runtime["requested_provider"],
                runtime["base_url"],
                runtime["api_mode"],
                runtime["command"],
                tuple(runtime["args"]),
            ),
        }

        service_tier = getattr(self, "_service_tier", None)
        if not service_tier:
            route["request_overrides"] = {}
            return route

        try:
            overrides = resolve_fast_mode_overrides(route["model"])
        except Exception:
            overrides = None
        route["request_overrides"] = overrides or {}
        return route

    def _sync_session_model_from_agent(self, session_id: str, agent: Any) -> None:
        """Persist the runtime model/provider actually used by a gateway turn.

        Provider fallback can switch ``agent.model``/``agent.provider`` after the
        session row was created. Keep the session DB metadata in sync so session
        lists, desktop/dashboard details, and follow-up session tooling report the
        backend that actually answered the latest turn.

        Called from the ``run_sync`` closure, which executes off the event loop
        in the executor thread — so the synchronous ``SessionDB`` (``_db``) is
        used directly rather than awaiting the AsyncSessionDB forwarder.
        """
        from gateway.run import logger
        if not session_id or agent is None or self._session_db is None:
            return
        model = getattr(agent, "model", None)
        if not model:
            return
        runtime = {
            "provider": getattr(agent, "provider", None),
            "base_url": getattr(agent, "base_url", None),
            "api_mode": getattr(agent, "api_mode", None),
            "fallback_active": bool(getattr(agent, "_fallback_activated", False)),
        }
        runtime = {k: v for k, v in runtime.items() if v not in (None, "")}

        try:
            db = self._session_db._db
            row = db.get_session(session_id)
            if not row:
                return
            current_model = row.get("model")
            raw_config = row.get("model_config")
            try:
                config = json.loads(raw_config) if raw_config else {}
            except Exception:
                config = {}
            if not isinstance(config, dict):
                config = {}
            gateway_runtime = dict(config.get("gateway_runtime") or {})
            if current_model == model and all(
                gateway_runtime.get(k) == v for k, v in runtime.items()
            ):
                return
            config["gateway_runtime"] = runtime
            db.update_session_meta(session_id, json.dumps(config), model=model)
        except Exception:
            logger.debug("Failed to sync gateway session model metadata", exc_info=True)

    async def _handle_reaction_event(self, ctx: Dict[str, Any]) -> None:
        """Fan a normalised platform reaction event out to the HookRegistry.

        Adapters call this via ``set_reaction_handler`` for every
        platform-native reaction event they surface. The adapter-supplied
        ``event_name`` ("reaction:added" / "reaction:removed") becomes the
        hook event so user hooks subscribe with the same name scheme as the
        existing ``agent:*`` family. Errors never block the adapter's event
        loop — the hook contract is non-blocking.
        """
        from gateway.run import logger
        event_name = str(ctx.get("event_name") or "reaction:added")
        try:
            await self.hooks.emit(event_name, ctx)
        except Exception:
            logger.debug("[Gateway] reaction hook emit failed", exc_info=True)

    async def _handle_adapter_fatal_error(self, adapter: BasePlatformAdapter) -> None:
        """React to an adapter failure after startup.

        If the error is retryable (e.g. network blip, DNS failure), queue the
        platform for background reconnection instead of giving up permanently.

        The notification arrives on the failing adapter's own polling task,
        and the disconnect inside the handler can cancel that task mid-flight:
        disconnect()'s current-task guard misses it because
        _safe_adapter_disconnect runs the close in a wrapper task. A cancelled
        handler dies between the fatal log and the reconnect queue, silently
        stranding the platform (observed 2026-07-21: telegram popped from
        adapters but never queued after a travel network outage). Run the real
        work in a detached task that adapter teardown cannot cancel.
        """
        tasks = getattr(self, "_fatal_handler_tasks", None)
        if tasks is None:
            tasks = self._fatal_handler_tasks = set()
        task = asyncio.create_task(self._handle_adapter_fatal_error_detached(adapter))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        # Await so callers that expect completion still get it — but through
        # shield(): Task.cancel() on the caller also cancels the future it is
        # awaiting (_fut_waiter), so a plain `await task` would tunnel the
        # cancellation straight into the "detached" task. shield() absorbs
        # it: the caller sees CancelledError, the handler runs to completion.
        await asyncio.shield(task)

    def _queue_retryable_fatal_platform(self, adapter: BasePlatformAdapter) -> bool:
        """Queue a retryable fatal adapter for background reconnection.

        Returns True when the platform was newly queued. Idempotent if already
        queued. Must not await: callers invoke this *before* any disconnect
        await so a wedged close cannot strand the platform (#80598).
        """
        from gateway.run import logger
        if not adapter.fatal_error_retryable:
            return False
        platform_config = self.config.platforms.get(adapter.platform)
        if not platform_config or adapter.platform in self._failed_platforms:
            return False
        self._failed_platforms[adapter.platform] = {
            "config": platform_config,
            "attempts": 0,
            "next_retry": time.monotonic(),
            "queued_at": time.monotonic(),
            "credential_claim": self._adapter_credential_claim(
                adapter.platform, adapter
            ),
            "listener_claim": self._adapter_listener_claim(
                adapter.platform, adapter
            ),
        }
        logger.info(
            "%s queued for background reconnection",
            adapter.platform.value,
        )
        # Ensure the reconnect watcher is alive — if it died (e.g. from
        # exhausting its restart budget), respawn it so queued platforms
        # are not permanently stranded (#70344).
        self._ensure_reconnect_watcher_running()
        return True

    async def _handle_adapter_fatal_error_detached(
        self, adapter: BasePlatformAdapter
    ) -> None:
        """Run the fatal handler; if the platform still ends up stranded
        (not reconnected, not queued, not intentionally disabled), exit the
        gateway with failure so the service manager restarts it instead of
        leaving a silent partial outage."""
        from gateway.run import logger
        try:
            # Outer hard deadline (#80598): even with queue-before-disconnect,
            # a hang anywhere in the impl (status write side effects, detach
            # races, etc.) must not leave this task wedged forever — the
            # stranded check in ``finally`` only runs when we return.
            timeout = self._adapter_disconnect_timeout_secs()
            if timeout <= 0:
                await self._handle_adapter_fatal_error_impl(adapter)
            else:
                # Disconnect budget plus a small overhead for queue/status
                # bookkeeping. Keep the additive proportional so tests that
                # shrink the disconnect timeout still finish promptly.
                outer = timeout + min(2.0, max(0.05, timeout))
                completed = await self._await_adapter_cleanup_with_timeout(
                    self._handle_adapter_fatal_error_impl(adapter),
                    outer,
                )
                if not completed:
                    logger.error(
                        "Fatal-error handling for %s timed out after %.1fs; "
                        "ensuring reconnect queue is populated",
                        adapter.platform.value,
                        outer,
                    )
                    self._queue_retryable_fatal_platform(adapter)
        except asyncio.CancelledError:
            # Best-effort queue before re-raising: a cancelled fatal handler
            # must not strand a retryable platform (#80598).
            try:
                self._queue_retryable_fatal_platform(adapter)
            except Exception:
                logger.debug(
                    "Failed to queue %s after fatal-handler cancellation",
                    adapter.platform.value,
                    exc_info=True,
                )
            raise
        except Exception:
            logger.exception(
                "Fatal-error handling for %s raised unexpectedly",
                adapter.platform.value,
            )
            # Best-effort queue so an unexpected raise mid-handler cannot
            # leave a retryable platform permanently deaf (#80598).
            try:
                self._queue_retryable_fatal_platform(adapter)
            except Exception:
                logger.debug(
                    "Failed to queue %s after fatal-handler exception",
                    adapter.platform.value,
                    exc_info=True,
                )
        finally:
            platform = adapter.platform
            shutdown_event = getattr(self, "_shutdown_event", None)
            stranded = (
                adapter.fatal_error_retryable
                and platform not in self.adapters
                and platform not in getattr(self, "_failed_platforms", {})
                and not (shutdown_event is not None and shutdown_event.is_set())
            )
            if stranded:
                logger.error(
                    "%s adapter was lost without entering the reconnection "
                    "queue; exiting gateway so the service manager restarts it.",
                    platform.value,
                )
                self._exit_reason = (
                    f"{platform.value} adapter lost without reconnection queue"
                )
                self._exit_with_failure = True
                await self.stop()

    async def _handle_adapter_fatal_error_impl(self, adapter: BasePlatformAdapter) -> None:
        # Snapshot the current owner of this platform slot before doing
        # anything else. If it's neither this adapter nor empty, a different
        # adapter has already taken over (e.g. this is a delayed notification
        # from a background retry chain that raced with, and lost to, a
        # reconnect that already succeeded). Acting on a stale notification
        # would overwrite an already-healthy platform's runtime status and
        # incorrectly re-queue it for reconnection, so bail out before any of
        # that happens.
        from gateway.run import logger
        existing = self.adapters.get(adapter.platform)
        if existing is not None and existing is not adapter:
            logger.debug(
                "Ignoring stale fatal error from a superseded %s adapter instance: %s",
                adapter.platform.value,
                adapter.fatal_error_code or "unknown",
            )
            return

        logger.error(
            "Fatal %s adapter error (%s): %s",
            adapter.platform.value,
            adapter.fatal_error_code or "unknown",
            adapter.fatal_error_message or "unknown error",
        )
        # Phase 7 Unit 7d-B: a relay credential revoked by opt-out is not an
        # error to retry — render it as a clean "disabled" state, not red
        # "fatal"/"retrying". (The code is set non-retryable, so it also drops
        # out of the reconnect queue below.)
        if adapter.fatal_error_code == "relay_disabled":
            platform_state = "disabled"
        elif adapter.fatal_error_retryable:
            platform_state = "retrying"
        else:
            platform_state = "fatal"
        self._update_platform_runtime_status(
            adapter.platform.value,
            platform_state=platform_state,
            error_code=adapter.fatal_error_code,
            error_message=adapter.fatal_error_message,
        )

        if existing is adapter:
            # Claim this adapter for teardown before awaiting disconnect() —
            # a second fatal-error notification for the same adapter (e.g.
            # from a concurrent recovery path) would otherwise still see
            # itself as "existing" during the await below and disconnect()
            # the same object twice.
            self.adapters.pop(adapter.platform, None)
            self.delivery_router.adapters = self.adapters

        # Queue retryable failures BEFORE any disconnect await (#80598).
        # A half-dead transport can wedge native close() (or swallow
        # CancelledError inside it) so the previous "disconnect then queue"
        # order left platforms permanently deaf inside a live process even
        # after the network recovered. Populate the queue first so the
        # reconnect watcher always has work; teardown is best-effort after.
        self._queue_retryable_fatal_platform(adapter)

        if existing is adapter:
            # A half-closed transport can wedge an adapter's native close()
            # indefinitely. Reuse the shutdown-path timeout so this runtime
            # fatal handler always returns to the stay-alive / stranded path.
            await self._safe_adapter_disconnect(adapter, adapter.platform)

        if not self.adapters and not self._failed_platforms:
            self._exit_reason = adapter.fatal_error_message or "All messaging adapters disconnected"
            if adapter.fatal_error_retryable:
                self._exit_with_failure = True
                logger.error("No connected messaging platforms remain. Shutting down gateway for service restart.")
            else:
                logger.error("No connected messaging platforms remain. Shutting down gateway cleanly.")
            await self.stop()
        elif not self.adapters and self._failed_platforms:
            # All platforms are down and queued for background reconnection.
            # Keep the gateway alive so:
            #   • cron jobs still run
            #   • the reconnect watcher can recover platforms when the
            #     underlying problem clears (proxy comes back, user runs
            #     `hermes whatsapp`, etc.)
            # We used to exit-with-failure here to trigger systemd restart,
            # but that converted a transient outage into a restart loop and
            # killed in-process state every time. The reconnect watcher
            # already handles long-running recovery — let it do its job.
            logger.warning(
                "No connected messaging platforms remain, but %d platform(s) "
                "queued for reconnection — gateway staying alive, watcher will "
                "retry in background.",
                len(self._failed_platforms),
            )

    def _request_clean_exit(self, reason: str) -> None:
        self._exit_cleanly = True
        self._exit_reason = reason
        self._shutdown_event.set()

    def _running_agent_count(self) -> int:
        return len(self._running_agents)

    def _active_work_count(self) -> int:
        """All agent work the gateway must expose and drain as one total."""
        return (
            self._running_agent_count()
            + self._active_cron_job_count()
            + self._active_api_run_count()
        )

    def _active_cron_job_count(self) -> int:
        """Count of cron jobs currently executing, from the cron scheduler's
        own in-flight tracking (``cron.scheduler._running_job_ids``).

        Cron jobs run through a standalone ``AIAgent`` on the scheduler's own
        thread pool (``cron/scheduler.py::run_job``), entirely outside
        ``self._running_agents`` — the dict every OTHER active-work check on
        this class (``_running_agent_count``, ``_drain_active_agents``) reads.
        Without this, the shutdown drain is structurally blind to in-flight
        cron work: it can report ``active_at_start=0`` and proceed straight
        to killing tool subprocesses while a cron job's terminal command is
        still running (#60432). Best-effort: returns 0 if the cron module
        can't be imported (e.g. a minimal test double for this class).
        """
        try:
            from cron.scheduler import get_running_job_ids
            return len(get_running_job_ids())
        except Exception:
            return 0

    def _active_api_run_count(self) -> int:
        """Count API-server work that is outside ``_running_agents``.

        The primary API server owns the sole HTTP listener. Secondary multiplex
        profiles cannot create an ``api_server`` adapter because it binds a port,
        so only the primary registry is a supported source of this work.
        """
        try:
            adapter = getattr(self, "adapters", {}).get(Platform.API_SERVER)
            helper = getattr(adapter, "active_agent_work_count", None)
            return max(0, int(helper())) if callable(helper) else 0
        except Exception:
            return 0

    def _interrupt_api_server_runs(self, reason: str) -> int:
        """Interrupt API-server agents that are not in ``_running_agents``.

        Counterpart of ``_active_api_run_count()``: that method folds
        adapter-owned API work into the shutdown drain, so this one must reach
        the same agents when the drain times out. Duck-typed on the adapter so
        an older adapter (or a minimal test double for this class) without the
        hook is simply skipped rather than raising mid-shutdown.
        """
        from gateway.run import logger
        try:
            adapter = getattr(self, "adapters", {}).get(Platform.API_SERVER)
            helper = getattr(adapter, "interrupt_active_runs", None)
            return max(0, int(helper(reason))) if callable(helper) else 0
        except Exception as exc:
            logger.debug("Failed interrupting api_server runs during shutdown: %s", exc)
            return 0

    # ── scale-to-zero idle detection / dormant-quiesce (Phase 0) ──────────────
    # The gateway-side BEHAVIOUR that consumes the relay scale-to-zero primitives
    # (gateway-gateway Phase 5). Pure logic lives in gateway/scale_to_zero.py; the
    # methods here bind it to the live runner/transport. See ~/nous/specs/
    # scale-to-zero (decisions.md) for the design + the F12/F14 distinctions.

    def _scale_to_zero_has_live_background_work(self) -> bool:
        """Live background work that must block a suspend (D3/F7).

        Backgrounded delegate_task / kanban / terminal(background=true) are NOT
        counted by _running_agent_count(), but suspending mid-flight loses them.
        Checks the runner's own tracked tasks + the process registry's running
        processes + any pending process-completion watchers.

        PERMANENT supervised watchers (tagged _hermes_supervised_watcher by
        _spawn_supervised) are excluded: they live for the whole process —
        including the scale-to-zero watcher itself — so counting them would
        make this predicate True forever and the gateway could never go
        dormant. Verified live on staging (2026-08-12): an armed, fully idle
        instance never logged "going dormant" because ~9 supervised watchers
        sat in _background_tasks. Fly's coarse autostop used to mask this;
        with the gateway owning the suspend it became load-bearing.
        """
        from gateway.run import logger
        if any(
            not t.done() and not getattr(t, "_hermes_supervised_watcher", False)
            for t in self._background_tasks
        ):
            return True
        try:
            from tools.async_delegation import active_count

            if active_count() > 0:
                return True
        except Exception:  # noqa: BLE001 - never let the idle check raise
            logger.debug("scale-to-zero async-delegation check failed", exc_info=True)
        try:
            from tools.process_registry import process_registry

            if process_registry.has_any_active():
                return True
            if process_registry.pending_watchers:
                return True
        except Exception:  # noqa: BLE001 - never let the idle check raise
            logger.debug("scale-to-zero bg-work check failed", exc_info=True)
        return False

    def _scale_to_zero_idle_timeout_seconds(self) -> float:
        from gateway.run import _load_gateway_config
        from gateway.scale_to_zero import parse_idle_timeout_seconds

        raw = None
        try:
            user_cfg = _load_gateway_config()
            gw = user_cfg.get("gateway") if isinstance(user_cfg, dict) else None
            stz = gw.get("scale_to_zero") if isinstance(gw, dict) else None
            if isinstance(stz, dict):
                raw = stz.get("idle_timeout_minutes")
        except Exception:  # noqa: BLE001
            raw = None
        return parse_idle_timeout_seconds(raw)

    def _restart_loop_guard_config(self) -> tuple:
        """Return ``(max_restarts, window_seconds, max_gap_seconds)`` for the
        auto-resume restart-loop breaker (#30719, defense-3), read from
        ``gateway.restart_loop_guard`` in config.yaml with the module defaults
        as fallback. ``max_restarts <= 0`` disables the breaker.

        ``max_gap_seconds`` is the longest spacing between two consecutive
        restart-interrupted boots that still counts them as the same loop, so
        a crash cycle slower than ``window_seconds`` stays visible (#81642).
        """
        from gateway.run import _load_gateway_config
        from gateway import restart_loop_guard as _rlg

        max_restarts = _rlg.DEFAULT_MAX_RESTARTS
        window_seconds = _rlg.DEFAULT_WINDOW_SECONDS
        max_gap_seconds = _rlg.DEFAULT_MAX_GAP_SECONDS
        try:
            user_cfg = _load_gateway_config()
            gw = user_cfg.get("gateway") if isinstance(user_cfg, dict) else None
            rlg = gw.get("restart_loop_guard") if isinstance(gw, dict) else None
            if isinstance(rlg, dict):
                if isinstance(rlg.get("max_restarts"), int):
                    max_restarts = rlg["max_restarts"]
                if isinstance(rlg.get("window_seconds"), int) and rlg["window_seconds"] > 0:
                    window_seconds = rlg["window_seconds"]
                if (
                    isinstance(rlg.get("max_gap_seconds"), int)
                    and rlg["max_gap_seconds"] > 0
                ):
                    max_gap_seconds = rlg["max_gap_seconds"]
        except Exception:  # noqa: BLE001
            pass
        return max_restarts, window_seconds, max_gap_seconds

    def _scale_to_zero_active_messaging_platforms(self) -> list:
        """ENABLED platforms that count for the relay-only arm gate (D1/F6).

        Two filters, both load-bearing:
        - enabled only: config.platforms is pre-seeded with disabled
          placeholders for the full platform catalog (the F25 bug).
        - MESSAGING only: non-messaging surfaces must not disarm scale-to-zero.
          The api_server is a loopback listener force-enabled by the presence
          of API_SERVER_KEY (which the Docker stage2 hook now generates for
          every container, so hosted instances ALWAYS have it enabled) — it
          holds no outbound socket and Chronos fires through it already reset
          the idle clock. Counting it made messaging_is_relay_only_or_absent
          False on every hosted instance, silently disarming the feature.
          Mirrors the non-messaging exclusion set used for handoff eligibility
          (see the `messaging_platforms` computation in _connect_platforms).
        """
        if not self.config:
            return []
        non_messaging = {Platform.LOCAL, Platform.API_SERVER, Platform.WEBHOOK}
        try:
            return [
                p
                for p, pc in self.config.platforms.items()
                if getattr(pc, "enabled", False) and p not in non_messaging
            ]
        except Exception:  # noqa: BLE001
            return []

    def _scale_to_zero_should_arm(self) -> bool:
        """Whether to start the idle watcher (D1/D11/§3.4(1))."""
        from gateway.relay import relay_wake_url
        from gateway.scale_to_zero import (
            messaging_is_relay_only_or_absent,
            scale_to_zero_enabled,
            should_arm,
        )

        platforms = self._scale_to_zero_active_messaging_platforms()
        try:
            wake_url = relay_wake_url()
        except Exception:  # noqa: BLE001
            wake_url = None
        return should_arm(
            enabled=scale_to_zero_enabled(),
            relay_only_or_absent=messaging_is_relay_only_or_absent(platforms),
            wake_url=wake_url,
        )

    def _log_scale_to_zero_not_armed_reason(self) -> None:
        """Log why the idle watcher did NOT arm — but only for an OPTED-IN instance.

        A non-opted instance (no HERMES_SCALE_TO_ZERO stamp) not arming is the normal
        case and must stay silent. When the Labs stamp IS set but the watcher still
        didn't arm, that's the surprising case worth one INFO line so "why won't it
        suspend/wake?" is a log grep, not a box-dive.
        """
        from gateway.run import logger
        from gateway.relay import relay_wake_url
        from gateway.scale_to_zero import (
            messaging_is_relay_only_or_absent,
            scale_to_zero_enabled,
        )

        try:
            enabled = scale_to_zero_enabled()
            if not enabled:
                return  # not opted in — normal, stay quiet
            active = [
                getattr(p, "value", p)
                for p in self._scale_to_zero_active_messaging_platforms()
            ]
            relay_only = messaging_is_relay_only_or_absent(active)
            try:
                wake_url = relay_wake_url()
            except Exception:  # noqa: BLE001
                wake_url = None
            logger.info(
                "scale-to-zero: NOT armed despite opt-in — "
                "relay_only_or_absent=%s (enabled platforms=%s), wake_url=%s. "
                "Need relay-only messaging + a registered wake URL.",
                relay_only,
                active or "none",
                "set" if wake_url else "MISSING",
            )
        except Exception:  # noqa: BLE001 - diagnostics must never block startup
            logger.debug("scale-to-zero: not-armed reason logging failed", exc_info=True)

    def _scale_to_zero_is_idle(self) -> bool:
        from gateway.scale_to_zero import is_idle

        return is_idle(
            running_agent_count=self._running_agent_count(),
            seconds_since_last_inbound=time.time() - self._last_inbound_at,
            idle_timeout_seconds=self._scale_to_zero_idle_timeout_seconds(),
            has_live_background_work=self._scale_to_zero_has_live_background_work(),
        )

    def _scale_to_zero_note_real_inbound(self) -> None:
        """Stamp real inbound and restore lifecycle after a dormant wake.

        The watcher marks runtime status `draining` as it quiesces the relay, but
        dormancy is not the stop/restart drain path: the process remains alive and
        should present as running once real traffic wakes it and re-enters the
        gateway. Internal completion/replay events intentionally do not call this
        helper, so they do not keep an otherwise idle gateway awake.
        """
        from gateway.run import logger
        self._last_inbound_at = time.time()
        if getattr(self, "_scale_to_zero_cooldown_until", 0.0) > 0:
            try:
                self._update_runtime_status("running")
            except Exception:  # noqa: BLE001 - status restoration is best-effort
                logger.debug("scale-to-zero: status restore failed", exc_info=True)
            self._scale_to_zero_cooldown_until = 0.0

    def _relay_adapter_for_dormancy(self):
        """Return the connected RELAY adapter, if any (the one go_dormant targets)."""
        try:
            from gateway.platforms.base import Platform
        except Exception:  # noqa: BLE001
            return None
        return self.adapters.get(Platform.RELAY)

    async def _scale_to_zero_watcher(self, interval: float = 30.0) -> None:
        """Watch for idle, drive the relay dormant, then self-suspend the machine.

        Started ONLY when _scale_to_zero_should_arm() (opted in via the Labs
        HERMES_SCALE_TO_ZERO stamp + relay-only/absent messaging + a wakeUrl).
        On a sustained idle window it runs the DORMANT sequence (D12/F12/F14):
          - mark runtime status `draining` (composes with the existing state
            machine, §3.4(6); does NOT set _running=False),
          - relay adapter.go_dormant() — going_idle->ack + supervisor-preserving
            socket close (NOT disconnect(), NOT the run.py stop path),
          - deliberately NO mark_resume_pending (D13 — suspend preserves RAM),
          - THEN suspend this machine through the local flaps socket
            (gateway.scale_to_zero.suspend_self). The gateway owns the suspend
            because Fly Proxy autostop judges idle on INBOUND connections only:
            it cannot see an in-flight agent turn (outbound-only LLM traffic)
            and, since the mid-2026 proxy change, an open outbound relay socket
            no longer holds the machine awake — autostop:"suspend" would freeze
            the machine mid-job or before the relay flip (the buffered-event
            black hole). NAS therefore provisions scale-to-zero machines with
            autostop:"off"; the suspend only ever happens HERE, strictly after
            the idle predicate held and the dormant quiesce completed.
        Autostart stays platform-side: the connector's wakeUrl poke (Fly-proxied)
        wakes the machine, the preserved reconnect supervisor re-dials, and the
        connector drains the buffered backlog. After driving dormant we set a
        re-arm cooldown so a wake's drained backlog isn't immediately re-quiesced.
        Off-Fly (no flaps socket / machine identity) the suspend step is skipped:
        dormancy still happens, the process just stays running — fail-awake.
        """
        from gateway.run import logger
        await asyncio.sleep(min(interval, 30.0))  # let startup settle
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running:
                    return
                if time.time() < self._scale_to_zero_cooldown_until:
                    continue
                if not self._scale_to_zero_is_idle():
                    continue
                adapter = self._relay_adapter_for_dormancy()
                if adapter is None:
                    continue
                go_dormant = getattr(adapter, "go_dormant", None)
                if not callable(go_dormant):
                    continue
                logger.info(
                    "scale-to-zero: gateway idle for >= %.0fs — going dormant "
                    "(relay buffered, socket closed) then self-suspending",
                    self._scale_to_zero_idle_timeout_seconds(),
                )
                try:
                    self._update_runtime_status("draining")
                except Exception:  # noqa: BLE001 - status is best-effort
                    logger.debug("scale-to-zero: status mark failed", exc_info=True)
                dormant_ok = True
                try:
                    result = go_dormant()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:  # noqa: BLE001 - dormancy is best-effort
                    dormant_ok = False
                    logger.debug("scale-to-zero: go_dormant failed", exc_info=True)
                # 0.F: after a wake the drained inbound updates _last_inbound_at,
                # but give it a window so we don't immediately re-go-dormant on the
                # same idle reading before traffic lands.
                self._scale_to_zero_cooldown_until = time.time() + max(interval, 60.0)
                # Self-suspend ONLY after a clean quiesce: the relay flip must be
                # set (buffered delivery + wake poke armed) before the freeze, or
                # inbound events black-hole while we sleep. Re-check idle one last
                # time — inbound may have landed during the quiesce await.
                if not dormant_ok:
                    continue
                if not self._scale_to_zero_is_idle():
                    logger.info(
                        "scale-to-zero: inbound arrived during quiesce — skipping suspend"
                    )
                    continue
                await self._scale_to_zero_self_suspend()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the watcher must never crash the gateway
                logger.debug("scale-to-zero watcher iteration error", exc_info=True)

    async def _scale_to_zero_self_suspend(self) -> None:
        """Suspend this Fly machine via the local flaps socket (fail-awake).

        Runs the blocking unix-socket call in a worker thread so the event loop
        stays live right up to the kernel freeze. On success the process is
        frozen shortly after — nothing meaningful runs until the wake resume.
        Off-Fly (self_suspend_available() False) this is a silent no-op.
        """
        from gateway.run import logger
        from gateway.scale_to_zero import self_suspend_available, suspend_self

        try:
            if not self_suspend_available():
                logger.debug(
                    "scale-to-zero: flaps socket / machine identity absent — "
                    "dormant without platform suspend"
                )
                return
            accepted = await asyncio.to_thread(suspend_self)
            if not accepted:
                logger.warning(
                    "scale-to-zero: self-suspend not accepted — machine stays "
                    "awake (fail-awake); will retry on the next idle window"
                )
        except Exception:  # noqa: BLE001 - suspend is best-effort, never crash
            logger.debug("scale-to-zero: self-suspend failed", exc_info=True)

    def _status_action_label(self) -> str:
        return "restart" if self._restart_requested else "shutdown"

    def _status_action_gerund(self) -> str:
        return "restarting" if self._restart_requested else "shutting down"

    def _queue_during_drain_enabled(
        self, busy_input_mode: Optional[str] = None
    ) -> bool:
        # Both "queue" and "steer" modes imply the user doesn't want messages
        # to be lost during restart — queue them for the newly-spawned gateway
        # process to pick up.  "interrupt" mode drops them (current behaviour).
        mode = busy_input_mode or self._busy_input_mode
        return self._restart_requested and mode in {"queue", "steer"}

    # -------- /queue FIFO helpers --------------------------------------
    # /queue must produce one full agent turn per invocation, in FIFO
    # order, with no merging.  The adapter's _pending_messages dict is a
    # single "next-up" slot (shared with photo-burst follow-ups), so we
    # use it for the head of the queue and an overflow list for the
    # tail.  Enqueue puts new items in the slot when free, otherwise in
    # the overflow.  Promotion (called after each run's drain) moves the
    # next overflow item into the slot so the following recursion picks
    # it up.  Clearing happens on /new and /reset via
    # _handle_reset_command.

    def _enqueue_fifo(self, session_key: str, queued_event: "MessageEvent", adapter: Any) -> None:
        """Append a /queue event to the FIFO chain for a session."""
        if adapter is None:
            return
        pending_slot = getattr(adapter, "_pending_messages", None)
        if pending_slot is None:
            return
        if session_key in pending_slot:
            self._session_state(session_key).conversation.queued_events.append(
                queued_event
            )
        else:
            pending_slot[session_key] = queued_event

    def _promote_queued_event(
        self,
        session_key: str,
        adapter: Any,
        pending_event: Optional["MessageEvent"],
    ) -> Optional["MessageEvent"]:
        """Promote the next overflow item after the slot was drained.

        Called at the drain site after _dequeue_pending_event consumed
        (or failed to consume) the slot.  If there's an overflow item:
          - When pending_event is None (slot was empty), return the
            overflow head as the new pending_event.
          - When pending_event already exists (slot was populated by an
            interrupt follow-up or similar), stage the overflow head in
            the slot so the NEXT recursion picks it up.
        Returns the (possibly updated) pending_event for drain to use.
        """
        _q_state = self._peek_session_state(session_key)
        overflow = _q_state.conversation.queued_events if _q_state else None
        if not overflow:
            return pending_event
        next_queued = overflow.pop(0)
        if pending_event is None:
            return next_queued
        if adapter is not None and hasattr(adapter, "_pending_messages"):
            adapter._pending_messages[session_key] = next_queued
        else:
            # No adapter — push back so we don't silently drop the item.
            overflow.insert(0, next_queued)
        return pending_event

    def _queue_depth(self, session_key: str, *, adapter: Any = None) -> int:
        """Total pending /queue items for a session — slot + overflow."""
        _q_state = self._peek_session_state(session_key)
        depth = len(_q_state.conversation.queued_events) if _q_state else 0
        if adapter is not None and session_key in getattr(adapter, "_pending_messages", {}):
            depth += 1
        return depth

    @staticmethod
    def _is_goal_continuation_event(event_or_text: Any) -> bool:
        """Return True for synthetic /goal continuation turns.

        Goal continuations are normal queued user-role events, so pause/clear
        must distinguish them from real user /queue messages before removing or
        suppressing them.
        """
        text = getattr(event_or_text, "text", event_or_text) or ""
        return str(text).startswith("[Continuing toward your standing goal]\nGoal:")

    def _clear_goal_pending_continuations(self, session_key: str, adapter: Any) -> int:
        """Remove queued synthetic /goal continuations for one session.

        User-issued /goal pause/clear can race with a continuation already
        queued by the judge.  Remove only synthetic goal continuations while
        preserving normal /queue and user follow-up events.
        """
        removed = 0
        pending_slot = getattr(adapter, "_pending_messages", None) if adapter is not None else None
        if isinstance(pending_slot, dict):
            pending_event = pending_slot.get(session_key)
            if self._is_goal_continuation_event(pending_event):
                pending_slot.pop(session_key, None)
                removed += 1

        _q_state = self._peek_session_state(session_key)
        overflow = _q_state.conversation.queued_events if _q_state else []
        if overflow:
            kept = []
            for queued_event in overflow:
                if self._is_goal_continuation_event(queued_event):
                    removed += 1
                else:
                    kept.append(queued_event)
            _q_state.conversation.queued_events = kept
        return removed

    def _goal_still_active_for_session(self, session_id: str) -> bool:
        """Best-effort fresh DB check before running a queued continuation."""
        from gateway.run import logger
        if not session_id:
            return False
        try:
            from hermes_cli.goals import GoalManager
            return GoalManager(session_id=session_id).is_active()
        except Exception as exc:
            logger.debug("goal continuation: active-state recheck failed: %s", exc)
            return False

    def _update_runtime_status(self, gateway_state: Optional[str] = None, exit_reason: Optional[str] = None) -> None:
        try:
            from gateway.status import write_runtime_status
            write_runtime_status(
                gateway_state=gateway_state,
                exit_reason=exit_reason,
                restart_requested=self._restart_requested,
                active_agents=self._active_work_count(),
            )
        except Exception:
            pass

    def _persist_active_agents(self) -> None:
        """Persist the live in-flight agent count to ``gateway_state.json``.

        Called at every turn boundary (a running-agent slot is claimed or
        released) so the dashboard ``/api/status`` readout reflects in-flight
        gateway turns in near-real-time.  Without this the file is only
        rewritten on lifecycle transitions, so any ``active_agents`` read
        between transitions is stale (a turn could start and finish without the
        file ever moving).

        Deliberately passes ONLY ``active_agents`` — ``gateway_state`` and the
        other fields stay ``_UNSET`` so ``write_runtime_status``'s
        read-merge-write preserves the current lifecycle state (``running`` /
        ``draining`` / …).  Passing ``gateway_state=None`` here would clobber it.
        Best-effort: a failed status write must never disrupt a turn.
        """
        try:
            from gateway.status import write_runtime_status
            write_runtime_status(active_agents=self._active_work_count())
        except Exception:
            pass

    # ------------------------------------------------------------------
    # External drain control (NAS-driven quiesce-without-restart, Phase 2).
    # The dashboard's begin/cancel-drain endpoint writes/removes the
    # ``.drain_request.json`` marker (gateway/drain_control.py); this watcher
    # observes the marker and flips the gateway between accepting and refusing
    # NEW turns, WITHOUT exiting the process. Reversible by design (D4a): NAS
    # POSTs begin-drain, polls /api/status until active_agents hits 0, proceeds
    # with its lifecycle action, then (on cancel/abort) the marker is removed
    # and the gateway re-accepts turns.
    # ------------------------------------------------------------------
    def _enter_external_drain(self) -> None:
        """Begin external drain: stop accepting new turns, flip state.

        Idempotent — re-entering while already draining is a no-op beyond a
        best-effort status re-write. In-flight turns are NOT interrupted (the
        whole point is to let them finish); only NEW turns are refused.
        """
        from gateway.run import logger
        if self._external_drain_active:
            return
        self._external_drain_active = True
        logger.info(
            "External drain ENGAGED (.drain_request.json present) — refusing "
            "new turns; %d in-flight turn(s) will finish. Process stays up.",
            self._active_work_count(),
        )
        # Flip the persisted lifecycle state so /api/status.gateway_busy /
        # gateway_drainable track the drain. Preserve active_agents (the
        # read-merge keeps the live count); only the state changes.
        self._update_runtime_status("draining")

    def _exit_external_drain(self) -> None:
        """Cancel external drain: revert state, re-accept new turns.

        Idempotent. Only reverts to ``running`` when we are actually mid-drain
        AND not also shutting down (a real shutdown ``_draining`` must win —
        never resurrect a stopping gateway to ``running``).
        """
        from gateway.run import logger
        if not self._external_drain_active:
            return
        self._external_drain_active = False
        if self._draining or not self._running:
            # A shutdown drain is in progress / the loop has stopped — do not
            # clobber the terminal state back to running.
            logger.info(
                "External drain marker cleared during shutdown — not reverting "
                "to running (shutdown takes precedence)."
            )
            return
        logger.info(
            "External drain RELEASED (.drain_request.json removed) — "
            "re-accepting new turns; gateway_state -> running."
        )
        self._update_runtime_status("running")

    async def _drain_control_watcher(self, interval: float = 1.0) -> None:
        """Background task: reconcile gateway accept-state with the drain marker.

        Polls ``.drain_request.json`` (presence-based contract,
        gateway/drain_control.py). Marker present -> ``_enter_external_drain``;
        marker absent -> ``_exit_external_drain``. The 1s cadence bounds the
        observe-the-marker latency the live-validation gate checks (point a).
        Reconciles once at startup. A marker stamped with a PRIOR
        instantiation epoch (one that survived a machine restart on the durable
        HERMES_HOME volume — NS-570) is treated as absent by ``drain_requested``
        and is NOT honoured; only a marker from the current instantiation flips
        the gateway into drain. Best-effort: any tick error is logged and the
        loop continues (a transient stat() failure must not wedge the gateway).
        """
        from gateway.run import logger
        from gateway.drain_control import drain_requested

        while self._running:
            try:
                if drain_requested():
                    self._enter_external_drain()
                    # API and cron work live outside messaging's
                    # _running_agents map. Refresh the aggregate while an
                    # external caller polls this reversible drain state.
                    self._persist_active_agents()
                else:
                    self._exit_external_drain()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Drain-control watcher tick error: %s", exc, exc_info=True)
            await asyncio.sleep(interval)

    def _update_platform_runtime_status(
        self,
        platform: str,
        *,
        platform_state: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        needs_attention: Optional[bool] = None,
        retrying_since: Any = _UNSET,
    ) -> None:
        from gateway.run import _UNSET
        try:
            from gateway.status import write_runtime_status
            extra: Dict[str, Any] = {}
            if needs_attention is not None:
                extra["needs_attention"] = needs_attention
            if retrying_since is not _UNSET:
                extra["retrying_since"] = retrying_since
            write_runtime_status(
                platform=platform,
                platform_state=platform_state,
                error_code=error_code,
                error_message=error_message,
                **extra,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Per-platform circuit breaker (pause/resume) — used by the reconnect
    # watcher when a retryable failure recurs past a threshold, and by the
    # /platform pause|resume slash command for manual control.
    # ------------------------------------------------------------------
    def _pause_failed_platform(self, platform, *, reason: str = "") -> None:
        """Mark a queued platform as paused — keep it in ``_failed_platforms``
        but stop the reconnect watcher from hammering it.

        Used by ``/platform pause <name>`` for manual operator intervention.
        Paused platforms are surfaced in ``/platform list`` and resumed with
        ``/platform resume <name>``.  Note: the reconnect watcher does NOT
        auto-pause — retryable (network/DNS) failures keep retrying at the
        backoff cap indefinitely so a transient outage self-heals without
        manual intervention.
        """
        from gateway.run import logger
        info = getattr(self, "_failed_platforms", {}).get(platform)
        if info is None:
            return
        if info.get("paused"):
            return
        info["paused"] = True
        info["pause_reason"] = reason or "auto-paused after repeated failures"
        # Push next_retry far enough out that even if "paused" is missed
        # by a stale code path, the watcher won't fire on it.
        info["next_retry"] = float("inf")
        try:
            self._update_platform_runtime_status(
                platform.value,
                platform_state="paused",
                error_code=None,
                error_message=info["pause_reason"],
            )
        except Exception:
            pass
        logger.warning(
            "%s paused after %d consecutive failures (%s) — "
            "fix the underlying issue then run `/platform resume %s` "
            "to retry, or `hermes gateway restart` to restart the gateway.",
            platform.value, info.get("attempts", 0),
            info["pause_reason"], platform.value,
        )

    def _resume_paused_platform(self, platform) -> bool:
        """Unpause a platform — reset its attempt counter and schedule an
        immediate retry.  Returns True if the platform was paused and is
        now queued; False if it wasn't paused (or wasn't in the queue).
        """
        from gateway.run import logger
        info = getattr(self, "_failed_platforms", {}).get(platform)
        if info is None:
            return False
        if not info.get("paused"):
            return False
        info["paused"] = False
        info.pop("pause_reason", None)
        info["attempts"] = 0
        info["next_retry"] = time.monotonic()  # retry on next watcher tick
        try:
            self._update_platform_runtime_status(
                platform.value,
                platform_state="retrying",
                error_code=None,
                error_message=None,
            )
        except Exception:
            pass
        logger.info("%s resumed — retrying on next watcher tick", platform.value)
        return True

    @staticmethod
    def _load_prefill_messages() -> List[Dict[str, Any]]:
        """Load ephemeral prefill messages from config or env var.
        
        Checks HERMES_PREFILL_MESSAGES_FILE env var first, then falls back to
        the top-level prefill_messages_file key in ~/.hermes/config.yaml.
        agent.prefill_messages_file is accepted as a legacy fallback.
        Relative paths are resolved from ~/.hermes/.
        """
        from gateway.run import _hermes_home, _load_gateway_runtime_config, logger
        file_path = os.getenv("HERMES_PREFILL_MESSAGES_FILE", "")
        if not file_path:
            cfg = _load_gateway_runtime_config()
            file_path = str(cfg.get("prefill_messages_file", "") or "")
            if not file_path:
                file_path = str(cfg_get(cfg, "agent", "prefill_messages_file", default="") or "")
        if not file_path:
            return []
        path = Path(file_path).expanduser()
        if not path.is_absolute():
            path = _hermes_home / path
        if not path.exists():
            logger.warning("Prefill messages file not found: %s", path)
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                logger.warning("Prefill messages file must contain a JSON array: %s", path)
                return []
            return data
        except Exception as e:
            logger.warning("Failed to load prefill messages from %s: %s", path, e)
            return []

    @staticmethod
    def _load_ephemeral_system_prompt() -> str:
        """Load ephemeral system prompt from config or env var.

        Checks HERMES_EPHEMERAL_SYSTEM_PROMPT env var first, then
        ``display.personality`` / ``agent.system_prompt`` in config.yaml.
        """
        from gateway.run import _load_gateway_runtime_config
        from hermes_cli.config import resolve_ephemeral_system_prompt_from_config

        prompt = os.getenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", "")
        if prompt:
            return prompt
        cfg = _load_gateway_runtime_config()
        return resolve_ephemeral_system_prompt_from_config(cfg)

    def _resolve_model_for_channel(
        self,
        platform: Platform,
        chat_id: str,
        *,
        user_config: Optional[dict] = None,
        thread_id: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> str:
        """Resolve model for this channel: channel_overrides else global default.

        Delegates the precedence rule to
        :func:`hermes_cli.model_switch.resolve_effective_model` (session
        override > channel override > global default) — the single owner
        shared with the API server, so the two surfaces cannot diverge
        again (see 7dd00bb47d).  This call site has no session tier: session
        /model overrides are applied later by
        ``_apply_session_model_override`` on the resolved runtime.
        """
        from gateway.run import _get_channel_override, _resolve_gateway_model
        from hermes_cli.model_switch import resolve_effective_model

        override = None
        config = getattr(self, "config", None)
        if config:
            override = _get_channel_override(
                config,
                platform,
                chat_id,
                thread_id=thread_id,
                parent_id=parent_id,
            )
        return resolve_effective_model(
            None,  # session tier applied downstream (_apply_session_model_override)
            override,
            _resolve_gateway_model(user_config),
        )

    def _get_system_prompt_for_channel(
        self,
        platform: Platform,
        chat_id: str,
        *,
        thread_id: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> str:
        """Ephemeral system prompt for this channel/thread.

        Uses ``channel_overrides`` when set, else the global gateway prompt.
        Legacy ``channel_prompts`` are applied separately via ``event.channel_prompt``
        in ``run_sync`` (adapter ``resolve_channel_prompt``), so they are not
        duplicated here.
        """
        from gateway.run import _get_channel_override
        config = getattr(self, "config", None)
        if config:
            override = _get_channel_override(
                config,
                platform,
                chat_id,
                thread_id=thread_id,
                parent_id=parent_id,
            )
            if override and override.system_prompt:
                return (override.system_prompt or "").strip()
        return getattr(self, "_ephemeral_system_prompt", None) or ""

    @staticmethod
    def _load_reasoning_config(model: str = "") -> dict | None:
        """Load reasoning effort from config.yaml, respecting per-model overrides.

        Thin wrapper over the shared chokepoint
        :func:`hermes_constants.resolve_reasoning_config` (per-model override >
        global ``agent.reasoning_effort``; YAML boolean False = disabled).
        Closes #21256.

        Args:
            model: The effective model for the calling session. When empty,
                   the config's ``model.default`` is used.
        """
        from gateway.run import _load_gateway_runtime_config
        from hermes_constants import resolve_reasoning_config
        cfg = _load_gateway_runtime_config()
        return resolve_reasoning_config(cfg, model)

    @staticmethod
    def _parse_reasoning_command_args(raw_args: str) -> tuple[str, bool]:
        """Parse `/reasoning` args into `(value, persist_global)`.

        `/reasoning <level>` is session-scoped by default. `--global` may be
        supplied in any position to persist the change to config.yaml.
        """
        import shlex

        text = str(raw_args or "").strip().replace("—", "--")
        if not text:
            return "", False
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()

        persist_global = False
        value_tokens = []
        for token in tokens:
            if token == "--global":
                persist_global = True
            else:
                value_tokens.append(token)
        return " ".join(value_tokens).strip().lower(), persist_global

    def _resolve_session_reasoning_config(
        self,
        *,
        source: Optional[SessionSource] = None,
        session_key: Optional[str] = None,
        model: str = "",
    ) -> dict | None:
        """Resolve reasoning effort for a session, honoring session overrides.

        Priority: session-scoped ``/reasoning --session`` override >
        per-model override (``agent.reasoning_overrides``) > global
        ``agent.reasoning_effort``. ``model`` should be the session's
        *effective* model (session ``/model`` override included) so
        per-model overrides track what the session actually runs — when
        empty, the config's ``model.default`` is used.
        """
        resolved_session_key = session_key
        if not resolved_session_key and source is not None:
            try:
                resolved_session_key = self._session_key_for_source(source)
            except Exception:
                resolved_session_key = None

        if resolved_session_key:
            _r_state = self._peek_session_state(resolved_session_key)
            if _r_state is not None and _r_state.conversation.reasoning_override is not None:
                return _r_state.conversation.reasoning_override
        return self._load_reasoning_config(model)

    def _set_session_reasoning_override(
        self,
        session_key: str,
        reasoning_config: Optional[dict],
    ) -> None:
        """Set or clear the session-scoped reasoning override."""
        if not session_key:
            return
        # Per-session field write — the old lazy ``self._session_reasoning_overrides
        # = {}`` init replaced the WHOLE dict, racing concurrent sessions'
        # overrides; a SessionState field reset cannot cross sessions.
        self._session_state(session_key).conversation.reasoning_override = (
            None if reasoning_config is None else dict(reasoning_config)
        )

    def _resolve_session_service_tier(
        self,
        source=None,
        session_key: Optional[str] = None,
    ) -> Optional[str]:
        """Resolve the effective service tier for a session.

        A session-scoped /fast override wins over the config default. The
        override dict stores "priority" or None (explicit normal), so key
        presence — not value truthiness — decides whether it applies.
        """
        resolved_session_key = session_key
        if not resolved_session_key and source is not None:
            try:
                resolved_session_key = self._session_key_for_source(source)
            except Exception:
                resolved_session_key = None

        if resolved_session_key:
            _t_state = self._peek_session_state(resolved_session_key)
            if (
                _t_state is not None
                and _t_state.conversation.service_tier_override
                is not _SERVICE_TIER_UNSET
            ):
                return _t_state.conversation.service_tier_override
        return self._load_service_tier()

    def _set_session_service_tier_override(
        self,
        session_key: str,
        service_tier,
        clear: bool = False,
    ) -> None:
        """Set or clear the session-scoped /fast override.

        ``service_tier`` is "priority" or None (explicit normal). Pass
        ``clear=True`` to remove the override entirely (fall back to config).
        """
        if not session_key:
            return
        # Presence-sensitive: "priority" or None (explicit normal) both count
        # as an override; the sentinel means "no override".  Old code
        # wholesale-replaced the dict on lazy init (cross-session race) —
        # per-session field writes eliminate that class of bug.
        self._session_state(session_key).conversation.service_tier_override = (
            _SERVICE_TIER_UNSET if clear else service_tier
        )

    @staticmethod
    def _load_service_tier() -> str | None:
        """Load Priority Processing setting from config.yaml.

        Reads agent.service_tier from config.yaml. Accepted values mirror the CLI:
        "fast"/"priority"/"on" => "priority", while "normal"/"off" disables it.
        Returns None when unset or unsupported.
        """
        from gateway.run import _load_gateway_runtime_config, logger
        cfg = _load_gateway_runtime_config()
        raw = str(cfg_get(cfg, "agent", "service_tier", default="") or "").strip()

        value = raw.lower()
        if not value or value in {"normal", "default", "standard", "off", "none"}:
            return None
        if value in {"fast", "priority", "on"}:
            return "priority"
        logger.warning("Unknown service_tier '%s', ignoring", raw)
        return None

    @staticmethod
    def _load_show_reasoning() -> bool:
        """Load show_reasoning toggle from config.yaml display section."""
        from gateway.run import _load_gateway_runtime_config
        cfg = _load_gateway_runtime_config()
        return is_truthy_value(
            cfg_get(cfg, "display", "show_reasoning"),
            default=False,
        )

    @staticmethod
    def _load_busy_input_mode() -> str:
        """Load gateway drain-time busy-input behavior from config/env."""
        from gateway.run import _load_gateway_runtime_config
        mode = os.getenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "").strip().lower()
        if not mode:
            cfg = _load_gateway_runtime_config()
            mode = str(cfg_get(cfg, "display", "busy_input_mode", default="") or "").strip().lower()
        if mode == "queue":
            return "queue"
        if mode == "steer":
            return "steer"
        return "interrupt"

    @staticmethod
    def _load_busy_text_mode() -> str:
        """Resolve normal busy TEXT follow-up behavior.

        ``busy_input_mode`` is the single source of truth (default
        ``interrupt``). The legacy ``busy_text_mode`` knob is honored only
        when a user explicitly set it, so existing queue setups keep
        working; new installs follow ``busy_input_mode``. Returns one of
        ``interrupt`` | ``queue`` (``steer`` is handled upstream by
        ``busy_input_mode`` and maps to non-queue text handling here).
        """
        # Legacy explicit override wins for backward compat.
        from gateway.run import GatewayRunner, _load_gateway_runtime_config
        legacy = os.getenv("HERMES_GATEWAY_BUSY_TEXT_MODE", "").strip().lower()
        if not legacy:
            cfg = _load_gateway_runtime_config()
            legacy = str(cfg_get(cfg, "display", "busy_text_mode", default="") or "").strip().lower()
        if legacy == "interrupt":
            return "interrupt"
        if legacy == "queue":
            return "queue"
        # No explicit legacy knob → follow busy_input_mode.
        input_mode = GatewayRunner._load_busy_input_mode()
        return "queue" if input_mode == "queue" else "interrupt"

    @staticmethod
    def _busy_modes_from_config(
        config: dict,
        *,
        fallback_input: str,
        fallback_text: str,
    ) -> tuple[str, str]:
        """Resolve one profile's busy modes without consulting process env."""
        raw_input = str(
            cfg_get(config, "display", "busy_input_mode", default="") or ""
        ).strip().lower()
        input_mode = (
            raw_input
            if raw_input in {"interrupt", "queue", "steer"}
            else fallback_input
        )

        raw_text = str(
            cfg_get(config, "display", "busy_text_mode", default="") or ""
        ).strip().lower()
        if raw_text in {"interrupt", "queue"}:
            text_mode = raw_text
        elif raw_input in {"interrupt", "queue", "steer"}:
            text_mode = "queue" if input_mode == "queue" else "interrupt"
        else:
            text_mode = fallback_text
        return input_mode, text_mode

    def _snapshot_profile_busy_modes(self, profile_name: str, config: dict) -> None:
        """Cache a routed profile's busy policy for this gateway lifetime."""
        input_mode, text_mode = self._busy_modes_from_config(
            config,
            fallback_input=getattr(self, "_busy_input_mode", "interrupt"),
            fallback_text=getattr(self, "_busy_text_mode", "interrupt"),
        )
        input_modes = self.__dict__.setdefault("_busy_input_modes_by_profile", {})
        text_modes = self.__dict__.setdefault("_busy_text_modes_by_profile", {})
        input_modes[profile_name] = input_mode
        text_modes[profile_name] = text_mode

    def _busy_profile_name_for_source(self, source: SessionSource) -> Optional[str]:
        """Return the routed profile whose busy policy applies, if any."""
        if not getattr(getattr(self, "config", None), "multiplex_profiles", False):
            return None
        name = str(getattr(source, "profile", "") or "").strip()
        if not name:
            try:
                name = str(self._profile_name_for_source(source) or "").strip()
            except Exception:
                name = ""
        return name or None

    def _effective_busy_input_mode(self, source: SessionSource) -> str:
        """Resolve busy input mode from the routed profile startup snapshot."""
        fallback = getattr(self, "_busy_input_mode", "interrupt")
        profile_name = self._busy_profile_name_for_source(source)
        if not profile_name:
            return fallback
        modes = getattr(self, "_busy_input_modes_by_profile", None)
        return modes.get(profile_name, fallback) if isinstance(modes, dict) else fallback

    def _effective_busy_text_mode(self, source: SessionSource) -> str:
        """Resolve legacy busy text mode from the routed profile snapshot."""
        fallback = getattr(self, "_busy_text_mode", "interrupt")
        profile_name = self._busy_profile_name_for_source(source)
        if not profile_name:
            return fallback
        modes = getattr(self, "_busy_text_modes_by_profile", None)
        return modes.get(profile_name, fallback) if isinstance(modes, dict) else fallback

    @staticmethod
    def _load_restart_drain_timeout() -> float:
        """Load graceful gateway restart/stop drain timeout in seconds."""
        from gateway.run import _load_gateway_runtime_config, logger
        raw = os.getenv("HERMES_RESTART_DRAIN_TIMEOUT", "").strip()
        if not raw:
            cfg = _load_gateway_runtime_config()
            raw = str(cfg_get(cfg, "agent", "restart_drain_timeout", default="") or "").strip()
        value = parse_restart_drain_timeout(raw)
        if raw and value == DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT:
            try:
                float(raw)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid restart_drain_timeout '%s', using default %.0fs",
                    raw,
                    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
                )
        return value

    @staticmethod
    def _load_restart_after_turn_timeout() -> float:
        """Load in-band restart wait-for-idle timeout in seconds (#77184)."""
        from gateway.run import _load_gateway_runtime_config, logger
        env_raw = os.getenv("HERMES_RESTART_AFTER_TURN_TIMEOUT")
        if env_raw is not None and str(env_raw).strip() != "":
            raw: object = env_raw
        else:
            cfg = _load_gateway_runtime_config()
            raw = cfg_get(cfg, "agent", "restart_after_turn_timeout", default=None)
        value = parse_restart_after_turn_timeout(raw)
        # Warn only when the user supplied a non-empty value that failed to
        # parse (parser falls back to the default). ``0`` is valid.
        if raw is not None and str(raw).strip() != "":
            try:
                float(raw)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid restart_after_turn_timeout '%s', using default %.0fs",
                    raw,
                    DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT,
                )
        return value

    @staticmethod
    def _load_background_notifications_mode() -> str:
        """Load background process notification mode from config or env var.

        Modes:
          - ``concise`` — one-line status message on completion (default);
            failures append a short output tail
          - ``all``    — running-output updates *and* the final raw-output message
          - ``result`` — only the final raw-output completion message
          - ``error``  — only the final raw-output message when exit code is non-zero
          - ``off``    — no watcher messages at all
        """
        from gateway.run import _load_gateway_runtime_config, logger
        mode = os.getenv("HERMES_BACKGROUND_NOTIFICATIONS", "")
        if not mode:
            cfg = _load_gateway_runtime_config()
            raw = cfg_get(cfg, "display", "background_process_notifications")
            if raw is False:
                mode = "off"
            elif raw not in {None, ""}:
                mode = str(raw)
        mode = (mode or "concise").strip().lower()
        valid = {"concise", "all", "result", "error", "off"}
        if mode not in valid:
            logger.warning(
                "Unknown background_process_notifications '%s', defaulting to 'concise'",
                mode,
            )
            return "concise"
        return mode

    @staticmethod
    def _load_provider_routing() -> dict:
        """Load OpenRouter provider routing preferences from config.yaml."""
        from gateway.run import _load_gateway_runtime_config
        try:
            # Canonical gateway loader (fail-open): managed overlay + ${VAR}
            # expansion now apply to provider_routing too.
            cfg = _load_gateway_runtime_config()
            return cfg.get("provider_routing", {}) or {}
        except Exception:
            pass
        return {}

    @staticmethod
    def _load_fallback_model() -> list | None:
        """Load fallback provider chain from config.yaml.

        Returns the merged effective chain from ``fallback_providers`` plus any
        legacy ``fallback_model`` entries. ``fallback_providers`` stays first
        when both keys are present.
        """
        from gateway.run import _load_gateway_runtime_config
        try:
            # Canonical gateway loader (fail-open): managed overlay + ${VAR}
            # expansion now apply to the fallback chain too.
            cfg = _load_gateway_runtime_config()
            fb = get_fallback_chain(cfg)
            if fb:
                return fb
        except Exception:
            pass
        return None

    def _refresh_fallback_model(self) -> list | None:
        """Re-read fallback_providers from disk for the next agent create/reuse.

        Cron already does this per job via ``get_fallback_chain``; the gateway
        previously froze ``self._fallback_model`` at process start, so a chain
        configured (or changed) after ``hermes gateway`` was running never
        reached messaging sessions even though the same process's cron jobs
        fell back correctly. Fixes #60955.

        A TRANSIENT read/parse failure (user mid-edit of config.yaml with a
        non-atomic write) keeps the last known-good chain instead of wiping a
        cached agent's working fallback for that turn.  Only a successful read
        that genuinely lacks the key clears the chain.
        """
        from gateway.run import _hermes_home, logger
        try:
            from hermes_cli.config import read_user_config_raw
            cfg_path = _hermes_home / "config.yaml"
            if not cfg_path.exists():
                self._fallback_model = None
                return self._fallback_model
            # Raw primitive (raises on parse failure) is required here: the
            # canonical fail-open loader would return {} on a torn mid-edit
            # write and WIPE the last known-good chain. The overlay/expansion
            # below fixes the managed-scope/${VAR} drift without losing that.
            cfg = read_user_config_raw(cfg_path)
            try:
                from hermes_cli import managed_scope
                cfg = managed_scope.apply_managed_overlay(cfg)
            except Exception:
                pass
            try:
                from hermes_cli.config import _expand_env_vars
                expanded = _expand_env_vars(cfg)
                if isinstance(expanded, dict):
                    cfg = expanded
            except Exception:
                pass
        except Exception:
            # Transient failure — keep last known-good chain.
            logger.debug(
                "fallback_providers refresh: config.yaml read failed; "
                "keeping last known-good chain", exc_info=True,
            )
            return self._fallback_model
        self._fallback_model = get_fallback_chain(cfg) or None
        return self._fallback_model

    @staticmethod
    def _apply_fallback_chain_to_agent(agent: Any, chain: list | None) -> None:
        """Keep a cached agent's fallback chain aligned with current config.

        Skips rewrite while a cooldown is holding the agent on an already-
        activated fallback provider — ``restore_primary_runtime`` owns that
        turn-scoped lifecycle. When primary is active (or cooldown expired),
        replace the chain so mid-uptime ``fallback_providers`` edits take
        effect without requiring a gateway restart (#60955).
        """
        if agent is None:
            return
        new_chain = list(chain or [])
        rate_limited_until = getattr(agent, "_rate_limited_until", 0) or 0
        if (
            getattr(agent, "_fallback_activated", False)
            and rate_limited_until > time.monotonic()
        ):
            return
        old_chain = list(getattr(agent, "_fallback_chain", []) or [])
        agent._fallback_chain = new_chain
        agent._fallback_model = new_chain[0] if new_chain else None
        if not getattr(agent, "_fallback_activated", False):
            agent._fallback_index = 0
        # A config edit signals the user changed something — drop the
        # session-scoped unavailability memo so re-configured entries
        # (e.g. credentials added mid-uptime for a previously-failing
        # provider) get retried instead of staying suppressed for the
        # cached agent's lifetime.  Only on actual content change, so
        # the per-message no-op refresh keeps the memo's rate-limiting
        # benefit (#60955).
        if new_chain != old_chain:
            unavailable = getattr(agent, "_unavailable_fallback_keys", None)
            if unavailable:
                unavailable.clear()

    def _snapshot_running_agents(self) -> Dict[str, Any]:
        from gateway.run import _AGENT_PENDING_SENTINEL
        return {
            session_key: agent
            for session_key, agent in self._running_agent_items()
            if agent is not _AGENT_PENDING_SENTINEL
        }

    def _get_max_concurrent_sessions(self) -> Optional[int]:
        """Return the configured active chat session cap, if enabled."""
        try:
            from hermes_cli.active_sessions import resolve_max_concurrent_sessions

            return resolve_max_concurrent_sessions(getattr(self, "config", None))
        except Exception:
            return None

    def _active_session_limit_message(self, session_key: str) -> Optional[str]:
        """Return a user-facing rejection when starting a new session exceeds the cap."""
        max_sessions = self._get_max_concurrent_sessions()
        if max_sessions is None:
            return None
        if self._is_session_running(session_key):
            return None
        active_count = self._running_agent_count()
        if active_count < max_sessions:
            return None
        from hermes_cli.active_sessions import active_session_limit_message

        return active_session_limit_message(active_count, max_sessions)

    def _claim_active_session_slot(
        self,
        session_key: str,
        source: SessionSource,
    ) -> tuple[Any, Optional[str]]:
        """Claim a cross-process active-session slot for a new gateway turn."""
        from gateway.run import logger
        if self._is_session_running(session_key):
            return None, None
        local_limit_message = self._active_session_limit_message(session_key)
        if local_limit_message is not None:
            return None, local_limit_message
        try:
            from hermes_cli.active_sessions import try_acquire_active_session

            platform = source.platform.value if source and source.platform else "gateway"
            return try_acquire_active_session(
                session_id=session_key,
                surface=f"gateway:{platform}",
                config=getattr(self, "config", None),
                metadata={
                    "platform": platform,
                    "chat_id": getattr(source, "chat_id", "") or "",
                    "user_id": getattr(source, "user_id", "") or "",
                },
            )
        except Exception as exc:
            logger.warning("Failed to claim active session slot: %s", exc)
            return None, None

    @staticmethod
    def _agent_has_active_subagents(running_agent: Any) -> bool:
        """Return True when *running_agent* is currently driving subagents
        via the ``delegate_task`` tool.

        Background (#30170): ``AIAgent.interrupt()`` cascades through the
        parent's ``_active_children`` list and calls ``interrupt()`` on
        every child synchronously, which aborts in-flight subagent work
        and produces a fallback cascade with no actionable signal.
        Demoting ``busy_input_mode='interrupt'`` to ``queue`` semantics
        whenever this helper returns True protects subagent work from
        conversational follow-ups while leaving the explicit ``/stop``
        path (which goes through ``_interrupt_and_clear_session``)
        untouched. Safe-by-default: returns False on any attribute or
        lock error so a missing/broken parent never blocks the existing
        interrupt path.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL
        if running_agent is None or running_agent is _AGENT_PENDING_SENTINEL:
            return False
        children = getattr(running_agent, "_active_children", None)
        # AIAgent always initialises this as a concrete list (see
        # agent/agent_init.py). Reject anything that isn't a real
        # collection — this guards against ``MagicMock()._active_children``
        # auto-creating a truthy stub in tests and triggering the demotion
        # against an agent that doesn't actually have subagents.
        if not isinstance(children, (list, tuple, set)):
            return False
        if not children:
            return False
        lock = getattr(running_agent, "_active_children_lock", None)
        try:
            if lock is not None:
                with lock:
                    return bool(children)
            return bool(children)
        except Exception:
            return False

    async def _session_has_compression_in_flight(self, session_key: str) -> bool:
        """Return True when a compression lock is held for this session's id.

        Context compression is interrupt-protected (#23975) but gateway
        ``interrupt`` busy-input mode can still start a follow-up turn against
        the pre-rotation parent while compression is mid-flight, producing
        orphaned compression siblings (#56391). Callers demote interrupt to
        queue when this returns True.

        Both blocking sources — the ``session_store`` lock + JSON load, and the
        SQLite ``get_compression_lock_holder`` SELECT — are offloaded to a
        worker thread so a large state.db never freezes the event loop (#5).
        """
        from gateway.run import logger
        session_store = getattr(self, "session_store", None)
        if not session_key or session_store is None:
            return False
        try:
            session_id = await asyncio.to_thread(
                self._lookup_session_id_under_store_lock, session_store, session_key
            )
        except (AttributeError, TypeError):
            return False
        except Exception:
            logger.warning(
                "Compression in-flight check failed while reading session %s; "
                "treating compression as active to avoid interrupting a possible "
                "parent-session rotation",
                session_key,
                exc_info=True,
            )
            return True
        if not session_id:
            return False
        session_db = getattr(self, "_session_db", None)
        if session_db is None:
            return False
        raw_db = getattr(session_db, "_db", session_db)
        try:
            holder = await asyncio.to_thread(
                raw_db.get_compression_lock_holder, str(session_id)
            )
            return bool(holder)
        except (AttributeError, TypeError):
            return False
        except Exception:
            logger.warning(
                "Compression in-flight check failed while reading lock holder "
                "for session %s; treating compression as active to avoid "
                "interrupting a possible parent-session rotation",
                session_id,
                exc_info=True,
            )
            return True

    @staticmethod
    def _lookup_session_id_under_store_lock(session_store, session_key: str):
        """Sync helper run in the thread pool: read session_id under the store lock."""
        # noqa: SLF001 — intentional private access; runs off the event loop.
        with session_store._lock:  # noqa: SLF001
            session_store._ensure_loaded_locked()  # noqa: SLF001
            entry = session_store._entries.get(session_key)  # noqa: SLF001
        return getattr(entry, "session_id", None) if entry is not None else None

    # Hard cap on per-session pending follow-ups for busy_input_mode=queue
    # (and the draining/steer-fallback/subagent-demotion paths that share
    # this entry point).  Without a cap, a stuck agent + a rapid-fire user
    # could grow the overflow list unboundedly.  32 turns of queued
    # follow-ups is far beyond any realistic conversational backlog while
    # still small enough to never threaten memory.
    _BUSY_QUEUE_MAX_PENDING = 32

    def _queue_or_replace_pending_event(self, session_key: str, event: MessageEvent) -> None:
        from gateway.run import logger
        adapter = self._adapter_for_source(event.source)
        if not adapter:
            return
        # #28503 — Previously this called ``merge_pending_message_event``
        # with the default ``merge_text=False``, which silently OVERWROTE
        # the single pending slot when consecutive text messages arrived
        # in ``busy_input_mode: queue``. Route through the FIFO
        # infrastructure shared with ``/queue`` so each follow-up gets
        # its own turn in arrival order. Photo bursts still merge into
        # the head slot via ``merge_pending_message_event`` (album
        # semantics); everything else appends to the overflow tail.
        pending_slot = getattr(adapter, "_pending_messages", None)
        existing = pending_slot.get(session_key) if isinstance(pending_slot, dict) else None
        security_metadata_keys = (
            "hermes_plugin_id",
            "hermes_plugin_injection",
            "gateway_session_key",
            "gateway_session_id",
            "gateway_session_strict",
        )
        same_security_context = existing is not None and (
            getattr(existing, "internal", False) == getattr(event, "internal", False)
            and getattr(existing, "allow_gateway_control", True)
            == getattr(event, "allow_gateway_control", True)
            and all(
                (getattr(existing, "metadata", None) or {}).get(key)
                == (getattr(event, "metadata", None) or {}).get(key)
                for key in security_metadata_keys
            )
        )
        if same_security_context and (
            getattr(existing, "message_type", None) == MessageType.PHOTO
            or event.message_type == MessageType.PHOTO
            or bool(getattr(existing, "media_urls", None))
            or bool(getattr(event, "media_urls", None))
        ):
            # Preserve photo-burst / media-merge semantics for the head slot.
            merge_pending_message_event(
                adapter._pending_messages,
                session_key,
                event,
                merge_text=event.message_type == MessageType.TEXT,
            )
            return

        if self._queue_depth(session_key, adapter=adapter) >= self._BUSY_QUEUE_MAX_PENDING:
            logger.warning(
                "Dropping busy-mode follow-up for session %s — pending queue at cap (%d).",
                session_key,
                self._BUSY_QUEUE_MAX_PENDING,
            )
            return

        self._enqueue_fifo(session_key, event, adapter)

    async def _prepare_busy_steer_text(self, event: MessageEvent) -> str:
        """Return steerable text for a busy follow-up, transcribing voice first.

        Fresh and queued voice messages reach the normal inbound STT pipeline,
        but successful steer messages intentionally bypass that queue. Without
        preprocessing here, a media-only voice follow-up has an empty text
        payload and steer mode silently degrades to queue mode.

        Audio file attachments remain files; only voice-message media follows
        the automatic STT contract used by ``_prepare_inbound_message_text``.
        If transcription fails, preserve any caption and let the existing
        steer fallback handle an otherwise empty event without losing it.

        Routes through ``_transcribe_and_echo_pending_voice`` — the single
        out-of-band transcription choke point shared with the interrupt
        monitor and the pending-drain path — so the STT call is made at most
        once per platform message (cached on the event) and the transcript
        echo respects the count-based ledger.  If steering later falls back
        to queue mode, the drain path reuses the cached transcript instead of
        paying for a second STT call or re-echoing the same line.
        """
        text = (event.text or "").strip()
        if not self._pending_event_audio_paths(event):
            return text

        adapter = self._adapter_for_source(event.source)
        enriched_text, successful_transcripts = await self._transcribe_and_echo_pending_voice(
            event,
            adapter,
            event.source,
            text,
            log_context="Busy-steer",
        )
        if not successful_transcripts:
            return text
        return (enriched_text or text).strip()

    async def _handle_active_session_busy_message(self, event: MessageEvent, session_key: str) -> bool:
        # --- Authorization gate (#17775) ---
        # The cold path (_handle_message) checks _is_user_authorized before
        # creating a session.  The busy path must enforce the same check;
        # otherwise unauthorized users in shared threads (Slack/Telegram/Discord)
        # can inject messages into an active session they don't own.
        from gateway.run import _AGENT_PENDING_SENTINEL, _build_media_placeholder, _hermes_home, _load_gateway_config, _platform_config_key, logger
        if not self._is_user_authorized(event.source):
            logger.warning(
                "Dropping message from unauthorized user in active session: "
                "user=%s (%s), platform=%s, session=%s",
                event.source.user_id,
                event.source.user_name,
                event.source.platform.value if event.source.platform else "unknown",
                session_key,
            )
            return True  # handled (silently dropped); do not fall through

        effective_mode = self._effective_busy_input_mode(event.source)

        # --- Draining case (gateway restarting/stopping) ---
        if self._draining:
            adapter = self._adapter_for_source(event.source)
            if not adapter:
                return True

            reply_anchor = self._reply_anchor_for_event(event)
            thread_meta = self._thread_metadata_for_source(event.source, reply_anchor)
            if self._queue_during_drain_enabled(effective_mode):
                self._queue_or_replace_pending_event(session_key, event)
                message = f"⏳ Gateway {self._status_action_gerund()} — queued for the next turn after it comes back."
            else:
                message = f"⏳ Gateway is {self._status_action_gerund()} and is not accepting another turn right now."

            await adapter._send_with_retry(
                chat_id=event.source.chat_id,
                content=message,
                reply_to=(
                    reply_anchor
                    if event.source.platform == Platform.TELEGRAM
                    and event.source.chat_type == "dm"
                    and event.source.thread_id
                    else (None if event.source.platform == Platform.TELEGRAM and event.source.thread_id else event.message_id)
                ),
                metadata=thread_meta,
            )
            return True

        # --- Approval response routing (#46866) ---
        # When the agent is blocked waiting for a dangerous-command approval,
        # plain-text responses like "yes" or "approve" must be routed to the
        # approval handler instead of being steered/queued/interrupted.
        # Otherwise approval via messaging platforms never succeeds — the
        # reply is queued behind a turn that can't start until the approval
        # resolves, so the approval times out and auto-denies (a deadlock).
        #
        # Slash forms (/approve, /deny) already bypass to the runner at the
        # base-adapter guard.  This handles the bare-word forms (Signal/SMS
        # users naturally type "yes" rather than "/approve").  Gating on
        # has_blocking_approval(session_key) is the disambiguator that keeps
        # a conversational "yes" from triggering a dangerous command when no
        # approval is actually pending (design intent — see run.py "Pending
        # exec approvals are handled by /approve and /deny" note).
        #
        # We reuse the canonical /approve and /deny handlers rather than
        # re-deriving the resolution + i18n messaging: they resolve the
        # waiting thread, resume typing, AND return a localized confirmation
        # string.  The busy-handler path does not auto-send that return, so
        # we deliver it ourselves (mirroring the draining-case send above).
        try:
            from tools.approval import has_blocking_approval
            if event.allow_gateway_control and has_blocking_approval(session_key):
                _raw_text = (event.text or "").strip().lower()
                _approve_words = {"approve", "yes", "ok", "okay", "confirm", "y", "👍"}
                _deny_words = {"deny", "no", "reject", "cancel", "n", "👎"}
                _approval_handler = None
                _normalized_args = ""
                if _raw_text in _approve_words:
                    _approval_handler = self._handle_approve_command
                elif _raw_text in _deny_words:
                    _approval_handler = self._handle_deny_command
                elif _raw_text in {"always", "approve always", "always approve"}:
                    _approval_handler = self._handle_approve_command
                    _normalized_args = "always"
                elif _raw_text in {"session", "approve session", "session approve"}:
                    _approval_handler = self._handle_approve_command
                    _normalized_args = "session"
                if _approval_handler is not None:
                    # Synthesize the canonical "/approve [args]" / "/deny"
                    # command text so the slash handlers parse modifiers via
                    # event.get_command_args().  Always use a literal "/" —
                    # MessageEvent.is_command()/get_command_args() only
                    # recognize the "/" prefix, not the per-platform display
                    # prefix ("!" on Slack/Matrix).
                    _verb = "approve" if _approval_handler is self._handle_approve_command else "deny"
                    _synth = f"/{_verb}"
                    if _normalized_args:
                        _synth = f"{_synth} {_normalized_args}"
                    event.text = _synth
                    _reply = await _approval_handler(event)
                    logger.info(
                        "Approval response via plain text: session=%s verb=%s args=%r",
                        session_key, _verb, _normalized_args,
                    )
                    _adapter = self._adapter_for_source(event.source)
                    if _adapter and _reply:
                        _text, _eph_ttl = _adapter._unwrap_ephemeral(_reply)
                        if _text:
                            _anchor = self._reply_anchor_for_event(event)
                            await _adapter._send_with_retry(
                                chat_id=event.source.chat_id,
                                content=_text,
                                reply_to=_anchor,
                                metadata=self._thread_metadata_for_source(event.source, _anchor),
                            )
                    return True
        except Exception:
            logger.warning(
                "Plain-text approval routing failed for session %s; "
                "falling through to busy handling",
                session_key, exc_info=True,
            )

        # Normal busy case (agent actively running a task)
        adapter = self._adapter_for_source(event.source)
        if not adapter:
            return False  # let default path handle it

        # --- Internal synthetic events must never interrupt/steer ---
        # Async-delegation completions (delegate_task(background=true)) and
        # background-process completions (terminal notify_on_complete) re-enter
        # the originating session as internal MessageEvents. When the session
        # is busy, treating them like a user TEXT message means interrupt-mode
        # (the default busy_text_mode) aborts the active turn AND sends a "⚡
        # Interrupting current task" ack — exactly the opposite of the design
        # invariant that a completion surfaces as a NEW turn only when idle and
        # never splices into a running turn. Plugin events carry untrusted
        # payload text, so queue those through the gateway FIFO to keep their
        # security metadata separate from pending user input.
        if getattr(event, "internal", False) and not event.allow_gateway_control:
            self._queue_or_replace_pending_event(session_key, event)
            return True
        if getattr(event, "internal", False):
            return False

        _busy_state = self._peek_session_state(session_key)
        running_agent = _busy_state.turn.agent if _busy_state else None

        busy_text_mode = self._effective_busy_text_mode(event.source)
        if (
            event.message_type == MessageType.TEXT
            and busy_text_mode == "queue"
            and effective_mode != "steer"
        ):
            return False

        # Steer mode: inject mid-run via running_agent.steer() instead of
        # queueing + interrupting.  If the agent isn't running yet
        # (sentinel) or lacks steer(), or the payload is empty, fall back
        # to queue semantics so nothing is lost.
        # #30170 — Subagent protection. ``AIAgent.interrupt()`` cascades
        # to every entry in the parent's ``_active_children`` list and
        # aborts in-flight ``delegate_task`` work. Demote ``interrupt``
        # to ``queue`` when the parent is currently driving subagents so
        # a conversational follow-up doesn't destroy minutes of subagent
        # work. Explicit ``/stop`` and ``/new`` slash commands go through
        # ``_interrupt_and_clear_session`` and are unaffected — the
        # operator still has a way to force-cancel everything.
        demoted_for_subagents = (
            effective_mode == "interrupt"
            and self._agent_has_active_subagents(running_agent)
        )
        if demoted_for_subagents:
            logger.info(
                "Demoting busy_input_mode 'interrupt' to 'queue' for session %s "
                "because the running agent has active subagents (#30170)",
                session_key,
            )
            effective_mode = "queue"
        demoted_for_compression = (
            effective_mode == "interrupt"
            and await self._session_has_compression_in_flight(session_key)
        )
        if demoted_for_compression:
            logger.info(
                "Demoting busy_input_mode 'interrupt' to 'queue' for session %s "
                "because context compression is in flight (#56391)",
                session_key,
            )
            effective_mode = "queue"
        steered = False
        redirected = False
        if effective_mode == "steer":
            steer_text = await self._prepare_busy_steer_text(event)
            # A follow-up qualifies for steering when it is plain text, OR
            # when every attachment is STT-eligible voice media whose
            # transcript was just folded into steer_text — otherwise a voice
            # note in steer mode silently degrades to queue mode (#58780).
            _steer_media_urls = getattr(event, "media_urls", None) or []
            _steer_all_voice = bool(_steer_media_urls) and (
                len(self._pending_event_audio_paths(event)) == len(_steer_media_urls)
            )
            can_steer = (
                steer_text
                and (
                    (
                        event.message_type == MessageType.TEXT
                        and not event.media_urls
                        and not event.media_types
                    )
                    or _steer_all_voice
                )
                and running_agent is not None
                and running_agent is not _AGENT_PENDING_SENTINEL
                and hasattr(running_agent, "steer")
            )
            if can_steer:
                try:
                    steered = bool(running_agent.steer(steer_text))
                except Exception as exc:
                    logger.warning("Gateway steer failed for session %s: %s", session_key, exc)
                    steered = False
            if not steered:
                # Fall back to queue (merge into pending messages, no interrupt)
                effective_mode = "queue"
        elif (
            effective_mode == "interrupt"
            and event.message_type == MessageType.TEXT
            and not event.media_urls
            and not event.media_types
            and running_agent is not None
            and running_agent is not _AGENT_PENDING_SENTINEL
            and getattr(running_agent, "_supports_active_turn_redirect", False) is True
            and hasattr(running_agent, "redirect")
        ):
            try:
                redirected = bool(running_agent.redirect((event.text or "").strip()))
            except Exception as exc:
                logger.warning("Gateway redirect failed for session %s: %s", session_key, exc)
                redirected = False

        # Store the message so it's processed as the next turn after the
        # current run finishes (or is interrupted).  Skip this for a
        # successful steer — the text already landed inside the run and
        # must NOT also be replayed as a next-turn user message.
        #
        # Route through _queue_or_replace_pending_event (the same FIFO
        # infrastructure used by busy queue-mode and /queue) rather than a
        # raw merge_pending_message_event(merge_text=True). The raw merge
        # newline-joins consecutive TEXT follow-ups into a SINGLE pending
        # turn, destroying message boundaries — so two separate user
        # messages sent while the agent was busy (interrupt mode, or a
        # steer that fell back to queue) arrived as one mashed-together
        # turn (#43066 sub-bug 2). The FIFO path gives each text its own
        # turn in arrival order while still preserving photo-burst / album
        # merge semantics for media.
        if not steered and not redirected:
            self._queue_or_replace_pending_event(session_key, event)

        is_queue_mode = effective_mode == "queue"
        is_steer_mode = effective_mode == "steer"
        is_redirect_mode = effective_mode == "interrupt" and redirected

        # If not in queue/steer mode, interrupt the running agent immediately.
        # This aborts in-flight tool calls and causes the agent loop to exit
        # at the next check point.
        if (
            effective_mode == "interrupt"
            and not redirected
            and running_agent
            and running_agent is not _AGENT_PENDING_SENTINEL
        ):
            try:
                _interrupt_text = event.text
                _media_urls = getattr(event, "media_urls", None) or []
                if self._pending_event_audio_paths(event):
                    _interrupt_text, _ = await self._transcribe_and_echo_pending_voice(
                        event,
                        adapter,
                        event.source,
                        event.text or "",
                        log_context="Voice-busy-interrupt",
                    )
                elif not _interrupt_text and _media_urls:
                    _interrupt_text = _build_media_placeholder(event)
                running_agent.interrupt(_interrupt_text)
            except Exception:
                pass  # don't let interrupt failure block the ack

        # Check if busy ack is disabled — skip sending but still process the input.
        # Placed before debounce so we don't stamp a "last ack" timestamp that was
        # never actually delivered.
        busy_ack_enabled = os.environ.get("HERMES_GATEWAY_BUSY_ACK_ENABLED", "true").lower() == "true"
        if not busy_ack_enabled:
            logger.debug("Busy ack suppressed for session %s", session_key)
            return True  # input still processed, just no ack sent

        # Debounce before consulting config-heavy display settings. Rapid
        # follow-ups should be processed but should not trigger another config
        # read just to discover that no ack will be sent.
        _BUSY_ACK_COOLDOWN = 30
        now = time.time()
        last_ack = _busy_state.turn.busy_ack_ts if _busy_state else 0
        if now - last_ack < _BUSY_ACK_COOLDOWN:
            return True  # interrupt sent (if not queue), ack already delivered recently

        from gateway.display_config import resolve_display_setting
        platform_key = _platform_config_key(event.source.platform)

        # In steer mode the user's text has already been injected into the
        # active run. Some mobile chat setups want that steering to be silent,
        # like STT transcript echo suppression: keep the behavior, drop only
        # the confirmation bubble.
        if is_steer_mode:
            steer_ack_env = os.environ.get("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED")
            if steer_ack_env is not None:
                steer_ack_enabled = steer_ack_env.strip().lower() in {"1", "true", "yes", "on"}
            else:
                steer_ack_enabled = bool(
                    resolve_display_setting(
                        _load_gateway_config(),
                        platform_key,
                        "busy_steer_ack_enabled",
                        True,
                    )
                )
            if not steer_ack_enabled:
                logger.debug("Busy steer ack suppressed for session %s", session_key)
                return True

        self._session_state(session_key).turn.busy_ack_ts = now

        # Build a status-rich acknowledgment. Mobile chat defaults keep this
        # terse; detailed iteration/tool state is still available in logs and
        # can be opted in per platform via display.platforms.<platform>.busy_ack_detail.
        status_parts = []
        busy_ack_detail_enabled = bool(
            resolve_display_setting(
                _load_gateway_config(),
                _platform_config_key(event.source.platform),
                "busy_ack_detail",
                True,
            )
        )

        if busy_ack_detail_enabled and running_agent and running_agent is not _AGENT_PENDING_SENTINEL:
            try:
                summary = running_agent.get_activity_summary()
                iteration = summary.get("api_call_count", 0)
                max_iter = summary.get("max_iterations", 0)
                current_tool = summary.get("current_tool")
                start_ts = _busy_state.turn.started_ts if _busy_state else 0
                if start_ts:
                    elapsed_min = int((now - start_ts) / 60)
                    if elapsed_min > 0:
                        status_parts.append(f"{elapsed_min} min elapsed")
                if max_iter:
                    status_parts.append(f"iteration {iteration}/{max_iter}")
                if current_tool:
                    status_parts.append(f"running: {current_tool}")
            except Exception:
                pass

        status_detail = f" ({', '.join(status_parts)})" if status_parts else ""
        if is_steer_mode:
            message = (
                f"⏩ Steered into current run{status_detail}. "
                f"Your message arrives after the next tool call."
            )
        elif is_redirect_mode:
            message = (
                f"↪ Redirected current run{status_detail}. "
                f"I'll adjust using your correction."
            )
        elif is_queue_mode and demoted_for_subagents:
            # #30170 — explain the demotion so the user knows their
            # follow-up didn't accidentally kill the subagent and
            # discovers `/stop` as the explicit escape hatch.
            message = (
                f"⏳ Subagent working{status_detail} — your message is queued for "
                f"when it finishes (use /stop to cancel everything)."
            )
        elif is_queue_mode and demoted_for_compression:
            message = (
                f"⏳ Compressing context{status_detail} — your message is queued for "
                f"when it finishes (use /stop to cancel everything)."
            )
        elif is_queue_mode:
            message = (
                f"⏳ Queued for the next turn{status_detail}. "
                f"I'll respond once the current task finishes."
            )
        else:
            message = (
                f"⚡ Interrupting current task{status_detail}. "
                f"I'll respond to your message shortly."
            )

        # First-touch onboarding: the very first time a user sends a message
        # while the agent is busy, append a one-time hint explaining the
        # queue/interrupt knob.  Flag is persisted to config.yaml so it never
        # fires again on this install.
        try:
            from agent.onboarding import (
                BUSY_INPUT_FLAG,
                busy_input_hint_gateway,
                is_seen,
                mark_seen,
            )
            _user_cfg = _load_gateway_config()
            if not is_seen(_user_cfg, BUSY_INPUT_FLAG):
                if is_steer_mode:
                    _hint_mode = "steer"
                elif is_queue_mode:
                    _hint_mode = "queue"
                elif is_redirect_mode:
                    _hint_mode = "redirect"
                else:
                    _hint_mode = "interrupt"
                message = (
                    f"{message}\n\n"
                    f"{busy_input_hint_gateway(_hint_mode)}"
                )
                mark_seen(_hermes_home / "config.yaml", BUSY_INPUT_FLAG)
        except Exception as _onb_err:
            logger.debug("Failed to apply busy-input onboarding hint: %s", _onb_err)

        reply_anchor = self._reply_anchor_for_event(event)
        thread_meta = self._thread_metadata_for_source(event.source, reply_anchor)
        try:
            await adapter._send_with_retry(
                chat_id=event.source.chat_id,
                content=message,
                reply_to=(
                    reply_anchor
                    if event.source.platform == Platform.TELEGRAM
                    and event.source.chat_type == "dm"
                    and event.source.thread_id
                    else (None if event.source.platform == Platform.TELEGRAM and event.source.thread_id else event.message_id)
                ),
                metadata=thread_meta,
            )
        except Exception as e:
            logger.debug("Failed to send busy-ack: %s", e)

        return True

    async def _drain_active_agents(self, timeout: float) -> tuple[Dict[str, Any], bool]:
        snapshot = self._snapshot_running_agents()
        last_active_count = self._running_agent_count()
        last_cron_count = self._active_cron_job_count()
        last_api_count = self._active_api_run_count()
        last_status_at = 0.0

        def _maybe_update_status(force: bool = False) -> None:
            nonlocal last_active_count, last_cron_count, last_api_count, last_status_at
            now = asyncio.get_running_loop().time()
            active_count = self._running_agent_count()
            cron_count = self._active_cron_job_count()
            api_count = self._active_api_run_count()
            if (
                force
                or active_count != last_active_count
                or cron_count != last_cron_count
                or api_count != last_api_count
                or (now - last_status_at) >= 1.0
            ):
                self._update_runtime_status("draining")
                last_active_count = active_count
                last_cron_count = cron_count
                last_api_count = api_count
                last_status_at = now

        # Cron jobs run on the scheduler's own thread pool, outside
        # ``self._running_agents`` — fold their in-flight count into the
        # same wait/timeout this method already applies to chat sessions,
        # or a cron job's tool work gets killed with zero warning the
        # instant it's the only active thing running (#60432).
        # API-server / desk sessions have the same structural gap (#63529).
        if not self._running_agents and last_cron_count == 0 and last_api_count == 0:
            _maybe_update_status(force=True)
            return snapshot, False

        _maybe_update_status(force=True)
        if timeout <= 0:
            return snapshot, True

        deadline = asyncio.get_running_loop().time() + timeout
        while (
            (
                len(self._running_agents)
                or self._active_cron_job_count()
                or self._active_api_run_count()
            )
            and asyncio.get_running_loop().time() < deadline
        ):
            _maybe_update_status()
            await asyncio.sleep(0.1)
        timed_out = (
            bool(len(self._running_agents))
            or bool(self._active_cron_job_count())
            or bool(self._active_api_run_count())
        )
        _maybe_update_status(force=True)
        return snapshot, timed_out

    def _interrupt_running_agents(self, reason: str) -> None:
        from gateway.run import _AGENT_PENDING_SENTINEL, logger
        for session_key, agent in list(self._running_agents.items()):
            if agent is _AGENT_PENDING_SENTINEL:
                continue
            try:
                request_hard_interrupt(agent, reason)
                logger.debug("Interrupted running agent for session %s during shutdown", session_key)
            except Exception as e:
                logger.debug("Failed interrupting agent during shutdown: %s", e)
        # API-server / desk turns are adapter-owned and never enter
        # _running_agents, so the loop above cannot see them even though
        # _drain_active_agents() waited for them (#63529).
        interrupted_api = self._interrupt_api_server_runs(reason)
        if interrupted_api:
            logger.debug("Interrupted %d api_server run(s) during shutdown", interrupted_api)

    async def _notify_active_sessions_of_shutdown(self) -> None:
        """Send shutdown/restart notifications to active chats and home channels.

        Called at the very start of stop() — adapters are still connected so
        messages can be delivered. Best-effort: individual send failures are
        logged and swallowed so they never block the shutdown sequence.
        """
        from gateway.run import _parse_session_key, logger
        active = self._snapshot_running_agents()
        restart_source = self._restart_command_source if self._restart_requested else None

        action = "restarting" if self._restart_requested else "shutting down"
        hint = (
            "Your current task will be interrupted. "
            "Send any message after restart and I'll try to resume where you left off."
            if self._restart_requested
            else "Your current task will be interrupted."
        )
        msg = f"⚠️ Gateway {action} — {hint}"

        notified: set[tuple[str, str, Optional[str]]] = set()
        for session_key in active:
            source = None
            try:
                if getattr(self, "session_store", None) is not None:
                    await self.async_session_store._ensure_loaded()
                    entry = self.session_store._entries.get(session_key)
                    source = getattr(entry, "origin", None) if entry else None
            except Exception as e:
                logger.debug(
                    "Failed to load session origin for shutdown notification %s: %s",
                    session_key,
                    e,
                )

            if source is None:
                source = self._get_cached_session_source(session_key)

            if source is not None:
                platform_str = source.platform.value
                chat_id = str(source.chat_id)
                thread_id = source.thread_id
            else:
                # Fall back to parsing the session key when no persisted
                # origin is available (legacy sessions/tests).
                _parsed = _parse_session_key(session_key)
                if not _parsed:
                    continue
                platform_str = _parsed["platform"]
                chat_id = _parsed["chat_id"]
                thread_id = _parsed.get("thread_id")

            # Deduplicate only identical delivery targets. Thread/topic-aware
            # platforms can share a parent chat while still routing to distinct
            # destinations via metadata.
            dedup_key = (platform_str, chat_id, str(thread_id) if thread_id else None)
            if dedup_key in notified:
                continue

            try:
                platform = Platform(platform_str)
                adapter = self.adapters.get(platform)
                if not adapter:
                    continue

                platform_cfg = self.config.platforms.get(platform)
                if platform_cfg is not None and not platform_cfg.gateway_restart_notification:
                    logger.info(
                        "Shutdown notification suppressed for active session: %s has gateway_restart_notification=false",
                        platform_str,
                    )
                    continue

                reply_to_message_id = getattr(source, "message_id", None) if source is not None else None
                if reply_to_message_id is None and restart_source is not None:
                    try:
                        restart_platform = restart_source.platform.value
                        restart_chat_id = str(restart_source.chat_id)
                        restart_thread_id = str(restart_source.thread_id) if restart_source.thread_id else None
                        if (restart_platform, restart_chat_id, restart_thread_id) == dedup_key:
                            reply_to_message_id = getattr(restart_source, "message_id", None)
                    except Exception:
                        pass

                metadata = self._thread_metadata_for_target(
                    platform,
                    chat_id,
                    thread_id,
                    chat_type=getattr(source, "chat_type", None) if source is not None else None,
                    reply_to_message_id=reply_to_message_id,
                    adapter=adapter,
                )

                result = await adapter.send(chat_id, msg, metadata=metadata)
                if result is not None and getattr(result, "success", True) is False:
                    logger.debug(
                        "Failed to send shutdown notification to %s:%s: %s",
                        platform_str,
                        chat_id,
                        getattr(result, "error", "send returned success=False"),
                    )
                    continue

                notified.add(dedup_key)
                logger.info(
                    "Sent shutdown notification to active chat %s:%s",
                    platform_str, chat_id,
                )
            except Exception as e:
                logger.debug(
                    "Failed to send shutdown notification to %s:%s: %s",
                    platform_str, chat_id, e,
                )

        if self._restart_requested and restart_source is not None:
            logger.debug("Skipping home-channel shutdown notifications for in-chat restart")
            return

        # Suppress ONLY the home-channel broadcast when the drain that is ending
        # in this shutdown asked us to be quiet (e.g. a NAS auto-update image
        # migration — drain-gated, then the machine is recreated). On the
        # always-on Hermes Cloud fleet that broadcast would otherwise fire on
        # every routine auto-update, spamming home channels with operator-
        # flavoured "gateway shutting down" pings the user doesn't care about.
        # The per-active-session interrupt pings above are deliberately NOT
        # gated: on a drained shutdown they're empty by construction, and in the
        # force-interrupt (deadline-exceeded) case they carry the genuinely
        # useful "your task was cut off, message me to resume" hint. The flag is
        # only honoured for a CURRENT-epoch marker (drain_notification_suppressed
        # reuses the NS-570 staleness check), so an orphaned marker can never
        # silence a fresh gateway's legitimate broadcast.
        try:
            from gateway.drain_control import drain_notification_suppressed
            if drain_notification_suppressed():
                logger.info(
                    "Home-channel shutdown broadcast suppressed by drain marker "
                    "(suppress_notification=true)"
                )
                return
        except Exception as e:
            # Never let the suppression check block the shutdown broadcast —
            # fail toward the louder, more-visible behaviour.
            logger.debug("drain_notification_suppressed check failed: %s", e)

        # Snapshot adapters up front: adapter.send() can hit a fatal error
        # path that pops the adapter from self.adapters (see _handle_fatal
        # elsewhere), which would otherwise trigger
        # ``RuntimeError: dictionary changed size during iteration`` —
        # observed in a user report during gateway shutdown.
        for platform, adapter in list(self.adapters.items()):
            home = self.config.get_home_channel(platform)
            if not home or not home.chat_id:
                continue

            platform_cfg = self.config.platforms.get(platform)
            if platform_cfg is not None and not platform_cfg.gateway_restart_notification:
                logger.info(
                    "Shutdown notification suppressed for home channel: %s has gateway_restart_notification=false",
                    platform.value,
                )
                continue

            dedup_key = (platform.value, str(home.chat_id), str(home.thread_id) if home.thread_id else None)
            if dedup_key in notified:
                continue

            try:
                metadata = self._thread_metadata_for_target(
                    platform,
                    home.chat_id,
                    home.thread_id,
                    adapter=adapter,
                )
                if metadata:
                    result = await adapter.send(str(home.chat_id), msg, metadata=metadata)
                else:
                    result = await adapter.send(str(home.chat_id), msg)
                if result is not None and getattr(result, "success", True) is False:
                    logger.debug(
                        "Failed to send shutdown notification to home channel %s:%s: %s",
                        platform.value,
                        home.chat_id,
                        getattr(result, "error", "send returned success=False"),
                    )
                    continue

                notified.add(dedup_key)
                logger.info(
                    "Sent shutdown notification to home channel %s:%s",
                    platform.value,
                    home.chat_id,
                )
            except Exception as e:
                logger.debug(
                    "Failed to send shutdown notification to home channel %s:%s: %s",
                    platform.value,
                    home.chat_id,
                    e,
                )

    async def _finalize_shutdown_agents(self, active_agents: Dict[str, Any]) -> None:
        from gateway.run import logger
        for agent in active_agents.values():
            # Persist any in-flight transcript to the SQLite session store
            # before teardown (#13121).  An agent forcibly interrupted by the
            # drain-timeout escalation may never reach
            # ``turn_finalizer.finalize_turn`` (the only place that flushes the
            # turn to state.db) — e.g. it was blocked in a tool call that did
            # not abort within the post-interrupt grace window.  Its in-flight
            # tool rounds live only in the in-memory ``_session_messages``
            # (refreshed per tool round in ``conversation_loop`` but never
            # written to SQLite mid-turn), so the immediate pre-restart turn is
            # silently dropped from ``load_transcript()`` on resume.  Flushing
            # here closes that gap; the resume_pending / fresh-tool-tail
            # branches in ``_handle_message_with_agent`` already expect a
            # transcript whose tail may be a pending tool result.  The flush is
            # idempotent (identity-tracked in ``_flush_messages_to_session_db``),
            # so agents that DID finish gracefully re-flush nothing.
            try:
                _flush = getattr(agent, "_flush_messages_to_session_db", None)
                _session_messages = getattr(agent, "_session_messages", None)
                if callable(_flush) and isinstance(_session_messages, list) and _session_messages:
                    # Strip private empty-response retry scaffolding from the
                    # tail first, mirroring the graceful ``_persist_session``
                    # path, so a resumed turn doesn't replay synthetic recovery
                    # nudges.
                    _strip = getattr(
                        agent, "_drop_trailing_empty_response_scaffolding", None
                    )
                    if callable(_strip):
                        try:
                            _strip(_session_messages)
                        except Exception:
                            pass
                    try:
                        _flush(_session_messages)
                    except Exception as _flush_err:
                        # The in-memory transcript could not be persisted
                        # (e.g. FTS/SQLite index corruption — #72680). A plain
                        # debug log loses the conversation permanently when the
                        # process exits. Dump the live agent history to an
                        # external JSON recovery snapshot so an operator can
                        # salvage it after repairing state.db. The flush is
                        # non-fatal; shutdown must never block on a best-effort
                        # backup.
                        logger.warning(
                            "Shutdown transcript flush failed (%s); preserving "
                            "%d in-memory message(s) to recovery snapshot",
                            _flush_err,
                            len(_session_messages),
                        )
                        from gateway.shutdown_flush import flush_agent_history_to_file
                        flush_agent_history_to_file(
                            getattr(agent, "session_id", None),
                            _session_messages,
                        )
            except Exception as _e:
                logger.debug("Shutdown transcript flush failed: %s", _e)
            try:
                from hermes_cli.lifecycle import finalize_session
                finalize_session(
                    session_id=getattr(agent, "session_id", None),
                    platform="gateway",
                    reason="shutdown",
                )
            except Exception:
                pass
            # Off-loop + bounded: a wedged memory provider here used to hang
            # the whole shutdown so SIGTERM never completed (#53175).
            await self._cleanup_agent_resources_off_loop(
                agent, context="shutdown finalize"
            )

    def _should_emit_long_running_notification(
        self,
        session_key: Optional[str],
        agent: Any,
        executor_task: Optional[Any],
    ) -> bool:
        """Only emit the heartbeat while this task still owns the live run.

        Guards against a stale ``running: delegate_task`` heartbeat outliving the
        run that started it: stop once the executor finishes, the agent is gone,
        or the session key has been rebound to a different live agent (e.g. the
        user sent ``/new`` and a fresh agent took the slot mid-run, #12029).
        """
        if agent is None:
            return False
        if executor_task is not None and executor_task.done():
            return False
        if session_key:
            _hb_state = self._peek_session_state(session_key)
            if (_hb_state.turn.agent if _hb_state else None) is not agent:
                return False
        return True

    # Upper bound on off-loop agent-resource cleanup invoked from coroutines
    # running on the gateway's event loop (session-expiry sweep, in-turn
    # cache-hygiene re-eviction). _cleanup_agent_resources is synchronous and
    # can block for a long time (agent.close() does subprocess teardown;
    # shutdown_memory_provider() may do network/SQLite IO via a memory plugin).
    # Calling it inline wedges the whole loop — the bot goes silent, the
    # runtime-status updated_at heartbeat freezes, and SIGTERM cannot be
    # serviced (#53175). Offload to a worker thread under this timeout so the
    # loop is never blocked; mirrors the /new reset path's fix (#35994).
    _CLEANUP_TIMEOUT_S = 30.0

    def _defer_agent_cleanup_until_future_done(
        self,
        future: asyncio.Future,
        agent: Any,
        *,
        context: str,
    ) -> None:
        """Clean up ``agent`` only after its executor future has finished.

        A timed-out executor call keeps running in its worker thread. Closing
        the agent before that thread exits can tear down clients or providers
        it is still using. Keep a strong task reference and wait for the real
        future before invoking the normal bounded, off-loop cleanup path.
        """

        from gateway.run import logger
        async def _cleanup_when_done() -> None:
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                # Loop shutdown can cancel this waiter while the executor still
                # runs. Never turn that cancellation into premature cleanup.
                return
            except Exception as exc:
                logger.debug(
                    "Deferred agent worker%s finished with an error: %s",
                    f" ({context})" if context else "",
                    exc,
                )
            await self._cleanup_agent_resources_off_loop(agent, context=context)

        task = asyncio.create_task(_cleanup_when_done())
        tasks = getattr(self, "_deferred_agent_cleanup_tasks", None)
        if tasks is None:
            tasks = set()
            self._deferred_agent_cleanup_tasks = tasks
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _cleanup_agent_resources_off_loop(
        self, agent: Any, *, context: str = ""
    ) -> None:
        """Run _cleanup_agent_resources in a worker thread with a bounded wait.

        Safe to await from coroutines on the gateway event loop: a slow or
        wedged teardown (memory provider IO, subprocess close) can no longer
        block message processing. On timeout the await is cancelled and the
        worker thread is left to finish (or leak) on its own — the caller
        proceeds regardless, exactly as the /new reset path does (#35994).
        """
        from gateway.run import logger
        if agent is None:
            return
        if context.startswith("shutdown") or context == "session expiry":
            try:
                agent._end_session_on_close = False
            except Exception:
                pass
        try:
            await asyncio.wait_for(
                self._run_in_executor_with_context(
                    self._cleanup_agent_resources, agent
                ),
                timeout=self._CLEANUP_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Agent resource cleanup%s exceeded %ss; proceeding without "
                "blocking the event loop (the worker thread is left to finish "
                "on its own). (#53175)",
                f" ({context})" if context else "",
                self._CLEANUP_TIMEOUT_S,
            )
        except Exception as cleanup_exc:
            logger.warning(
                "Agent resource cleanup%s failed: %s (#53175)",
                f" ({context})" if context else "",
                cleanup_exc,
            )

    def _cleanup_agent_resources(self, agent: Any) -> None:
        """Best-effort cleanup for temporary or cached agent instances."""
        if agent is None:
            return
        try:
            if hasattr(agent, "shutdown_memory_provider"):
                # Drain queued memory writes BEFORE tearing the provider down.
                # The memory manager persists per-turn sync and end-of-session
                # extraction on a single serialized background worker.
                # shutdown_memory_provider() -> shutdown_all() only gives that
                # worker a ~5s bounded drain and abandons (cancels) anything
                # still queued past it, so a /reset — or any gateway session
                # rotation that reaches this cleanup path — could silently drop
                # writes the session had already handed off. The next session
                # then loads stale memory (#73297). Give pending work a bounded
                # head start through the manager's own barrier first, mirroring
                # the CLI exit path (cli.py). Best-effort: a flush failure must
                # never block teardown.
                _mm = getattr(agent, "_memory_manager", None)
                if _mm is not None and hasattr(_mm, "flush_pending"):
                    try:
                        _mm.flush_pending(timeout=10)
                    except Exception:
                        pass
                # Pass the agent's own conversation transcript so memory
                # providers' ``on_session_end`` hooks see the real messages
                # instead of the empty default (#15165). ``_session_messages``
                # is set on ``AIAgent`` (run_agent.py:1518) and refreshed at
                # the end of every ``run_conversation`` turn via
                # ``_persist_session``; on an agent built through
                # ``object.__new__`` (test stubs) the attribute may be
                # absent, so ``getattr`` with a ``None`` default keeps the
                # call signature-compatible with the pre-fix behaviour
                # (``shutdown_memory_provider(messages=None)``).
                session_messages = getattr(agent, "_session_messages", None)
                if isinstance(session_messages, list):
                    agent.shutdown_memory_provider(session_messages)
                else:
                    agent.shutdown_memory_provider()
        except Exception:
            pass
        # Close tool resources (terminal sandboxes, browser daemons,
        # background processes, httpx clients) to prevent zombie
        # process accumulation.
        try:
            if hasattr(agent, "close"):
                agent.close()
        except Exception:
            pass
        # Auxiliary async clients (session_search/web/vision/etc.) live in a
        # process-global cache and are created inside worker threads. Clean up
        # any entries whose event loop is now dead so their httpx transports do
        # not accumulate across gateway turns.
        try:
            from agent.auxiliary_client import cleanup_stale_async_clients
            cleanup_stale_async_clients()
        except Exception:
            pass

    _STUCK_LOOP_THRESHOLD = 3  # restarts while active before auto-suspend
    _STUCK_LOOP_FILE = ".restart_failure_counts"

    def _increment_restart_failure_counts(self, active_session_keys: set) -> None:
        """Increment restart-failure counters for sessions active at shutdown.

        Persists to a JSON file so counters survive across restarts.
        Sessions NOT in active_session_keys are removed (they completed
        successfully, so the loop is broken).
        """
        from gateway.run import _hermes_home, atomic_json_write
        import json

        path = _hermes_home / self._STUCK_LOOP_FILE
        try:
            counts = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            counts = {}

        # Increment active sessions, remove inactive ones (loop broken)
        new_counts = {}
        for key in active_session_keys:
            new_counts[key] = counts.get(key, 0) + 1
        # Keep any entries that are still above 0 even if not active now
        # (they might become active again next restart)

        try:
            atomic_json_write(path, new_counts, indent=None)
        except Exception:
            pass

    def _suspend_stuck_loop_sessions(self) -> int:
        """Suspend sessions that have been active across too many restarts.

        Returns the number of sessions suspended.  Called on gateway startup
        AFTER suspend_recently_active() to catch the stuck-loop pattern:
        session loads → agent gets stuck → gateway restarts → repeat.
        """
        from gateway.run import _hermes_home, logger
        import json

        path = _hermes_home / self._STUCK_LOOP_FILE
        if not path.exists():
            return 0

        try:
            counts = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return 0

        suspended = 0
        stuck_keys = [k for k, v in counts.items() if v >= self._STUCK_LOOP_THRESHOLD]

        for session_key in stuck_keys:
            try:
                entry = self.session_store._entries.get(session_key)
                if entry and not entry.suspended:
                    entry.suspended = True
                    suspended += 1
                    logger.warning(
                        "Auto-suspended stuck session %s (active across %d "
                        "consecutive restarts — likely a stuck loop)",
                        session_key, counts[session_key],
                    )
            except Exception:
                pass

        if suspended:
            try:
                self.session_store._save()
            except Exception:
                pass

        # Clear the file — counters start fresh after suspension
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

        return suspended

    async def _clear_restart_failure_count(self, session_key: str) -> None:
        """Clear the restart-failure counter for a session that completed OK.

        Called after a successful agent turn to signal the loop is broken.
        Offloaded to a thread because the caller (_handle_message_with_agent)
        runs on the event loop and atomic_json_write calls os.fsync.
        """
        from gateway.run import _hermes_home, atomic_json_write
        import json

        path = _hermes_home / self._STUCK_LOOP_FILE
        if not path.exists():
            return
        try:
            counts = json.loads(path.read_text(encoding="utf-8"))
            if session_key in counts:
                del counts[session_key]
                if counts:
                    await asyncio.to_thread(atomic_json_write, path, counts, indent=None)
                else:
                    path.unlink(missing_ok=True)
        except Exception:
            pass

    async def _launch_detached_restart_command(self) -> None:
        from gateway.run import _resolve_hermes_bin, logger
        import shutil
        import subprocess

        hermes_cmd = _resolve_hermes_bin()
        if not hermes_cmd:
            logger.error("Could not locate hermes binary for detached /restart")
            return
        if self._detached_restart_helper_started:
            return
        self._detached_restart_helper_started = True

        current_pid = os.getpid()
        restart_after_s = max(float(getattr(self, "_restart_drain_timeout", 0.0) or 0.0) + 5.0, 5.0)

        # On Windows there's no bash/setsid chain — spawn a tiny Python
        # watcher directly via sys.executable instead.  The watcher polls
        # current_pid, waits for our exit, then runs `hermes gateway
        # restart` with detach flags so the respawn survives the CLI
        # that triggered the /restart command closing its console.
        if sys.platform == "win32":
            import textwrap
            from hermes_cli._subprocess_compat import (
                windows_detach_flags_without_breakaway,
                windows_detach_popen_kwargs,
            )

            cmd_argv = [*hermes_cmd, "gateway", "restart"]
            watcher = textwrap.dedent(
                """
                import os, subprocess, sys, time
                from hermes_cli._subprocess_compat import windows_detach_flags_without_breakaway
                pid = int(sys.argv[1])
                restart_after_s = float(sys.argv[2])
                cmd = sys.argv[3:]
                deadline = time.monotonic() + restart_after_s

                def _alive(p):
                    # On Windows, os.kill(pid, 0) is NOT a no-op — it maps to
                    # GenerateConsoleCtrlEvent(0, pid) (bpo-14484). Use the
                    # Win32 handle-based existence check instead.
                    if os.name == 'nt':
                        import ctypes
                        k32 = ctypes.windll.kernel32
                        k32.OpenProcess.restype = ctypes.c_void_p
                        k32.WaitForSingleObject.restype = ctypes.c_uint
                        k32.GetLastError.restype = ctypes.c_uint
                        h = k32.OpenProcess(0x1000 | 0x100000, False, int(p))
                        if not h:
                            return k32.GetLastError() != 87
                        try:
                            return k32.WaitForSingleObject(h, 0) == 0x102
                        finally:
                            k32.CloseHandle(h)
                    try:
                        os.kill(int(p), 0)
                        return True
                    except ProcessLookupError:
                        return False
                    except PermissionError:
                        return True
                    except OSError:
                        return False

                while time.monotonic() < deadline:
                    if not _alive(pid):
                        break
                    time.sleep(0.2)
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=windows_detach_flags_without_breakaway(),
                )
                """
            ).strip()
            from tools.environments.local import build_subprocess_env
            watcher_env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=True)
            # This watcher is intentionally outside the running gateway. If it
            # inherits the gateway marker, `hermes gateway restart` refuses to
            # run as a self-restart loop guard and the gateway stays stopped.
            watcher_env.pop("_HERMES_GATEWAY", None)
            project_root = Path(__file__).resolve().parent.parent
            # The watcher runs sys.executable (console python) under the
            # CREATE_NO_WINDOW detach kwargs below: it owns one hidden
            # console, inherited by the `hermes gateway restart` child, so
            # nothing flashes. Do NOT swap in GUI-subsystem pythonw.exe —
            # a console-less watcher forces every console-subsystem
            # descendant to allocate a visible conhost (#54220/#56747).
            watcher_python = sys.executable
            venv_dir = Path(watcher_env.get("VIRTUAL_ENV") or project_root / "venv")
            site_packages = venv_dir / "Lib" / "site-packages"
            if site_packages.exists():
                watcher_env["VIRTUAL_ENV"] = str(venv_dir)
                pythonpath = [str(project_root), str(site_packages)]
                if watcher_env.get("PYTHONPATH"):
                    pythonpath.append(watcher_env["PYTHONPATH"])
                watcher_env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(pythonpath))
            watcher_argv = [
                watcher_python,
                "-c",
                watcher,
                str(current_pid),
                str(restart_after_s),
                *cmd_argv,
            ]
            # The watcher process must itself break away from any job object the
            # parent CLI lives in (Electron/Tauri-wrapped Hermes Desktop, Windows
            # Terminal, schtasks shells); otherwise it is reaped when the CLI
            # exits and the gateway never respawns.  windows_detach_popen_kwargs()
            # carries CREATE_BREAKAWAY_FROM_JOB, but a restrictive job object
            # (no JOB_OBJECT_LIMIT_BREAKAWAY_OK) rejects that bit with
            # ERROR_ACCESS_DENIED, surfaced as OSError.  Retry once without the
            # breakaway bit, preserving argv and the scrubbed watcher_env.
            # Mirrors the canonical fallback in
            # hermes_cli/gateway_windows.py::_spawn_detached.
            try:
                subprocess.Popen(
                    watcher_argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=watcher_env,
                    **windows_detach_popen_kwargs(),
                )
            except OSError:
                try:
                    subprocess.Popen(
                        watcher_argv,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env=watcher_env,
                        creationflags=windows_detach_flags_without_breakaway(),
                    )
                except OSError as exc:
                    # Both spawn attempts failed (a breakaway-denying job object
                    # is the common cause, but OSError covers others too).
                    # Record a minimal, path-safe diagnostic and return without
                    # crashing the caller: state plainly that no watcher was
                    # started, and log only the interpreter basename and a
                    # numeric error code — never argv, env, watcher source, or
                    # str(exc) (which can carry a full interpreter path for a
                    # FileNotFoundError).
                    winerror = getattr(exc, "winerror", None)
                    error_code = winerror if winerror is not None else exc.errno
                    error_field = "winerror" if winerror is not None else "errno"
                    logger.warning(
                        "Detached restart watcher was not started after the "
                        "no-breakaway retry (%s; %s=%r). The gateway will not "
                        "be respawned by this restart attempt.",
                        os.path.basename(watcher_python),
                        error_field,
                        error_code,
                    )
            return

        cmd = " ".join(shlex.quote(part) for part in hermes_cmd)
        shell_cmd = (
            f"deadline=$(( $(date +%s) + {int(restart_after_s)} )); "
            f"while kill -0 {current_pid} 2>/dev/null && [ $(date +%s) -lt $deadline ]; do sleep 0.2; done; "
            f"{cmd} gateway restart"
        )
        # Same marker scrub as the Windows watcher above: this watcher runs
        # `hermes gateway restart` from outside the gateway, but it inherits
        # _HERMES_GATEWAY=1 from us, and the CLI's self-restart loop guard
        # refuses to run when that marker is set — silently (DEVNULL), so the
        # gateway stops and never comes back.
        from tools.environments.local import build_subprocess_env
        watcher_env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=True)
        watcher_env.pop("_HERMES_GATEWAY", None)
        setsid_bin = shutil.which("setsid")
        if setsid_bin:
            subprocess.Popen(
                [setsid_bin, "bash", "-lc", shell_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=watcher_env,
                start_new_session=True,
            )
        else:
            subprocess.Popen(
                ["bash", "-lc", shell_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=watcher_env,
                start_new_session=True,
            )

    def _launch_systemd_restart_shortcut(self) -> None:
        """Best-effort helper to bypass systemd's automatic restart delay.

        For planned in-chat restarts, the gateway exits cleanly so systemd does
        not record a failure.  However, units with RestartSteps still count
        automatic restarts and can delay repeated /restart tests.  A transient
        user service survives our cgroup teardown and explicitly starts the
        gateway as soon as this PID exits, while the unit keeps its normal
        backoff for real crash loops.
        """
        from gateway.run import logger
        if sys.platform != "linux" or not os.environ.get("INVOCATION_ID"):
            return

        try:
            import shutil
            import subprocess

            systemd_run = shutil.which("systemd-run")
            systemctl = shutil.which("systemctl")
            if not systemd_run or not systemctl:
                return

            try:
                from hermes_cli.gateway import get_service_name

                service_name = get_service_name()
            except Exception:
                service_name = "hermes-gateway"

            current_pid = os.getpid()

            # Detect whether the gateway unit is registered as a system or
            # user service.  Daemon-style deployments are typically system
            # units (e.g. /etc/systemd/system/hermes-gateway.service), while
            # `hermes setup` under a non-root account may register a user
            # unit.  Hard-coding ``--user`` broke system-unit deployments:
            # systemctl returned an empty MainPID, the PID-equality check
            # below failed, and the planned-restart helper was never
            # launched — leaving the gateway dead until a manual reboot.
            def _query_pid(scope_flags):
                try:
                    out = subprocess.run(
                        [systemctl, *scope_flags, "show", service_name,
                         "--property=MainPID", "--value"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2,
                    )
                    return (out.stdout or "").strip()
                except Exception:
                    return ""

            system_pid = _query_pid([])
            user_pid = _query_pid(["--user"])
            if str(current_pid) == system_pid:
                scope_flags = []
                systemctl_scope = "systemctl"
            elif str(current_pid) == user_pid:
                scope_flags = ["--user"]
                systemctl_scope = "systemctl --user"
            else:
                # MainPID does not match in either scope — likely invoked
                # outside of systemd or the unit was renamed.  Bail out
                # rather than restart the wrong unit.
                return

            service_arg = shlex.quote(service_name)
            shell_cmd = (
                f"while kill -0 {current_pid} 2>/dev/null; do sleep 0.2; done; "
                f"{systemctl_scope} reset-failed {service_arg}; "
                f"{systemctl_scope} restart {service_arg}"
            )
            unit_name = f"{service_name}-planned-restart-{current_pid}".replace(".", "-")
            subprocess.Popen(
                [
                    systemd_run,
                    *scope_flags,
                    "--collect",
                    "--unit",
                    unit_name,
                    "/bin/sh",
                    "-lc",
                    shell_cmd,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            logger.info(
                "Launched systemd planned-restart helper for %s (pid=%s, scope=%s)",
                service_name,
                current_pid,
                "user" if scope_flags else "system",
            )
        except Exception as e:
            logger.debug("Failed to launch systemd planned-restart helper: %s", e)

    def _wedged_agent_count(self) -> int:
        """Count running chat agents already past the inactivity timeout.

        A turn whose agent has recorded no activity (no API bytes, no tool
        progress) for longer than ``agent.gateway_timeout`` is wedged — the
        same threshold at which the turn reaper gives up on it. The restart
        after-turn wait must not treat such turns as work worth waiting for:
        a wedged agent pinned ``hermes update`` in "draining" for the full
        ``restart_after_turn_timeout`` cap because the drain counted it as
        active while its own inactivity watchdog had already declared it dead
        (Aug 2026, WhatsApp turn idle 30+ min, drain waited on it anyway).

        Returns 0 when the inactivity timeout is disabled (``gateway_timeout``
        0/unset ⇒ the operator opted into unbounded turns; the after-turn cap
        still bounds the wait). Cron/API-server work has no per-turn activity
        clock and is never counted as wedged. Pending sentinels are brand-new
        turns, never wedged. Fail-open per agent: an unreadable activity
        summary means "not wedged".
        """
        from gateway.run import _AGENT_PENDING_SENTINEL, _float_env
        timeout = _float_env("HERMES_AGENT_TIMEOUT", 1800)
        if timeout <= 0:
            return 0
        wedged = 0
        for agent in list((getattr(self, "_running_agents", None) or {}).values()):
            if agent is None or agent is _AGENT_PENDING_SENTINEL:
                continue
            summary_fn = getattr(agent, "get_activity_summary", None)
            if not callable(summary_fn):
                continue
            try:
                summary = summary_fn()
                if not isinstance(summary, dict):
                    continue
                idle = float(summary.get("seconds_since_activity", 0.0))
            except Exception:
                continue
            if idle >= timeout:
                wedged += 1
        return wedged

    def _awaitable_work_count(self) -> int:
        """Active work minus wedged turns — what the restart wait waits on."""
        return max(0, self._active_work_count() - self._wedged_agent_count())

    async def _await_active_work_before_restart(self) -> bool:
        """Wait for in-flight work to finish before entering ``stop()``.

        In-band restart used to call ``stop()`` immediately, which folded the
        requesting turn into the drain wait set and force-interrupted it at
        ``restart_drain_timeout`` (#77184). Instead we refuse new turns and
        wait here for active agents/cron/api work to reach zero, then let
        ``stop()`` run against an idle gateway (drain is instant).

        Turns already past the inactivity timeout are excluded from the wait
        (``_wedged_agent_count``): restart is usually the *remedy* for a
        wedged turn, so deferring it behind one inverts the point of the
        graceful path. ``stop()``'s drain interrupts them under
        ``restart_drain_timeout`` instead.

        Returns True when work drained to zero, False when the safety cap
        elapsed with work still active — or when only wedged work remains —
        (caller proceeds to ``stop()``, which may then interrupt remaining
        runs under ``restart_drain_timeout``).
        """
        from gateway.run import logger
        active = self._active_work_count()
        if active <= 0:
            return True

        awaitable = self._awaitable_work_count()
        if awaitable <= 0:
            logger.warning(
                "Restart requested with %d active work unit(s), all wedged "
                "past the inactivity timeout; skipping the after-turn wait "
                "and proceeding to stop()/drain which will interrupt them",
                active,
            )
            return False

        timeout = float(getattr(self, "_restart_after_turn_timeout", 0.0) or 0.0)
        if timeout <= 0:
            logger.info(
                "Restart requested with %d active work unit(s); "
                "restart_after_turn_timeout=0 — entering stop()/drain immediately",
                active,
            )
            return False

        logger.info(
            "Restart requested with %d active work unit(s); "
            "deferring stop() until they finish (cap=%.0fs) so in-flight "
            "turns are not amputated (#77184)",
            active,
            timeout,
        )
        try:
            self._update_runtime_status("draining")
        except Exception:
            pass

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_status_at = 0.0
        while self._awaitable_work_count() > 0:
            now = loop.time()
            if now >= deadline:
                logger.warning(
                    "Restart after-turn wait timed out after %.0fs with %d "
                    "still active; proceeding to stop()/drain which may "
                    "interrupt remaining work (#77184)",
                    timeout,
                    self._active_work_count(),
                )
                return False
            if (now - last_status_at) >= 30.0:
                logger.info(
                    "Restart deferred: waiting on %d active work unit(s) "
                    "(%d wedged and excluded; %.0fs remaining before force drain)",
                    self._awaitable_work_count(),
                    self._wedged_agent_count(),
                    deadline - now,
                )
                try:
                    self._update_runtime_status("draining")
                except Exception:
                    pass
                last_status_at = now
            await asyncio.sleep(0.1)

        if self._active_work_count() > 0:
            logger.warning(
                "Restart deferred wait: %d wedged work unit(s) remain; "
                "proceeding to stop()/drain which will interrupt them",
                self._active_work_count(),
            )
            return False

        logger.info(
            "Restart deferred wait complete — active work drained; "
            "proceeding to stop()"
        )
        return True

    def request_restart(self, *, detached: bool = False, via_service: bool = False) -> bool:
        from gateway.run import logger
        if self._restart_task_started:
            return False
        self._restart_requested = True
        self._restart_detached = detached
        self._restart_via_service = via_service
        self._restart_task_started = True
        # Refuse new turns immediately while in-flight work finishes.
        # Keep ``_running`` True so adapters stay connected and the active
        # turn can still deliver its final response (#77184).
        self._draining = True

        async def _run_restart() -> None:
            await self._await_active_work_before_restart()
            # Launch the detached helper only AFTER the after-turn wait.
            # Its deadline is drain_timeout+5 and covers stop() teardown —
            # launching earlier would fire `hermes gateway restart` while
            # the requesting turn was still running.
            if detached:
                try:
                    await self._launch_detached_restart_command()
                except Exception as e:
                    logger.error("Failed to launch detached gateway restart helper: %s", e)
            await asyncio.sleep(0.05)
            await self.stop(restart=True, detached_restart=detached, service_restart=via_service)

        # _run_restart is a short-lived self-terminating task (calls stop()
        # then returns).  Don't add it to _background_tasks — _stop_impl
        # cancels all entries in that set, which would cancel _run_restart
        # while it's awaiting _stop_task, propagating CancelledError into
        # _stop_impl and preventing _shutdown_event.set() / _exit_code = 75.
        # See #12875.
        #
        # We still hold a strong reference in self._restart_task: a bare
        # asyncio.create_task() keeps only a weak reference, so the event
        # loop may garbage-collect a still-pending task mid-flight.  The
        # cancel loop in _stop_impl explicitly skips _restart_task for the
        # same reason it skips _stop_task.
        self._restart_task = asyncio.create_task(_run_restart())
        return True

    # Drain-timeout reasons set by _stop_impl() when a still-running turn is
    # force-interrupted; "restart_interrupted" is set by
    # SessionStore.suspend_recently_active() on crash recovery (no
    # .clean_shutdown marker).  All three mean "the agent was mid-turn and
    # we killed it" — eligible for startup auto-resume.
    _AUTO_RESUME_REASONS = frozenset(
        {"restart_timeout", "shutdown_timeout", "restart_interrupted"}
    )

    async def _run_startup_resume_event(
        self,
        adapter: BasePlatformAdapter,
        event: MessageEvent,
        session_key: str,
    ) -> None:
        """Dispatch one synthetic startup resume and wait for its agent turn.

        ``BasePlatformAdapter.handle_message()`` returns after it installs the
        adapter-level guard and spawns the background processing task.  Startup
        restore needs a stronger boundary: inbound messages must stay queued
        until the resumed agent turn itself has finished, otherwise a user
        message can race the restore turn immediately after ``handle_message``
        returns.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL
        try:
            await adapter.handle_message(event)
            session_tasks = getattr(adapter, "_session_tasks", {})
            task = session_tasks.get(session_key) if isinstance(session_tasks, dict) else None
            if task is not None:
                await asyncio.shield(task)
        finally:
            # _schedule_resume_pending_sessions pre-claims the runner slot
            # before spawning this task.  If adapter.handle_message raises
            # before _handle_message takes ownership, release that pre-claim;
            # otherwise the real run's normal cleanup owns the slot.
            _pre_state = self._peek_session_state(session_key)
            if (_pre_state.turn.agent if _pre_state else None) is _AGENT_PENDING_SENTINEL:
                self._release_running_agent_state(session_key)

    def _queue_startup_restore_event(self, event: MessageEvent) -> None:
        from gateway.run import logger
        queue = getattr(self, "_startup_restore_queue", None)
        if queue is None:
            queue = []
            self._startup_restore_queue = queue
        queue.append(event)
        try:
            source = event.source
            logger.info(
                "Queued inbound message during gateway startup restore: platform=%s chat=%s",
                source.platform.value if source and source.platform else "unknown",
                source.chat_id if source else "unknown",
            )
        except Exception:
            pass

    async def _drain_startup_restore_queue(self) -> int:
        """Replay inbound messages queued while startup auto-resume ran."""
        from gateway.run import logger
        drained = 0
        queue = getattr(self, "_startup_restore_queue", None)
        if queue is None:
            return 0
        while queue:
            event = queue.pop(0)
            source = getattr(event, "source", None)
            adapter = self._adapter_for_source(source)
            if adapter is None:
                logger.debug(
                    "Dropping startup-restore queued message: adapter unavailable for %s",
                    getattr(getattr(source, "platform", None), "value", None),
                )
                continue
            # Mark this replay so _handle_message does not queue it again while
            # the restore gate remains closed for any fresh inbound arrivals.
            try:
                setattr(event, "_hermes_startup_restore_replay", True)
            except Exception:
                pass
            await adapter.handle_message(event)
            drained += 1
        return drained

    async def _finish_startup_restore(self) -> None:
        """Wait (BOUNDED) for startup auto-resume, then release + drain inbound.

        The wait is bounded by ``_startup_restore_drain_timeout_secs`` so that
        a single pathologically long boot-resume turn cannot hold the inbound
        gate shut for every channel.  On timeout we release the gate and let
        the still-running resume turn(s) finish in the background — they are
        NOT cancelled.  This is safe because duplicate-agent protection does
        not depend on the wait: ``_schedule_resume_pending_sessions`` claims
        each session's ``_running_agents`` slot SYNCHRONOUSLY before this gate
        runs, so any inbound message drained while a resume turn is still in
        flight queues behind that slot instead of spawning a second agent.
        """
        from gateway.run import _startup_restore_drain_timeout_secs, logger
        tasks = list(getattr(self, "_startup_restore_tasks", []) or [])
        if tasks:
            timeout = _startup_restore_drain_timeout_secs()
            if timeout > 0:
                # asyncio.wait (unlike wait_for / gather+timeout) does NOT
                # cancel the pending tasks on timeout — the slow resume turn
                # keeps running in the background instead of being killed.
                done, pending = await asyncio.wait(tasks, timeout=timeout)
                if pending:
                    logger.warning(
                        "Startup-restore gate released after %.0fs with %d boot "
                        "auto-resume turn(s) still running; draining inbound "
                        "queue now (resume slots already claimed, so no "
                        "duplicate agents). Slow turn(s) continue in the "
                        "background.",
                        timeout,
                        len(pending),
                    )
                    # These tasks outlive the gate.  Their normal done-callback
                    # only discards them from _background_tasks, so a LATER
                    # failure would be silently swallowed.  Attach a logging
                    # callback so a background resume turn that fails after the
                    # timeout is still recorded.
                    for task in pending:
                        task.add_done_callback(self._log_background_resume_result)
            else:
                # Non-positive timeout => opt out of the bound (historical
                # "wait forever" behaviour).
                await asyncio.gather(*tasks, return_exceptions=True)
                done = set(tasks)
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    logger.debug(
                        "startup auto-resume task failed",
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
        self._startup_restore_tasks = []
        drained = await self._drain_startup_restore_queue()
        self._startup_restore_in_progress = False
        if drained:
            logger.info("Drained %d inbound message(s) queued during startup restore", drained)

    @staticmethod
    def _log_background_resume_result(task: "asyncio.Task") -> None:
        """Done-callback for a boot-resume turn that outlived the
        startup-restore gate.  Logs a late failure that would otherwise be
        swallowed once the task is discarded from ``_background_tasks``.
        Cancellation is expected (shutdown) and is not an error."""
        from gateway.run import logger
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.debug(
                "background startup auto-resume task failed after gate release",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def _redeliver_pending_obligations(self) -> int:
        """Redeliver final responses recorded in the delivery ledger by a
        previous (now dead) gateway process.

        Runs at startup BEFORE ``_schedule_resume_pending_sessions``. A
        session with a recoverable obligation already produced its answer —
        the turn completed and only delivery is owed — so this method sends
        the stored text and clears ``resume_pending`` for that session,
        preventing the resume path from re-running (and re-paying for) a
        turn whose output we hold.

        Crash-ambiguity contract (see gateway/delivery_ledger.py):
        rows that were mid-send or previously rejected carry a visible
        recovered-reply marker so a possible duplicate is labeled, never
        silent. Returns the number of redeliveries attempted.
        """
        from gateway.run import logger
        try:
            from gateway.delivery_ledger import (
                RECOVERED_MARKER,
                ledger_enabled,
                mark_delivered,
                mark_failed,
                sweep_recoverable,
            )

            if not await asyncio.to_thread(ledger_enabled):
                return 0
            # Only claim rows we can actually send this boot: self.adapters
            # holds a platform only after its connect() succeeded, and each
            # claim spends one of the row's three redelivery attempts.
            _deliverable = {
                getattr(p, "value", str(p)) for p in self.adapters
            }
            claimed = await asyncio.to_thread(
                sweep_recoverable, None, deliverable_platforms=_deliverable
            )
        except Exception:
            logger.debug("delivery ledger sweep failed", exc_info=True)
            return 0
        if not claimed:
            return 0

        redelivered = 0
        for row in claimed:
            try:
                platform = Platform(row["platform"])
            except Exception:
                logger.debug(
                    "obligation %s: unknown platform %r",
                    row["obligation_id"], row.get("platform"),
                )
                continue
            adapter = self.adapters.get(platform)
            if adapter is None:
                # Platform not connected this boot — leave the row claimed;
                # attempts cap + stale cutoff bound the retries on later boots.
                continue
            content = row["content"]
            if row.get("needs_marker"):
                content = RECOVERED_MARKER + content
            metadata = (
                {"thread_id": row["thread_id"]} if row.get("thread_id") else None
            )
            try:
                result = await adapter.send(
                    chat_id=row["chat_id"],
                    content=content,
                    metadata=metadata,
                )
            except Exception as send_err:
                logger.warning(
                    "obligation %s: redelivery send raised: %s",
                    row["obligation_id"], send_err,
                )
                result = None
            try:
                if result is not None and getattr(result, "success", False):
                    await asyncio.to_thread(mark_delivered, row["obligation_id"])
                    redelivered += 1
                    logger.info(
                        "Redelivered recovered final response to %s:%s "
                        "(obligation %s, attempt %d)",
                        row["platform"], row["chat_id"],
                        row["obligation_id"], row["attempts"],
                    )
                else:
                    await asyncio.to_thread(
                        mark_failed,
                        row["obligation_id"],
                        str(getattr(result, "error", "") or "send failed"),
                    )
            except Exception:
                logger.debug("delivery ledger update failed", exc_info=True)

            # The answer reached (or was owed to) this session — don't ALSO
            # re-run the turn via the resume path.
            session_key = row.get("session_key") or ""
            if session_key:
                try:
                    await self.async_session_store.clear_resume_pending(session_key)
                except Exception:
                    logger.debug(
                        "clear_resume_pending failed for %s", session_key,
                        exc_info=True,
                    )
        return redelivered

    def _schedule_resume_pending_sessions(self, platform=None) -> int:
        """Auto-continue fresh restart-interrupted sessions after startup.

        ``resume_pending`` already preserves the transcript AND the existing
        ``_is_resume_pending`` branch in ``_handle_message_with_agent``
        injects a reason-aware recovery system note on the next turn.  This
        method closes the UX gap by synthesizing that next turn once
        adapters are back online — the event text is empty so the existing
        injection path owns the wording and we never double up.

        Adapters that are not yet ready (adapter missing from
        ``self.adapters``) are skipped silently; their sessions stay
        ``resume_pending`` and will auto-resume on the next real user
        message, or when the platform reconnects — the reconnect watcher
        calls this again scoped to that ``platform``.

        ``platform`` (a ``Platform``) restricts the pass to sessions that
        originated on that platform.  The reconnect path passes it so a
        platform coming back online retries only its own sessions and never
        re-touches another platform's in-flight recoveries.  Sessions whose
        agent is already running are skipped regardless, so a session
        scheduled at startup is never resumed a second time.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL, _auto_continue_freshness_window, logger
        window = _auto_continue_freshness_window()
        try:
            with self.session_store._lock:  # noqa: SLF001 — snapshot under lock
                self.session_store._ensure_loaded_locked()  # noqa: SLF001
                candidates = [
                    entry for entry in self.session_store._entries.values()  # noqa: SLF001
                    if entry.resume_pending
                    and not entry.suspended
                    and entry.origin is not None
                    and entry.resume_reason in self._AUTO_RESUME_REASONS
                    and (platform is None or entry.origin.platform == platform)
                ]
        except Exception as exc:
            logger.warning("Failed to enumerate resume-pending sessions: %s", exc)
            return 0

        # Defense-3 (#30719): break the SIGTERM-respawn loop. Only count this
        # boot when there are restart-interrupted sessions to resume — a clean
        # boot must not accrue toward the breaker. If too many such boots have
        # happened in the configured window, skip auto-resume for THIS boot:
        # the gateway still comes up and serves real inbound messages, it just
        # stops replaying the session that keeps killing it. The session stays
        # resume_pending, so a real user message can still continue it (a human
        # is now in the loop). Defenses 1-2 cover the cron/CLI/terminal paths;
        # this catches every other SIGTERM source (e.g. a raw `terminal(
        # "launchctl kickstart ai.hermes.gateway")`).
        if candidates:
            try:
                from gateway import restart_loop_guard as _rlg

                _max_restarts, _window, _max_gap = self._restart_loop_guard_config()
                if _rlg.check_and_record(
                    _max_restarts, _window, max_gap_seconds=_max_gap
                ):
                    return 0
            except Exception as exc:  # noqa: BLE001 — breaker must fail OPEN
                logger.debug("Restart-loop guard check skipped: %s", exc)

        now = datetime.now()
        scheduled = 0
        for entry in candidates:
            marker = entry.last_resume_marked_at or entry.updated_at
            if marker is not None and (now - marker).total_seconds() > window:
                continue

            # Already being resumed (e.g. scheduled at startup and still
            # in-flight) — don't synthesize a second continuation turn.
            if self._is_session_running(entry.session_key):
                continue

            source = entry.origin
            adapter = self._adapter_for_source(source)
            if adapter is None:
                logger.debug(
                    "Skipping auto-resume for %s: adapter not ready for %s",
                    entry.session_key,
                    getattr(source.platform, "value", source.platform),
                )
                continue

            # Validate the session owner against the current allowlist
            # before auto-resuming. A session created before
            # TELEGRAM_ALLOWED_USERS (or equivalent) was configured, or
            # before the owner was removed from it, must not silently
            # receive a full agent response on gateway restart just
            # because it has a resume-pending marker (issue #23778).
            try:
                if not self._is_user_authorized(source):
                    logger.warning(
                        "Skipping auto-resume for %s: session owner is no "
                        "longer authorized under the current allowlist",
                        entry.session_key,
                    )
                    continue
            except Exception as exc:
                logger.warning(
                    "Skipping auto-resume for %s: authorization check failed: %s",
                    entry.session_key, exc,
                )
                continue

            # Claim the session slot *before* spawning the task so that an
            # inbound message arriving between task creation and the task's
            # first await (where _process_message_background sets the real
            # sentinel) sees the slot as occupied and queues behind it
            # instead of spinning up a duplicate AIAgent (#45456).
            _resume_state = self._session_state(entry.session_key)
            _resume_state.turn.agent = _AGENT_PENDING_SENTINEL
            _resume_state.turn.started_ts = time.time()
            self._persist_active_agents()

            # Empty-text internal event — the _is_resume_pending branch in
            # _handle_message_with_agent prepends the proper reason-aware
            # system note before the turn runs.
            event = MessageEvent(
                text="",
                message_type=MessageType.TEXT,
                source=source,
                internal=True,
            )
            task = asyncio.create_task(
                self._run_startup_resume_event(adapter, event, entry.session_key)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            if getattr(self, "_startup_restore_in_progress", False):
                tasks = getattr(self, "_startup_restore_tasks", None)
                if tasks is None:
                    tasks = []
                    self._startup_restore_tasks = tasks
                tasks.append(task)
            scheduled += 1
        if scheduled:
            logger.info(
                "Scheduled auto-resume for %d restart-interrupted session(s)",
                scheduled,
            )
        return scheduled

    def _startup_should_abort(self) -> bool:
        return (
            self._restart_requested
            or self._draining
            or self._shutdown_event.is_set()
        )

    async def _abort_startup_if_shutdown_requested(
        self,
        adapter: Optional[BasePlatformAdapter] = None,
        platform: Optional[Platform] = None,
    ) -> bool:
        """Clean up and exit startup when restart/shutdown begins mid-startup."""
        from gateway.run import logger
        if not self._startup_should_abort():
            return False
        if adapter is not None and platform is not None:
            try:
                await adapter.cancel_background_tasks()
            except Exception as e:
                logger.debug("✗ %s background-task cancel error: %s", platform.value, e)
            await self._safe_adapter_disconnect(adapter, platform)
        stop_task = self._stop_task
        current_task = asyncio.current_task()
        if stop_task is not None and stop_task is not current_task:
            await stop_task
        elif not self._shutdown_event.is_set():
            await self.stop(
                restart=self._restart_requested,
                detached_restart=self._restart_detached,
                service_restart=self._restart_via_service,
            )
        return True

    def _start_loop_liveness_guards(self, loop: asyncio.AbstractEventLoop) -> None:
        """Arm the selector floor and out-of-loop watchdog before adapters.

        Disabled entirely with ``gateway.loop_watchdog: false`` in config.yaml
        (no env override — config-only knob, #69089).
        """
        from gateway.run import logger
        config = getattr(self, "config", None)
        if config is not None and not getattr(config, "loop_watchdog", True):
            return
        if getattr(self, "_loop_floor_timer_handle", None) is None:
            try:
                self._loop_floor_timer_handle = _arm_loop_floor_timer(loop)
            except Exception:
                logger.debug("Failed to arm gateway loop floor timer", exc_info=True)

        watchdog = getattr(self, "_loop_liveness_watchdog", None)
        if watchdog is None or not watchdog.is_alive():
            try:
                self._loop_liveness_watchdog = start_loop_liveness_watchdog(loop)
            except Exception:
                logger.debug("Failed to start gateway loop liveness watchdog", exc_info=True)

    def _stop_loop_liveness_guards(self) -> None:
        """Disarm lifetime liveness guards before shutdown can load the loop."""
        from gateway.run import logger
        watchdog = getattr(self, "_loop_liveness_watchdog", None)
        self._loop_liveness_watchdog = None
        if watchdog is not None:
            try:
                watchdog.stop()
            except Exception:
                logger.debug("Failed to stop gateway loop liveness watchdog", exc_info=True)

        floor_timer = getattr(self, "_loop_floor_timer_handle", None)
        self._loop_floor_timer_handle = None
        if floor_timer is not None:
            try:
                floor_timer.cancel()
            except Exception:
                logger.debug("Failed to cancel gateway loop floor timer", exc_info=True)

    async def _consume_clean_shutdown_marker(self, marker_path) -> int:
        """Discard orphan turn markers before consuming a clean-exit receipt.

        If either persistence or marker removal fails, startup must fail closed.
        Continuing with the old receipt would let a later unclean exit masquerade
        as clean and discard genuinely interrupted turns.
        """
        discarded = await self.async_session_store.discard_active_turn_markers()
        marker_path.unlink()
        return discarded

    async def _recover_unclean_sessions(self) -> tuple[int, int]:
        """Recover exact active turns, then run the legacy recency fallback."""
        from gateway.run import _float_env, logger
        exact = 0
        fallback = 0
        try:
            agent_timeout = max(1.0, _float_env("HERMES_AGENT_TIMEOUT", 1800))
            marker_max_age = max(60 * 60, int(agent_timeout * 2))
            exact = await self.async_session_store.recover_interrupted_turns(
                max_age_seconds=marker_max_age
            )
        except Exception as exc:
            logger.warning("Exact active-turn recovery on startup failed: %s", exc)
        try:
            fallback = await self.async_session_store.suspend_recently_active(
                max_age_seconds=120
            )
        except Exception as exc:
            logger.warning("Legacy session recovery on startup failed: %s", exc)
        return exact, fallback

    async def start(self) -> bool:
        """
        Start the gateway and all configured platform adapters.
        
        Returns True if at least one adapter connected successfully.
        """
        from gateway.run import MultiplexConfigError, _OWN_POLICY_OPEN_ENV, _clear_planned_restart_notification, _hermes_home, _own_policy_open_startup_violation, _planned_restart_notification_pending, _platform_has_bot_credential, _restart_notification_pending, get_hermes_home, logger
        logger.info("Starting Hermes Gateway...")
        # Enable faulthandler for stack dumps on freezes/crashes (#70344).
        # Falls back to a log file when sys.stderr is None (Windows VBS /
        # pythonw / detached service) — otherwise the gateway would die
        # here and take every adapter offline. See #71671.
        try:
            faulthandler.enable()
        except (RuntimeError, ValueError, OSError):
            try:
                _fh_log_dir = getattr(self.config, "log_dir", None) or os.path.join(
                    str(get_hermes_home()),
                    "logs",
                )
                os.makedirs(_fh_log_dir, exist_ok=True)
                _fh_enable_path = os.path.join(_fh_log_dir, "gateway_faulthandler.log")
                _fh_enable_file = open(_fh_enable_path, "a", encoding="utf-8")
                faulthandler.enable(file=_fh_enable_file, all_threads=True)
            except Exception:
                logger.debug("faulthandler.enable() unavailable", exc_info=True)
        # Also dump stacks to a rotating file for off-line analysis when
        # the gateway is running under a service manager that doesn't
        # capture stderr.
        # faulthandler.register() and SIGUSR2 are POSIX-only; skip the
        # signal-triggered file dump on Windows (faulthandler.enable()
        # above still covers fatal-error dumps there).
        _sigusr2 = getattr(signal, "SIGUSR2", None)
        if _sigusr2 is not None and hasattr(faulthandler, "register"):
            try:
                _log_dir = getattr(self.config, "log_dir", None) or os.path.join(
                    str(get_hermes_home()),
                    "logs",
                )
                _faulthandler_path = os.path.join(_log_dir, "gateway_faulthandler.log")
                os.makedirs(_log_dir, exist_ok=True)
                _fh = open(_faulthandler_path, "a", encoding="utf-8")
                faulthandler.register(
                    _sigusr2,
                    file=_fh,
                    all_threads=True,
                    chain=True,
                )
            except Exception:
                logger.debug("Could not set up faulthandler file logging", exc_info=True)

        try:
            self._gateway_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._gateway_loop = None
        if self._gateway_loop is not None:
            self._start_loop_liveness_guards(self._gateway_loop)
        logger.info("Session storage: %s", self.config.sessions_dir)

        # Sanity-check that systemd's TimeoutStopSec covers our drain
        # window.  When the user upgraded hermes-agent without re-running
        # ``hermes setup``, their unit file may still encode the old
        # default — in which case SIGKILL hits mid-drain and looks like
        # a phantom kill in the journal.  Best-effort, never raises.
        try:
            from gateway.shutdown_forensics import check_systemd_timing_alignment
            _alignment = check_systemd_timing_alignment(self._restart_drain_timeout)
            if _alignment is not None and _alignment.get("mismatch"):
                logger.warning(
                    "Stale systemd unit detected: %s has TimeoutStopSec=%.0fs but "
                    "drain_timeout=%.0fs (expected >=%.0fs). systemd may SIGKILL the "
                    "gateway mid-drain. Run `hermes gateway install --force` "
                    "to regenerate the unit, or shorten agent.restart_drain_timeout.",
                    _alignment.get("unit", "(unknown)"),
                    _alignment["timeout_stop_sec"],
                    _alignment["drain_timeout"],
                    _alignment["expected_min"],
                )
        except Exception as _e:
            logger.debug("check_systemd_timing_alignment failed: %s", _e)
        # Log the resolved max_iterations budget so operators can verify the
        # config.yaml → env bridge did the right thing at a glance (instead
        # of silently running at a stale .env value for weeks).
        try:
            _effective_max_iter = int(os.getenv("HERMES_MAX_ITERATIONS", "500"))
            logger.info(
                "Agent budget: max_iterations=%d (agent.max_turns from config.yaml, "
                "or HERMES_MAX_ITERATIONS from .env, or default 500)",
                _effective_max_iter,
            )
        except Exception:
            pass
        # Redaction status: ON by default (#17691). Surface a prominent
        # warning if an operator has explicitly opted out so they don't
        # forget the downgrade is active — the redactor snapshots its
        # state at import time, so this log line is the source of truth
        # for this process's lifetime.
        try:
            _redact_raw = os.getenv("HERMES_REDACT_SECRETS", "true")
            _redact_on = _redact_raw.lower() in {"1", "true", "yes", "on"}
            if _redact_on:
                logger.info(
                    "Secret redaction: ENABLED (tool output, logs, and chat "
                    "responses are scrubbed before delivery)"
                )
            else:
                logger.warning(
                    "Secret redaction: DISABLED (HERMES_REDACT_SECRETS=%s). "
                    "API keys and tokens may appear verbatim in chat output, "
                    "session JSONs, and logs. Set security.redact_secrets: true "
                    "in config.yaml to re-enable.",
                    _redact_raw,
                )
        except Exception:
            pass
        try:
            from hermes_cli.profiles import get_active_profile_name
            _profile = get_active_profile_name()
            if _profile and _profile != "default":
                logger.info("Active profile: %s", _profile)
        except Exception:
            pass
        try:
            from gateway.status import write_runtime_status
            write_runtime_status(gateway_state="starting", exit_reason=None)
        except Exception:
            pass
        try:
            from hermes_cli.config import load_config
            from agent.monitoring.gateway_health_export import start_gateway_health_export
            self._gateway_health_export_runtime = start_gateway_health_export(load_config())
            if getattr(self._gateway_health_export_runtime, "enabled", False):
                logger.info("Gateway health OTLP export: enabled")
        except Exception:
            logger.debug("gateway health OTLP export startup failed", exc_info=True)

        # Log any active supply-chain security advisories. Operators see this
        # in gateway.log and `hermes status` surfaces it; we do NOT block
        # startup or surface it inline to user messages, since the gateway
        # operator is the one who can act on it (uninstall the package,
        # rotate credentials).  See hermes_cli/security_advisories.py.
        try:
            from hermes_cli.security_advisories import (
                detect_compromised,
                gateway_log_message,
            )
            _adv_hits = detect_compromised()
            _adv_msg = gateway_log_message(_adv_hits)
            if _adv_msg:
                logger.warning("%s", _adv_msg)
                logger.warning(
                    "Run `hermes doctor` on the gateway host for full "
                    "remediation steps."
                )
        except Exception:
            logger.debug(
                "security advisory check failed at gateway startup",
                exc_info=True,
            )
        if await self._abort_startup_if_shutdown_requested():
            return True
        
        # Warn if no user allowlists are configured and open access is not opted in
        _builtin_allowed_vars = (
            "TELEGRAM_ALLOWED_USERS", "DISCORD_ALLOWED_USERS",
            "WHATSAPP_ALLOWED_USERS", "WHATSAPP_CLOUD_ALLOWED_USERS",
            "SLACK_ALLOWED_USERS",
            "SIGNAL_ALLOWED_USERS", "SIGNAL_GROUP_ALLOWED_USERS",
            "TELEGRAM_GROUP_ALLOWED_USERS",
            "TELEGRAM_GROUP_ALLOWED_CHATS",
            "EMAIL_ALLOWED_USERS",
            "SMS_ALLOWED_USERS", "MATTERMOST_ALLOWED_USERS",
            "MATRIX_ALLOWED_USERS", "DINGTALK_ALLOWED_USERS",
            "FEISHU_ALLOWED_USERS",
            "WECOM_ALLOWED_USERS",
            "WECOM_CALLBACK_ALLOWED_USERS",
            "WEIXIN_ALLOWED_USERS",
            "BLUEBUBBLES_ALLOWED_USERS",
            "QQ_ALLOWED_USERS",
            "YUANBAO_ALLOWED_USERS",
            "GATEWAY_ALLOWED_USERS",
        )
        _builtin_allow_all_vars = (
            "TELEGRAM_ALLOW_ALL_USERS", "DISCORD_ALLOW_ALL_USERS",
            "WHATSAPP_ALLOW_ALL_USERS", "WHATSAPP_CLOUD_ALLOW_ALL_USERS",
            "SLACK_ALLOW_ALL_USERS",
            "SIGNAL_ALLOW_ALL_USERS", "EMAIL_ALLOW_ALL_USERS",
            "SMS_ALLOW_ALL_USERS", "MATTERMOST_ALLOW_ALL_USERS",
            "MATRIX_ALLOW_ALL_USERS", "DINGTALK_ALLOW_ALL_USERS",
            "FEISHU_ALLOW_ALL_USERS",
            "WECOM_ALLOW_ALL_USERS",
            "WECOM_CALLBACK_ALLOW_ALL_USERS",
            "WEIXIN_ALLOW_ALL_USERS",
            "BLUEBUBBLES_ALLOW_ALL_USERS",
            "QQ_ALLOW_ALL_USERS",
            "YUANBAO_ALLOW_ALL_USERS",
        )
        # Also pick up plugin-registered platforms — each entry can declare
        # its own allowed_users_env / allow_all_env, so the warning stays
        # accurate as plugins like IRC come online.
        _plugin_allowed_vars: tuple = ()
        _plugin_allow_all_vars: tuple = ()
        try:
            from gateway.platform_registry import platform_registry
            _plugin_allowed_vars = tuple(
                e.allowed_users_env for e in platform_registry.plugin_entries()
                if e.allowed_users_env
            )
            _plugin_allow_all_vars = tuple(
                e.allow_all_env for e in platform_registry.plugin_entries()
                if e.allow_all_env
            )
        except Exception:
            pass
        _any_allowlist = any(
            os.getenv(v) for v in _builtin_allowed_vars + _plugin_allowed_vars
        )
        _allow_all = os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in {"true", "1", "yes"} or any(
            os.getenv(v, "").lower() in {"true", "1", "yes"}
            for v in _builtin_allow_all_vars + _plugin_allow_all_vars
        )
        if not _any_allowlist and not _allow_all:
            logger.warning(
                "No env user allowlists configured. Messaging platforms default to "
                "pairing/allowlist policies and will deny unknown senders unless you "
                "configure platform allowlists (e.g., TELEGRAM_ALLOWED_USERS=your_id) "
                "or explicitly opt in with GATEWAY_ALLOW_ALL_USERS=true plus "
                "dm_policy/group_policy: open on the platform."
            )

        reason = _own_policy_open_startup_violation(self.config)
        if reason:
            platform_value = reason.split(":", 1)[0]
            allow_all_env = None
            for platform, open_env in _OWN_POLICY_OPEN_ENV.items():
                if platform.value == platform_value:
                    allow_all_env = open_env[2]
                    break
            logger.error(
                "Refusing to start: %s has dm_policy/group_policy set to 'open' "
                "but neither GATEWAY_ALLOW_ALL_USERS nor %s is enabled.",
                platform_value,
                allow_all_env or "a platform allow-all flag",
            )
            try:
                from gateway.status import write_runtime_status
                write_runtime_status(gateway_state="startup_failed", exit_reason=reason)
            except Exception:
                pass
            self._request_clean_exit(reason)
            return True
        
        # Discover Python plugins before shell hooks so plugin block
        # decisions take precedence in tie cases.  The CLI startup path
        # does this via an explicit call in hermes_cli/main.py; the
        # gateway lazily imports run_agent inside per-request handlers,
        # so the discover_plugins() side-effect in model_tools.py is NOT
        # guaranteed to have run by the time we reach this point.
        try:
            from hermes_cli.plugins import discover_plugins
            discover_plugins()
        except Exception:
            logger.warning(
                "plugin discovery failed at gateway startup", exc_info=True,
            )

        # Register the generic relay adapter when a connector relay URL is
        # configured (GATEWAY_RELAY_URL / gateway.relay_url). No URL -> no-op, so
        # direct/single-tenant deployments are unaffected. When configured, the
        # adapter dials the connector over a WebSocket, negotiates its capability
        # descriptor at handshake, and bridges inbound/outbound like any platform.
        try:
            from gateway.relay import (
                register_relay_adapter,
                relay_url,
                self_provision_relay,
                send_relay_policy,
            )

            # Boot-time relay self-provision: resolve the agent's NAS token ->
            # POST /relay/provision -> set GATEWAY_RELAY_* in os.environ BEFORE
            # registration reads them. No-op when relay is unconfigured, a secret
            # is already pinned, or no NAS token resolves (self-hosted, unenrolled).
            # Never raises.
            self_provision_relay()

            if register_relay_adapter():
                logger.info("relay adapter registered (connector at %s)", relay_url())
                # Declare this gateway's relevance policy (mention-gating /
                # free-response / allow-bots) to the connector so the SAME
                # behavior governs relay delivery (Phase 6 Unit ζ). Runs after
                # the secret is resolved; never raises, never blocks boot.
                send_relay_policy()
        except Exception:
            logger.warning(
                "relay adapter registration failed at gateway startup", exc_info=True,
            )

        # Register declarative shell hooks from cli-config.yaml.  Gateway
        # has no TTY, so consent has to come from one of the three opt-in
        # channels (--accept-hooks on launch, HERMES_ACCEPT_HOOKS env var,
        # or hooks_auto_accept: true in config.yaml).  We pass
        # accept_hooks=False here and let register_from_config resolve
        # the effective value from env + config itself — the CLI-side
        # registration already honored --accept-hooks, and re-reading
        # hooks_auto_accept here would just duplicate that lookup.
        # Failures are logged but must never block gateway startup.
        try:
            from hermes_cli.config import load_config
            from agent.shell_hooks import register_from_config
            _hooks_cfg = load_config()
            register_from_config(_hooks_cfg, accept_hooks=False)

            from agent.outbound_webhooks import (
                register_from_config as register_outbound_webhooks,
            )
            register_outbound_webhooks(_hooks_cfg)
        except Exception:
            logger.debug(
                "shell-hook registration failed at gateway startup",
                exc_info=True,
            )

        # Discover and load event hooks
        self.hooks.discover_and_load()

        
        # Recover background processes from checkpoint (crash recovery)
        try:
            from tools.process_registry import process_registry
            recovered = process_registry.recover_from_checkpoint()
            if recovered:
                logger.info("Recovered %s background process(es) from previous run", recovered)
        except Exception as e:
            logger.warning("Process checkpoint recovery: %s", e)

        # Recover sessions that were active when the gateway last exited.
        # Exact durable turn markers cover long-running work; the 120-second
        # recency heuristic remains as an upgrade fallback for turns started by
        # older Hermes versions that did not write exact markers.
        #
        # SKIP suspension after a clean (graceful) shutdown — the previous
        # process already drained active agents, so sessions aren't stuck.
        # This prevents unwanted auto-resets after `hermes update`,
        # `hermes gateway restart`, or `/restart`.
        _clean_marker = _hermes_home / ".clean_shutdown"
        if _clean_marker.exists():
            logger.info("Previous gateway exited cleanly — skipping session suspension")
            try:
                discarded = await self._consume_clean_shutdown_marker(_clean_marker)
            except Exception as exc:
                logger.error(
                    "Clean-start marker cleanup failed; refusing startup so the "
                    "clean-exit receipt cannot mask a later unclean exit: %s",
                    exc,
                )
                raise RuntimeError("clean-start recovery cleanup failed") from exc
            if discarded:
                logger.info(
                    "Discarded %d orphan active-turn marker(s) after clean shutdown",
                    discarded,
                )
        else:
            exact, fallback = await self._recover_unclean_sessions()
            recovered = exact + fallback
            if recovered:
                logger.info(
                    "Marked %d in-flight session(s) as resumable from previous run "
                    "(%d exact, %d legacy)",
                    recovered,
                    exact,
                    fallback,
                )

        # Stuck-loop detection (#7536): if a session has been active across
        # 3+ consecutive restarts, it's probably stuck in a loop (the same
        # history keeps causing the agent to hang).  Auto-suspend it so the
        # user gets a clean slate on the next message.
        try:
            stuck = self._suspend_stuck_loop_sessions()
            if stuck:
                logger.warning("Auto-suspended %d stuck-loop session(s)", stuck)
        except Exception as e:
            logger.debug("Stuck-loop detection failed: %s", e)

        # Serialize startup restore against inbound dispatch.  Platform
        # adapters can begin receiving messages as soon as they connect, but
        # restart-interrupted sessions are not auto-resumed until all startup
        # wiring below completes.  Queue inbound messages until the resume
        # pass runs and every synthetic resume turn has finished.
        self._startup_restore_in_progress = True
        self._startup_restore_queue = []
        self._startup_restore_tasks = []

        connected_count = 0
        enabled_platform_count = 0
        startup_nonretryable_errors: list[str] = []
        startup_retryable_errors: list[str] = []
        
        # Initialize and connect each configured platform
        _multiplex_on = bool(getattr(self.config, "multiplex_profiles", False))
        _multiplex_skipped_platforms: list[Platform] = []
        for platform, platform_config in self.config.platforms.items():
            if await self._abort_startup_if_shutdown_requested():
                return True
            if not platform_config.enabled:
                continue
            # Under multiplexing, a platform may be enabled on the default
            # profile's config.yaml while its bot token lives only in a
            # secondary profile's .env. Starting that primary adapter with an
            # empty token fails immediately and queues an infinite reconnect
            # loop that can never heal (#64674). Secondary profiles still
            # start their own adapters under _profile_runtime_scope with the
            # real token — skip the empty primary instead of failing loudly.
            if _multiplex_on and not _platform_has_bot_credential(platform, platform_config):
                logger.info(
                    "Skipping %s on default profile: no bot credential in this "
                    "profile's secrets. Secondary multiplexed profiles that "
                    "provide the token will still connect.",
                    platform.value,
                )
                _multiplex_skipped_platforms.append(platform)
                continue
            enabled_platform_count += 1
            
            adapter = self._create_adapter(platform, platform_config)
            if not adapter:
                # Distinguish between missing builtin deps and missing plugin
                _pval = platform.value
                _builtin_names = {m.value for m in Platform.__members__.values()}
                if _pval not in _builtin_names:
                    logger.warning(
                        "No adapter for '%s' — is the plugin installed? "
                        "(platform is enabled in config.yaml but no plugin registered it)",
                        _pval,
                    )
                else:
                    logger.warning("No adapter available for %s", _pval)
                continue
            
            # Set up message + fatal error handlers. Under multiplexing the
            # default profile needs the same whole-handler runtime scope as a
            # secondary profile: authorization and prompt rendering both run
            # before the narrower agent-turn scope is installed.
            adapter.set_message_handler(self._primary_message_handler())
            adapter.set_fatal_error_handler(self._handle_adapter_fatal_error)
            adapter.set_session_store(self.session_store)
            adapter.set_busy_session_handler(self._handle_active_session_busy_message)
            _set_reaction = getattr(adapter, "set_reaction_handler", None)
            if callable(_set_reaction):
                _set_reaction(self._handle_reaction_event)
            adapter.set_topic_recovery_fn(self._recover_telegram_topic_thread_id)
            adapter.set_authorization_check(self._make_adapter_auth_check(adapter.platform))
            adapter.set_platform_event_handler(self._primary_platform_event_handler())
            adapter._busy_text_mode = self._busy_text_mode
            
            # Try to connect
            logger.info("Connecting to %s...", platform.value)
            self._update_platform_runtime_status(
                platform.value,
                platform_state="connecting",
                error_code=None,
                error_message=None,
            )
            try:
                success = await self._connect_initial_adapter_with_timeout(
                    adapter, platform
                )
                if await self._abort_startup_if_shutdown_requested(adapter, platform):
                    return True
                if success:
                    self.adapters[platform] = adapter
                    self._sync_voice_mode_state_to_adapter(adapter)
                    # Wire voice input callback at connect time so voice
                    # transcription is forwarded without requiring /voice join.
                    if hasattr(adapter, "_voice_input_callback"):
                        adapter._voice_input_callback = self._handle_voice_channel_input
                    connected_count += 1
                    self._update_platform_runtime_status(
                        platform.value,
                        platform_state="connected",
                        error_code=None,
                        error_message=None,
                        needs_attention=False,
                        retrying_since=None,
                    )
                    logger.info("✓ %s connected", platform.value)
                else:
                    logger.warning("✗ %s failed to connect", platform.value)
                    # Defensive cleanup: a failed connect() may have
                    # allocated resources (aiohttp.ClientSession, poll
                    # tasks, bridge subprocesses) before giving up.
                    # Without this call, those resources are orphaned
                    # and Python logs "Unclosed client session" at
                    # process exit. Adapter disconnect() implementations
                    # are expected to be idempotent and tolerate
                    # partial-init state.
                    await self._safe_adapter_disconnect(adapter, platform)
                    if adapter.has_fatal_error:
                        self._update_platform_runtime_status(
                            platform.value,
                            platform_state="retrying" if adapter.fatal_error_retryable else "fatal",
                            error_code=adapter.fatal_error_code,
                            error_message=adapter.fatal_error_message,
                        )
                        target = (
                            startup_retryable_errors
                            if adapter.fatal_error_retryable
                            else startup_nonretryable_errors
                        )
                        target.append(
                            f"{platform.value}: {adapter.fatal_error_message}"
                        )
                        # Queue for reconnection if the error is retryable
                        if adapter.fatal_error_retryable:
                            self._failed_platforms[platform] = {
                                "config": platform_config,
                                "attempts": 1,
                                "next_retry": time.monotonic() + 30,
                                "queued_at": time.monotonic(),
                                "credential_claim": self._adapter_credential_claim(
                                    platform, adapter
                                ),
                                "listener_claim": self._adapter_listener_claim(
                                    platform, adapter
                                ),
                            }
                    else:
                        self._update_platform_runtime_status(
                            platform.value,
                            platform_state="retrying",
                            error_code=None,
                            error_message="failed to connect",
                        )
                        startup_retryable_errors.append(
                            f"{platform.value}: failed to connect"
                        )
                        # No fatal error info means likely a transient issue — queue for retry
                        self._failed_platforms[platform] = {
                            "config": platform_config,
                            "attempts": 1,
                            "next_retry": time.monotonic() + 30,
                            "queued_at": time.monotonic(),
                            "credential_claim": self._adapter_credential_claim(
                                platform, adapter
                            ),
                            "listener_claim": self._adapter_listener_claim(
                                platform, adapter
                            ),
                        }
            except Exception as e:
                logger.error("✗ %s error: %s", platform.value, e)
                # Same defensive cleanup path for exceptions — an adapter
                # that raised mid-connect may still have a live
                # aiohttp.ClientSession or child subprocess.
                await self._safe_adapter_disconnect(adapter, platform)
                self._update_platform_runtime_status(
                    platform.value,
                    platform_state="retrying",
                    error_code=None,
                    error_message=str(e),
                )
                startup_retryable_errors.append(f"{platform.value}: {e}")
                # Unexpected exceptions are typically transient — queue for retry
                self._failed_platforms[platform] = {
                    "config": platform_config,
                    "attempts": 1,
                    "next_retry": time.monotonic() + 30,
                    "queued_at": time.monotonic(),
                    "credential_claim": self._adapter_credential_claim(
                        platform, adapter
                    ),
                    "listener_claim": self._adapter_listener_claim(
                        platform, adapter
                    ),
                }
            if await self._abort_startup_if_shutdown_requested():
                return True

        # Multi-profile multiplexing: bring up adapters for every OTHER profile
        # this gateway serves. Each profile's adapters connect under that
        # profile's home + credential scope and stamp their inbound events with
        # the profile so the agent turn resolves correctly. No-op when off.
        try:
            _secondary_connected = await self._start_secondary_profile_adapters()
            connected_count += _secondary_connected
        except MultiplexConfigError as e:
            # Invalid multiplexer config — abort startup cleanly so the operator
            # fixes config.yaml rather than running a half-wired gateway.
            reason = str(e)
            logger.error("Gateway multiplexer config error: %s", reason)
            try:
                from gateway.status import write_runtime_status
                write_runtime_status(gateway_state="startup_failed", exit_reason=reason)
            except Exception:
                pass
            self._exit_code = GATEWAY_FATAL_CONFIG_EXIT_CODE
            self._request_clean_exit(reason)
            self._startup_restore_in_progress = False
            return True
        except Exception as e:
            logger.error("Secondary-profile adapter startup failed: %s", e, exc_info=True)
        finally:
            # Startup authority is one phase, not a persistent runner mode.
            # From this point onward every adapter retry is non-evicting.
            self._platform_lock_takeover_on_start = False

        # A platform we skipped on the primary for a missing credential was
        # supposed to be picked up by a secondary profile that owns the token.
        # If none did, the platform is enabled in config.yaml yet silently
        # unserved — surface it loudly so the operator sees a config problem
        # instead of a quiet dead channel (#64674 follow-up).
        for _skipped in _multiplex_skipped_platforms:
            _served_by_secondary = any(
                _skipped in _profile_map
                for _profile_map in self._profile_adapters.values()
            )
            if not _served_by_secondary:
                logger.warning(
                    "%s is enabled but no profile (default or secondary) "
                    "provided a bot credential for it — the platform is not "
                    "being served. Add its token to the profile that should "
                    "own it, or disable the platform.",
                    _skipped.value,
                )

        if connected_count == 0:
            if startup_nonretryable_errors and not startup_retryable_errors:
                reason = "; ".join(startup_nonretryable_errors)
                logger.error("Gateway hit a non-retryable startup conflict: %s", reason)
                try:
                    from gateway.status import write_runtime_status
                    write_runtime_status(gateway_state="startup_failed", exit_reason=reason)
                except Exception:
                    pass
                self._exit_code = GATEWAY_FATAL_CONFIG_EXIT_CODE
                self._request_clean_exit(reason)
                self._startup_restore_in_progress = False
                return True
            if startup_nonretryable_errors:
                # Mixed failure mode (NS-609): some platforms are fatally
                # misconfigured (e.g. WhatsApp enabled but never paired) while
                # others hit merely transient errors (e.g. Telegram TimedOut
                # during polling startup).  Exiting with
                # GATEWAY_FATAL_CONFIG_EXIT_CODE here is wrong in both
                # supervision worlds: under supervisors that honor the
                # exit-78 contract (systemd RestartPreventExitStatus, s6
                # finish→125 since #51228) the gateway goes PERMANENTLY down
                # over a network blip; under anything else it crash-loops.
                # Either way the retryable platforms never get their retry.
                # Log the fatal side loudly, then fall through to the
                # degraded/retry path below: the reconnect watcher recovers
                # the retryable platforms; the non-retryable ones remain
                # fatal-parked and visible in runtime status.
                logger.error(
                    "%d platform(s) fatally misconfigured and parked: %s. "
                    "Staying alive so retryable platforms can recover.",
                    len(startup_nonretryable_errors),
                    "; ".join(startup_nonretryable_errors),
                )
            if enabled_platform_count > 0:
                if startup_retryable_errors:
                    # All enabled platforms hit retryable failures (network
                    # blip, bridge not paired, npm install timeout, etc.).
                    # Keep the gateway alive so:
                    #   • cron jobs still run
                    #   • the reconnect watcher gets a chance to recover the
                    #     failing platforms once the underlying problem is
                    #     fixed (e.g. user runs `hermes whatsapp`, fixes
                    #     proxy, etc.)
                    # Exiting here used to convert a single misconfigured
                    # platform into an infinite systemd restart loop.
                    reason = "; ".join(startup_retryable_errors)
                    logger.warning(
                        "Gateway started with no connected platforms — "
                        "%d platform(s) queued for retry: %s",
                        len(self._failed_platforms), reason,
                    )
                    try:
                        from gateway.status import write_runtime_status
                        write_runtime_status(
                            gateway_state="degraded",
                            exit_reason=None,
                        )
                    except Exception:
                        pass
                    # Fall through to the normal "running" state — reconnect
                    # watcher takes it from here.
                # All enabled platforms had no adapter (missing library or credentials).
                # In fleet deployments the same config.yaml is shared across nodes that
                # may only have credentials for a subset of platforms.  Rather than
                # failing hard, degrade gracefully and allow cron jobs to run (#5196).
                logger.warning(
                    "No adapter could be created for any of the %d configured platform(s). "
                    "Check that required dependencies are installed and credentials are set. "
                    "Gateway will continue for cron job execution.",
                    enabled_platform_count,
                )
            else:
                logger.warning("No messaging platforms enabled.")
                logger.info("Gateway will continue running for cron job execution.")
        
        # Update delivery router with adapters
        if await self._abort_startup_if_shutdown_requested():
            return True
        self.delivery_router.adapters = self.adapters
        self._wire_teams_pipeline_runtime()

        self._running = True
        self._install_plugin_message_injector()
        self._update_runtime_status("running")

        # Loop-liveness heartbeat (#66892): an asyncio task so a frozen loop
        # stops refreshing ``state/gateway.heartbeat``. Cancelled with the
        # other background tasks during stop(). Best-effort — a liveness probe
        # must never be able to abort startup.
        try:
            _existing_hb = getattr(self, "_loop_heartbeat_task", None)
            if _existing_hb is None or _existing_hb.done():
                self._loop_heartbeat_task = asyncio.create_task(
                    loop_heartbeat_forever(
                        interval_s=DEFAULT_HEARTBEAT_INTERVAL_S,
                        start_time=getattr(self, "_gateway_started_at", 0.0),
                    )
                )
                _bg = getattr(self, "_background_tasks", None)
                if _bg is not None:
                    _bg.add(self._loop_heartbeat_task)
                    self._loop_heartbeat_task.add_done_callback(_bg.discard)
        except Exception:
            logger.debug("Failed to start gateway loop heartbeat", exc_info=True)

        # Emit gateway:startup hook
        hook_count = len(self.hooks.loaded_hooks)
        if hook_count:
            logger.info("%s hook(s) loaded", hook_count)
        await self.hooks.emit("gateway:startup", {
            "platforms": [p.value for p in self.adapters.keys()],
        })
        
        if connected_count > 0:
            logger.info("Gateway running with %s platform(s)", connected_count)
        
        # Build initial channel directory for send_message name resolution
        try:
            from gateway.channel_directory import build_channel_directory
            directory = await build_channel_directory(self.adapters)
            ch_count = sum(len(chs) for chs in directory.get("platforms", {}).values())
            logger.info("Channel directory built: %d target(s)", ch_count)
        except Exception as e:
            logger.warning("Channel directory build failed: %s", e)
        
        # Check if we're restarting after a /update command. If the update is
        # still running, keep watching so we notify once it actually finishes.
        notified = await self._send_update_notification()
        if not notified and any(
            path.exists()
            for path in (
                _hermes_home / ".update_pending.json",
                _hermes_home / ".update_pending.claimed.json",
            )
        ):
            self._schedule_update_notification_watch()

        # Give freshly connected platform adapters a brief moment to settle
        # before sending restart/startup lifecycle messages. In practice this
        # helps Discord thread deliveries right after reconnect.
        if connected_count > 0:
            await asyncio.sleep(1.0)

        # Notify the chat that initiated /restart that the gateway is back.
        chat_restart_notification_pending = _restart_notification_pending()
        planned_restart_notification_pending = _planned_restart_notification_pending()
        # Capture, before _send_restart_notification() unlinks the marker,
        # whether this process booted from a chat-originated /restart. Used as
        # a one-shot signal by the /restart redelivery guard so a missing
        # dedup marker only suppresses a /restart when we KNOW we just came out
        # of a restart cycle (see _is_stale_restart_redelivery).
        if chat_restart_notification_pending:
            self._booted_from_restart = True
        await self._send_restart_notification()

        # Broadcast a lightweight "gateway is back" message to configured home
        # channels only for non-chat planned restarts (terminal/SIGUSR1/service
        # paths). Chat-originated /restart already has a precise reply target
        # in .restart_notify.json, so keep that lifecycle in the originating
        # chat/topic instead of also leaking it to the configured home channel.
        if planned_restart_notification_pending:
            try:
                await self._send_home_channel_startup_notifications(
                    skip_targets=None,
                )
            finally:
                _clear_planned_restart_notification()

        # Automatically continue fresh sessions that were interrupted by the
        # previous gateway restart/shutdown.  The resume_pending flag is cleared
        # by the normal successful-turn path, so a failed auto-resume remains
        # visible for manual recovery on the next user message.
        #
        # Delivery-obligation redelivery runs FIRST: a session whose final
        # response was generated but never confirmed-delivered has its answer
        # in the ledger — redelivering it (and clearing resume_pending for
        # that session) is strictly cheaper and more correct than re-running
        # the whole turn.
        await self._redeliver_pending_obligations()
        self._schedule_resume_pending_sessions()
        await self._finish_startup_restore()

        # Drain any recovered process watchers (from crash recovery checkpoint)
        try:
            from tools.process_registry import process_registry
            # Detach the current batch atomically: reassigning to a fresh list
            # takes ownership of exactly the watchers present now, so any watcher
            # appended concurrently during the yield below isn't silently dropped
            # by a clear() on the shared list.
            watchers = process_registry.pending_watchers
            process_registry.pending_watchers = []
            # Process in batches of 100 with event-loop yield points to avoid
            # O(n^2) event-loop blocking when recovering thousands of watchers.
            for i, watcher in enumerate(watchers):
                self._spawn_supervised(
                    lambda w=watcher: self._run_process_watcher(w),
                    f"process_watcher:{watcher.get('session_id')}",
                    restart=False,
                )
                logger.info("Resumed watcher for recovered process %s", watcher.get("session_id"))
                if i % 100 == 99:
                    await asyncio.sleep(0)
        except Exception as e:
            logger.error("Recovered watcher setup error: %s", e)

        # Start background session expiry watcher to finalize expired sessions
        self._spawn_supervised(self._session_expiry_watcher, "session_expiry_watcher")

        # Stall watchdog: pending inbound + stale agent activity → warn user
        # to /new (does not kill the turn; see agent.session_stall_timeout).
        self._spawn_supervised(self._session_stall_watcher, "session_stall_watcher")

        # Start background kanban notifier — each gateway delivers events for
        # subscriptions owned by the profiles whose adapters it hosts, even
        # when another gateway owns the single dispatcher.
        self._spawn_supervised(self._kanban_notifier_watcher, "kanban_notifier_watcher")

        # Start background kanban dispatcher — spawns workers for ready
        # tasks. Gated by `kanban.dispatch_in_gateway` (default True).
        # When false, users run `hermes kanban daemon` externally or
        # simply don't use kanban; this loop becomes a no-op.
        self._spawn_supervised(self._kanban_dispatcher_watcher, "kanban_dispatcher_watcher")

        # Start background reconnection watcher for platforms that failed at startup
        if self._failed_platforms:
            logger.info(
                "Starting reconnection watcher for %d failed platform(s): %s",
                len(self._failed_platforms),
                ", ".join(p.value for p in self._failed_platforms),
            )
        # Track the reconnect watcher task so _ensure_reconnect_watcher_running
        # can detect if it dies and respawn it (#70344). Spawned via
        # _spawn_supervised (not a bare asyncio.create_task) so an exception
        # escaping the watcher's OUTER while-loop -- not just the per-platform
        # inner try/except -- is caught, logged, and auto-restarted with
        # backoff instead of silently killing the watcher forever. Without
        # this, a platform already queued in _failed_platforms when the
        # watcher dies stays stranded indefinitely: _ensure_reconnect_watcher_running()
        # only gets called from a NEW fatal-error arrival, so if no other
        # platform ever fails afterward, nothing ever notices the watcher is
        # dead (#71758 -- reported as 17.5h of silent downtime for a platform
        # whose transient upstream outage had long since recovered).
        # ``on_spawn`` keeps ``_reconnect_watcher_task`` pointed at the CURRENT
        # live task even when _spawn_supervised's own backoff respawns it — so
        # _ensure_reconnect_watcher_running never mistakes a superseded handle
        # for a dead watcher and spawns a duplicate.
        self._reconnect_watcher_task = self._spawn_supervised(
            self._platform_reconnect_watcher,
            "platform_reconnect_watcher",
            on_spawn=lambda t: setattr(self, "_reconnect_watcher_task", t),
        )

        # Start background handoff watcher — picks up CLI sessions marked
        # handoff_state='pending' in state.db and re-binds them to the
        # destination platform's home channel, then forges a synthetic user
        # turn so the agent kicks off the new chat.
        self._spawn_supervised(self._handoff_watcher, "handoff_watcher")

        # Start background async-delegation watcher — drains completion events
        # from delegate_task(background=true) subagents and injects each
        # result back into its originating session as a new turn, covering the
        # idle case where the subagent finishes with no agent turn running.
        self._spawn_supervised(self._async_delegation_watcher, "async_delegation_watcher")

        # Start background /loop wakeup watcher — scans persisted loops
        # (SessionDB loop:* rows) and injects due wakeup prompts into their
        # originating chats while the session is idle.
        self._spawn_supervised(self._loop_wakeup_watcher, "loop_wakeup_watcher")

        # Start the scale-to-zero idle watcher ONLY when this instance is opted
        # in (the NAS "Labs" HERMES_SCALE_TO_ZERO stamp), messaging is
        # relay-only/absent, and a wakeUrl is registered (decisions.md D1/D11/
        # §3.4(1)). A non-opted instance never starts it, so behaviour is exactly
        # as today. When armed, the watcher drives the relay dormant on sustained
        # idle and then suspends the machine itself via the local flaps socket
        # (Fly Proxy autostop is inbound-only and job-blind, so the gateway owns
        # the suspend decision; NAS provisions these machines autostop:"off").
        try:
            if self._scale_to_zero_should_arm():
                logger.info(
                    "scale-to-zero: armed (idle timeout %.0fs) — watching for idle",
                    self._scale_to_zero_idle_timeout_seconds(),
                )
                self._spawn_supervised(self._scale_to_zero_watcher, "scale_to_zero_watcher")
            else:
                # Surface WHY an OPTED-IN instance didn't arm (a non-opted instance
                # not arming is normal — stay silent there). Without this, a failed
                # arm is invisible and "why won't it suspend/wake?" needs a box-dive.
                self._log_scale_to_zero_not_armed_reason()
        except Exception:  # noqa: BLE001 - arming must never block startup
            logger.debug("scale-to-zero: arm check failed at startup", exc_info=True)

        # Start background drain-control watcher — reconciles the gateway's
        # new-turn accept-state with the external ``.drain_request.json`` marker
        # the dashboard begin/cancel-drain endpoint writes (Phase 2). A marker
        # left behind by a prior instantiation (durable-volume restart, NS-570)
        # is ignored via its instantiation epoch; only a current-epoch marker
        # engages drain on the first tick.
        self._spawn_supervised(self._drain_control_watcher, "drain_control_watcher")

        logger.info("Press Ctrl+C to stop")
        
        return True

    _MAX_SUPERVISED_RESTARTS = 5
    # A task that ran at least this long before crashing is treated as having
    # been HEALTHY — its crash is a fresh, isolated failure rather than part of
    # a rapid crash-loop, so the consecutive-restart counter resets to 0. Only
    # crashes that happen within this window of a (re)spawn accumulate toward
    # ``_MAX_SUPERVISED_RESTARTS``. Without this, a long-lived launchd daemon
    # whose watcher crashes a handful of times over days would hit the cap and
    # be permanently abandoned (NS: silent loss of platform-reconnect / kanban /
    # handoff for the rest of the process life).
    _SUPERVISED_HEALTHY_SECS = 300

    def _spawn_supervised(self, coro_factory, name, *, restart=True, _attempt=0, on_spawn=None):
        """Launch a long-lived background task with task-level supervision.

        Complements upstream's per-iteration inner-loop try/except (which only
        guards a single loop-body) by covering what that CANNOT: an exception
        raised in the watcher's OUTER ``while self._running:`` loop or its
        pre-try setup region, plus task-level death generally. A bare
        ``asyncio.create_task`` drops such an exception on the floor — no log,
        no restart, the watcher silently gone. This retains the handle in
        ``self._background_tasks``, logs any crash, and restarts with capped
        exponential backoff up to ``_MAX_SUPERVISED_RESTARTS`` failures in rapid
        succession (each within ``_SUPERVISED_HEALTHY_SECS`` of its restart).
        The counter resets after any run that stayed healthy for at least
        ``_SUPERVISED_HEALTHY_SECS`` — so a long-lived daemon that crashes
        occasionally over days is never permanently abandoned.

        ``on_spawn`` (optional) is invoked with the freshly-created task on
        every spawn, INCLUDING internal backoff respawns. Callers that also
        track the live handle elsewhere (e.g. ``self._reconnect_watcher_task``
        for ``_ensure_reconnect_watcher_running``) MUST pass it — otherwise the
        supervisor's own respawn creates a new task without updating that
        external handle, so ``_ensure_...`` later sees the stale/done handle
        and spawns a SECOND concurrent watcher (double reconnect attempts).
        """
        from gateway.run import logger
        if getattr(self, "_background_tasks", None) is None:
            self._background_tasks = set()

        # Monotonic spawn timestamp captured per spawn: the ``_done`` callback
        # uses it to distinguish a rapid crash-loop from a healthy-run-then-crash.
        _started = time.monotonic()

        # Deliberately do NOT pass name= to create_task — some test doubles mock
        # create_task with a signature that rejects the name kwarg.
        task = asyncio.create_task(coro_factory())
        # Mark this as a PERMANENT supervised watcher, not transient background
        # WORK. The scale-to-zero idle check must ignore these: supervised
        # watchers (session-expiry, kanban, reconnect, the scale-to-zero watcher
        # itself, ...) live for the whole process, so counting them as "live
        # background work" would make the gateway consider itself busy forever
        # and never go dormant/suspend. Transient tasks added to
        # _background_tasks elsewhere (startup-resume events etc.) stay counted.
        task._hermes_supervised_watcher = True  # type: ignore[attr-defined]
        self._background_tasks.add(task)
        if on_spawn is not None:
            # Record the live handle NOW so an external tracker (e.g.
            # _reconnect_watcher_task) always points at the current task, not a
            # dead one left behind by a prior supervised respawn.
            try:
                on_spawn(task)
            except Exception:  # pragma: no cover - defensive; a tracker must never kill the spawn
                logger.debug("on_spawn callback for %s raised", name, exc_info=True)

        def _done(t):
            self._background_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is None:
                # Clean return == deliberate shutdown or a self-disabling watcher
                # (e.g. a gated no-op that returns synchronously). Respawning here
                # would busy-spin such a watcher — so NEVER restart on clean exit.
                return
            logger.error("Supervised task %s died: %r", name, exc, exc_info=exc)
            if restart and self._running:
                ran_for = time.monotonic() - _started
                if ran_for >= self._SUPERVISED_HEALTHY_SECS:
                    # Ran healthily for a while before crashing — this is a
                    # FRESH failure, not part of a rapid crash-loop. Reset the
                    # consecutive counter so a daemon that crashes a handful of
                    # times over days is never permanently abandoned.
                    effective_attempt = 0
                else:
                    effective_attempt = _attempt
                if effective_attempt >= self._MAX_SUPERVISED_RESTARTS:
                    logger.error(
                        "Supervised task %s died %d times in rapid succession "
                        "(each within %ds of restart) — giving up restarts",
                        name,
                        effective_attempt,
                        self._SUPERVISED_HEALTHY_SECS,
                    )
                    return
                backoff = min(60, 2 ** min(effective_attempt, 6))

                async def _respawn():
                    await asyncio.sleep(backoff)
                    if self._running:
                        self._spawn_supervised(
                            coro_factory,
                            name,
                            restart=restart,
                            _attempt=effective_attempt + 1,
                            on_spawn=on_spawn,
                        )

                respawn_task = asyncio.create_task(_respawn())
                self._background_tasks.add(respawn_task)
                respawn_task.add_done_callback(self._background_tasks.discard)

        task.add_done_callback(_done)
        return task

    async def _handoff_watcher(self, interval: float = 2.0) -> None:
        """Background task that processes pending CLI→gateway session handoffs.

        Polls ``state.db`` for sessions in ``handoff_state='pending'`` and,
        for each one:

        1. Atomically claims it (pending → running).
        2. Resolves the destination platform's configured home channel.
        3. Re-binds the gateway's session_key for that home channel to the
           CLI's existing session_id via ``session_store.switch_session`` so
           the full role-aware transcript replays on the next agent turn.
        4. Forges a synthetic ``MessageEvent`` (``internal=True``) with a
           handoff-notice text and dispatches through the normal gateway
           message pipeline so the agent runs and replies on the platform.
        5. Marks the row ``completed`` (or ``failed`` with ``handoff_error``).

        The CLI process is poll-blocked on the row's terminal state and
        prints the result to the user.
        """
        # Initial delay so the gateway is fully connected to its platforms
        # before we try to dispatch handoffs through them.
        from gateway.run import logger
        await asyncio.sleep(5)
        while self._running:
            try:
                if self._session_db is None:
                    await asyncio.sleep(interval)
                    continue
                pending = await self._session_db.list_pending_handoffs()
                for row in pending:
                    session_id = row.get("id")
                    if not session_id:
                        continue
                    if not await self._session_db.claim_handoff(session_id):
                        # Another tick or another gateway already claimed it.
                        continue
                    try:
                        await self._process_handoff(row)
                        await self._session_db.complete_handoff(session_id)
                    except Exception as exc:
                        logger.warning(
                            "Handoff for session %s failed: %s",
                            session_id, exc, exc_info=True,
                        )
                        await self._session_db.fail_handoff(session_id, str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Handoff watcher tick error: %s", exc, exc_info=True)
            await asyncio.sleep(interval)

    async def _process_handoff(self, row: Dict[str, Any]) -> None:
        """Execute one handoff row. Raises on failure (caller marks failed)."""
        from gateway.run import logger
        from gateway.config import Platform
        from gateway.session import SessionSource, build_session_key
        from gateway.platforms.base import MessageEvent

        cli_session_id = row["id"]
        platform_name = (row.get("handoff_platform") or "").strip().lower()
        if not platform_name:
            raise RuntimeError("handoff_platform is empty")

        # Resolve platform enum
        try:
            platform = Platform(platform_name)
        except (ValueError, KeyError):
            raise RuntimeError(f"unknown platform '{platform_name}'")

        # Adapter must be live. A relay-fronted gateway registers ONE adapter
        # under Platform.RELAY that fronts N logical platforms — so a literal
        # adapters.get(discord) misses even though "discord" is deliverable.
        # resolve_delivery_transport is the shared alias-aware resolver (native
        # adapter wins; relay eligible only when its authenticated transport
        # advertises it fronts the logical platform).
        transport = resolve_delivery_transport(platform, self.config, self.adapters)
        if not transport:
            raise RuntimeError(
                f"platform '{platform_name}' is not active in this gateway"
            )
        adapter = transport.adapter

        # Home channel must be configured
        home = self.config.get_home_channel(platform)
        if not home or not home.chat_id:
            raise RuntimeError(
                f"no home channel configured for {platform_name}; "
                f"run /sethome on the desired chat first"
            )

        cli_title = row.get("title") or cli_session_id[:8]

        # Try to create a fresh thread on the destination so the handoff
        # has its own scrollback. Adapter returns None if threading isn't
        # supported (Matrix/WhatsApp/Signal/SMS) or if creation failed
        # (no permission, topics-mode off, parent is a DM, etc.). When
        # None we fall through to using the home channel directly — the
        # synthetic turn still lands; just without thread isolation.
        thread_name = f"Hermes — {cli_title}"
        try:
            new_thread_id = await adapter.create_handoff_thread(
                str(home.chat_id), thread_name,
            )
        except Exception as exc:
            logger.debug(
                "Handoff: create_handoff_thread raised on %s: %s",
                platform_name, exc, exc_info=True,
            )
            new_thread_id = None

        # Use the new thread if the adapter created one; otherwise fall
        # back to whatever thread (if any) the home channel was configured
        # with.
        effective_thread_id = new_thread_id or (
            str(home.thread_id) if home.thread_id else None
        )

        # Determine chat_type/user_id for the destination source.
        #
        # Telegram private-chat DM topics are represented differently from
        # group/forum threads by the inbound adapter. A handoff-created topic
        # in a positive Telegram chat_id must therefore use the same DM-topic
        # source shape as the user's next real message; otherwise the synthetic
        # handoff turn binds a generic `thread` session key while real replies
        # arrive on a `dm` session key.
        home_chat_id = str(home.chat_id)
        is_telegram_private_chat = (
            platform == Platform.TELEGRAM
            and looks_like_telegram_private_chat_id(home_chat_id)
        )

        if new_thread_id and not is_telegram_private_chat:
            dest_chat_type = "thread"
            dest_user_id = "system:handoff"
        else:
            # No thread — assume DM-style for the home channel. For Telegram
            # private-chat topics, use the real user id (same as chat_id) so
            # topic-mode checks and binding persistence see the same identity as
            # subsequent inbound user messages.
            dest_chat_type = "dm"
            dest_user_id = home_chat_id if is_telegram_private_chat else "system:handoff"

        # Discord thread destinations must key on the thread's OWN id, not the
        # parent channel's, because the Discord adapter builds organic in-thread
        # messages with ``chat_id == thread id`` — so ``build_session_key``
        # yields ``…:thread:{thread}:{thread}``. If the handoff keys on the
        # parent channel (``…:thread:{parent}:{thread}``) the next real user
        # reply in the thread resolves to a DIFFERENT session_key and spawns a
        # fresh session instead of continuing the handed-off one.
        #
        # This is Discord-specific: Slack and Telegram adapters key organic
        # thread messages with ``chat_id == parent_channel`` and the thread
        #/topic id only in ``thread_id``, so for those platforms the parent
        # channel is correct (and the deeper chat_type normalization — handoff
        # uses "thread" but Slack organic uses "group" — is a separate issue).
        if platform == Platform.DISCORD and dest_chat_type == "thread" and effective_thread_id:
            dest_chat_id = str(effective_thread_id)
        else:
            dest_chat_id = home_chat_id
        dest_source = SessionSource(
            platform=platform,
            chat_id=dest_chat_id,
            chat_name=home.name,
            chat_type=dest_chat_type,
            user_id=dest_user_id,
            user_name="Handoff",
            thread_id=effective_thread_id,
        )

        # Compute the gateway's session_key for that destination using the
        # same rules its adapters use, so switch_session targets the right
        # entry. For thread destinations build_session_key keys without
        # user_id (thread_sessions_per_user defaults to False) — so the
        # next real user message in the thread shares this same session.
        platform_cfg = self.config.platforms.get(platform)
        extra = platform_cfg.extra if platform_cfg else {}
        session_key = build_session_key(
            dest_source,
            group_sessions_per_user=extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
        )

        # Make sure there's an entry in the session_store for this key. If
        # the home channel has never been used, get_or_create_session
        # creates one; switch_session then re-points it.
        await self.async_session_store.get_or_create_session(dest_source)

        # Re-bind the destination key to the CLI session_id. switch_session
        # ends the prior session in SQLite and reopens the CLI session under
        # the new key. The CLI's transcript becomes the active one for the
        # gateway from this moment on.
        switched = await self.async_session_store.switch_session(session_key, cli_session_id)
        if switched is None:
            raise RuntimeError(
                f"could not switch session key {session_key} → {cli_session_id}"
            )

        # Evict any cached AIAgent for this session_key so the next dispatch
        # rebuilds it against the CLI session_id (mirrors /resume / /branch).
        self._evict_cached_agent(session_key)

        # Cancel any in-flight running-agent state for the destination key
        # so the synthetic turn isn't queued behind a stale running flag.
        self._release_running_agent_state(session_key)

        synthetic_text = (
            f"[Session was just handed off from CLI (\"{cli_title}\") to this "
            f"channel. The full prior conversation history is loaded above. "
            f"Briefly confirm you're working here and summarize what we were "
            f"working on, so the user can continue from this device.]"
        )

        synthetic_event = MessageEvent(
            text=synthetic_text,
            source=dest_source,
            internal=True,
        )

        logger.info(
            "Handoff: dispatching synthetic turn for CLI session %s → %s "
            "(home=%s, thread=%s, session_key=%s)",
            cli_session_id, platform_name, home.chat_id, effective_thread_id,
            session_key,
        )

        # Dispatch through the runner directly. Going through
        # adapter.handle_message would spawn a background task and we'd
        # lose synchronous error visibility; calling _handle_message inline
        # keeps the success/failure path observable for the watcher.
        response_text = await self._handle_message(synthetic_event)
        if not response_text:
            # Streaming may have already delivered the response inline.
            # Either way, agent ran without raising — count as success.
            return

        # Send the agent's reply to the destination. Route to the new
        # thread if we created one; otherwise the configured home channel
        # (which may itself carry a thread_id). Send through the resolved
        # transport (not adapter.send directly) so a relay-fronted logical
        # platform is stamped on the outbound frame (send_for_platform).
        send_metadata: Dict[str, Any] = {}
        if effective_thread_id:
            send_metadata["thread_id"] = effective_thread_id
        try:
            result = await transport.send(
                platform,
                str(home.chat_id),
                response_text,
                send_metadata or None,
            )
        except Exception as exc:
            raise RuntimeError(f"adapter.send failed: {exc}") from exc

        if not getattr(result, "success", True):
            err = getattr(result, "error", "send returned success=False")
            raise RuntimeError(f"adapter.send failed: {err}")

    async def _session_expiry_watcher(self, interval: int = 300):
        """Background task that finalizes expired sessions.

        Runs every ``interval`` seconds (default 5 min).  For each session
        whose reset policy has expired, invokes ``on_session_finalize``
        hooks, cleans up the cached AIAgent's tool resources, evicts the
        cache entry so it can be garbage-collected, and marks the session
        so it won't be finalized again.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL, logger
        await asyncio.sleep(60)  # initial delay — let the gateway fully start
        _finalize_failures: dict[str, int] = {}  # session_id -> consecutive failure count
        _MAX_FINALIZE_RETRIES = 3
        while self._running:
            try:
                await self.async_session_store._ensure_loaded()
                # Collect expired sessions first, then log a single summary.
                _expired_entries = []
                for key, entry in list(self.session_store._entries.items()):
                    if entry.expiry_finalized:
                        continue
                    if not await self.async_session_store._is_session_expired(entry):
                        continue
                    _expired_entries.append((key, entry))

                if _expired_entries:
                    # Extract platform names from session keys for a compact summary.
                    # Keys look like "agent:main:telegram:dm:12345" — platform is field [2].
                    _platforms: dict[str, int] = {}
                    for _k, _e in _expired_entries:
                        _parts = _k.split(":")
                        _plat = _parts[2] if len(_parts) > 2 else "unknown"
                        _platforms[_plat] = _platforms.get(_plat, 0) + 1
                    _plat_summary = ", ".join(
                        f"{p}:{c}" for p, c in sorted(_platforms.items())
                    )
                    logger.info(
                        "Session expiry: %d sessions to finalize (%s)",
                        len(_expired_entries), _plat_summary,
                    )

                for key, entry in _expired_entries:
                    try:
                        try:
                            from hermes_cli.lifecycle import finalize_session
                            _parts = key.split(":")
                            _platform = _parts[2] if len(_parts) > 2 else ""
                            finalize_session(
                                session_id=entry.session_id,
                                platform=_platform,
                                reason="session_expired",
                            )
                        except Exception:
                            pass
                        # Shut down memory provider and close tool resources
                        # on the cached agent.  Idle agents live in
                        # _agent_cache (not _running_agents), so look there.
                        _cached_agent = None
                        _cache_lock = getattr(self, "_agent_cache_lock", None)
                        if _cache_lock is not None:
                            with _cache_lock:
                                _cached = self._agent_cache.get(key)
                                _cached_agent = _cached[0] if isinstance(_cached, tuple) else _cached if _cached else None
                        # Fall back to _running_agents in case the agent is
                        # still mid-turn when the expiry fires.
                        if _cached_agent is None:
                            _exp_state = self._peek_session_state(key)
                            _cached_agent = _exp_state.turn.agent if _exp_state else None
                        if _cached_agent and _cached_agent is not _AGENT_PENDING_SENTINEL:
                            await self._cleanup_agent_resources_off_loop(
                                _cached_agent, context="session expiry"
                            )
                        # Drop the cache entry so the AIAgent (and its LLM
                        # clients, tool schemas, memory provider refs) can
                        # be garbage-collected.  Otherwise the cache grows
                        # unbounded across the gateway's lifetime.
                        self._evict_cached_agent(key)
                        # Permanently finalizing this session — one funnel
                        # call drops every conversation-scoped dict AND the
                        # boundary security state (approvals, update
                        # prompts, slash-confirm) so the dicts don't grow
                        # unbounded across the gateway's lifetime. (Idle
                        # agent-cache eviction must NOT do this: the
                        # session is still alive and a resumed turn rebuilds
                        # its agent from these overrides. Only true session
                        # finalization, /new, and /reset clear them.) See
                        # _CONVERSATION_SCOPED_STATE.
                        self._clear_conversation_scope(
                            key, reason="expiry_finalized"
                        )
                        # Persist the finalized flag to sessions.json AND
                        # state.db (single write-path, #9006) — also drops
                        # the persisted /model override, since finalization
                        # is a conversation boundary.
                        await self.async_session_store.set_expiry_finalized(entry)
                        logger.debug(
                            "Session expiry finalized for %s",
                            entry.session_id,
                        )
                        _finalize_failures.pop(entry.session_id, None)
                    except Exception as e:
                        failures = _finalize_failures.get(entry.session_id, 0) + 1
                        _finalize_failures[entry.session_id] = failures
                        if failures >= _MAX_FINALIZE_RETRIES:
                            logger.warning(
                                "Session finalize gave up after %d attempts for %s: %s. "
                                "Marking as finalized to prevent infinite retry loop.",
                                failures, entry.session_id, e,
                            )
                            await self.async_session_store.set_expiry_finalized(
                                entry, clear_model_override=False
                            )
                            _finalize_failures.pop(entry.session_id, None)
                        else:
                            logger.debug(
                                "Session finalize failed (%d/%d) for %s: %s",
                                failures, _MAX_FINALIZE_RETRIES, entry.session_id, e,
                            )

                if _expired_entries:
                    _done = sum(
                        1 for _, e in _expired_entries if e.expiry_finalized
                    )
                    _failed = len(_expired_entries) - _done
                    if _failed:
                        logger.info(
                            "Session expiry done: %d finalized, %d pending retry",
                            _done, _failed,
                        )
                    else:
                        logger.info(
                            "Session expiry done: %d finalized", _done,
                        )

                # Sweep agents that have been idle beyond the TTL regardless
                # of session reset policy.  This catches sessions with very
                # long / "never" reset windows, whose cached AIAgents would
                # otherwise pin memory for the gateway's entire lifetime.
                try:
                    _idle_evicted = self._sweep_idle_cached_agents()
                    if _idle_evicted:
                        logger.info(
                            "Agent cache idle sweep: evicted %d agent(s)",
                            _idle_evicted,
                        )
                except Exception as _e:
                    logger.debug("Idle agent sweep failed: %s", _e)

                # Neither the LRU cap nor the idle TTL is aware of how much
                # memory a cached transcript costs, so a busy gateway keeps
                # every warm session's tool output resident until RSS hits the
                # cgroup limit (#80764). Shed LRU transcripts once the heap is
                # over budget; they reload from the persisted session on the
                # next turn.
                try:
                    self._sweep_agent_cache_under_pressure()
                except Exception as _e:
                    logger.debug("Agent cache pressure sweep failed: %s", _e)

                # Periodically prune stale SessionStore entries.  The
                # in-memory dict (and sessions.json) would otherwise grow
                # unbounded in gateways serving many rotating chats /
                # threads / users over long time windows.  Pruning is
                # invisible to users — a resumed session just gets a
                # fresh session_id, exactly as if the reset policy fired.
                _last_prune_ts = getattr(self, "_last_session_store_prune_ts", 0.0)
                _prune_interval = 3600.0  # once per hour
                if time.time() - _last_prune_ts > _prune_interval:
                    try:
                        _max_age = int(
                            getattr(self.config, "session_store_max_age_days", 0) or 0
                        )
                        if _max_age > 0:
                            _pruned = await self.async_session_store.prune_old_entries(_max_age)
                            if _pruned:
                                logger.info(
                                    "SessionStore prune: dropped %d stale entries",
                                    _pruned,
                                )
                    except Exception as _e:
                        logger.debug("SessionStore prune failed: %s", _e)
                    self._last_session_store_prune_ts = time.time()
            except Exception as e:
                logger.debug("Session expiry watcher error: %s", e)
            # Sleep in small increments so we can stop quickly
            for _ in range(interval):
                if not self._running:
                    break
                await asyncio.sleep(1)

    def _session_stall_timeout_seconds(self) -> float:
        """Return configured stall timeout (seconds); 0 disables the watchdog."""
        from gateway.run import _float_env
        return _float_env("HERMES_SESSION_STALL_TIMEOUT", 300)

    def _iter_gateway_adapters(self):
        """Yield every live platform adapter (default + multiplex profiles)."""
        seen: set[int] = set()
        for adapter in list(getattr(self, "adapters", {}).values()):
            if adapter is None:
                continue
            aid = id(adapter)
            if aid in seen:
                continue
            seen.add(aid)
            yield adapter
        for amap in list(getattr(self, "_profile_adapters", {}).values()):
            for adapter in list(amap.values()):
                if adapter is None:
                    continue
                aid = id(adapter)
                if aid in seen:
                    continue
                seen.add(aid)
                yield adapter

    def _session_activity_for_stall(self, session_key: str) -> Optional[dict]:
        """Return the shared activity snapshot for stall progress (#72039).

        Single progress source: ``AIAgent.get_activity_summary()`` /
        ``agent.session_activity``. No turn-start or pending-inbound clocks.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL
        agent = (getattr(self, "_running_agents", None) or {}).get(session_key)
        if agent is None or agent is _AGENT_PENDING_SENTINEL:
            return None
        if not hasattr(agent, "get_activity_summary"):
            return None
        try:
            summary = agent.get_activity_summary()
        except Exception:
            return None
        return summary if isinstance(summary, dict) else None

    async def _check_session_stalls(self, timeout_seconds: float) -> int:
        """Scan pending inbound sessions and notify once per stall episode.

        Returns the number of notifications sent this pass (for tests).
        """
        from gateway.run import _STALL_NOTIFY_SEND_TIMEOUT_SECONDS, logger
        from gateway.session_stall import (
            format_session_stall_notification,
            resolve_session_idle_seconds_from_activity,
            should_clear_session_stall_notification,
            should_emit_session_stall_notification,
        )

        notified_map = getattr(self, "_session_stall_notified", None)
        if notified_map is None:
            notified_map = {}
            self._session_stall_notified = notified_map

        sent = 0
        now = time.time()
        candidates: Dict[str, tuple[Any, Any]] = {}

        for adapter in self._iter_gateway_adapters():
            pending_slot = getattr(adapter, "_pending_messages", None) or {}
            for session_key, event in list(pending_slot.items()):
                if session_key and session_key not in candidates and event is not None:
                    candidates[session_key] = (adapter, event)

        for session_key, overflow in list(
            (getattr(self, "_queued_events", None) or {}).items()
        ):
            if not session_key or session_key in candidates or not overflow:
                continue
            event = overflow[0]
            source = getattr(event, "source", None)
            adapter = (
                self._adapter_for_source(source) if source is not None else None
            )
            if adapter is None:
                continue
            candidates[session_key] = (adapter, event)

        for session_key, (adapter, pending_event) in list(candidates.items()):
            has_pending = pending_event is not None
            activity = (
                self._session_activity_for_stall(session_key) if has_pending else None
            )
            idle_seconds = (
                resolve_session_idle_seconds_from_activity(activity, now=now)
                if has_pending
                else None
            )
            already = bool(notified_map.get(session_key))
            if should_clear_session_stall_notification(
                timeout_seconds=timeout_seconds,
                idle_seconds=idle_seconds,
                has_pending_inbound=has_pending,
            ):
                notified_map.pop(session_key, None)
                already = False
            if not should_emit_session_stall_notification(
                timeout_seconds=timeout_seconds,
                idle_seconds=idle_seconds,
                has_pending_inbound=has_pending,
                already_notified=already,
            ):
                continue

            if idle_seconds is None:
                continue
            mins = max(1, int(idle_seconds // 60))
            activity = activity or {}
            logger.warning(
                "Session stall detected: session=%s idle=%.0fs "
                "(timeout=%.0fs, ~%d min); pending inbound present "
                "| last_activity=%s | provenance=%s "
                "(agent.session_stall_timeout)",
                session_key,
                idle_seconds,
                timeout_seconds,
                mins,
                activity.get("last_activity_desc")
                or activity.get("last_activity_description")
                or "unknown",
                activity.get("provenance")
                or activity.get("last_activity_provenance")
                or "unknown",
            )
            source = getattr(pending_event, "source", None)
            chat_id = getattr(source, "chat_id", None) if source is not None else None
            if not chat_id:
                logger.warning(
                    "Session stall notify skipped (no chat_id): session=%s",
                    session_key,
                )
                # Cannot deliver; latch to avoid log spam every tick.
                notified_map[session_key] = True
                continue
            # #76354 review S2: re-read pending state + activity timestamp
            # IMMEDIATELY before delivery. The snapshot above ages while
            # earlier candidates in this pass await their sends; an agent
            # that made progress (or drained its queue) in that window must
            # not receive a false stall notice. Abort and leave the latch
            # un-set so the next tick re-evaluates from scratch.
            still_pending = (
                (getattr(adapter, "_pending_messages", None) or {}).get(
                    session_key
                )
                is not None
                or bool(
                    (getattr(self, "_queued_events", None) or {}).get(
                        session_key
                    )
                )
            )
            fresh_idle = resolve_session_idle_seconds_from_activity(
                self._session_activity_for_stall(session_key),
                now=time.time(),
            )
            if not still_pending or (
                fresh_idle is not None and fresh_idle < timeout_seconds
            ):
                logger.info(
                    "Session stall notify aborted (no longer stale): "
                    "session=%s pending=%s fresh_idle=%s",
                    session_key,
                    still_pending,
                    fresh_idle,
                )
                # Re-arm: drop any stale latch so a FUTURE genuine stall
                # episode notifies again.
                notified_map.pop(session_key, None)
                continue
            try:
                metadata = (
                    self._thread_metadata_for_source(source)
                    if source is not None and hasattr(self, "_thread_metadata_for_source")
                    else None
                )
                # Round-2 #2: bound the send. A wedged adapter transport
                # (network hang, dead websocket) must not block the whole
                # watcher pass — sibling candidates in this loop would never
                # be evaluated and the watcher itself would stop ticking.
                try:
                    result = await asyncio.wait_for(
                        adapter.send(
                            str(chat_id),
                            format_session_stall_notification(idle_seconds),
                            metadata=metadata,
                        ),
                        timeout=_STALL_NOTIFY_SEND_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Session stall notify send timed out after %.0fs "
                        "for %s; will retry next tick",
                        _STALL_NOTIFY_SEND_TIMEOUT_SECONDS,
                        session_key,
                    )
                    continue  # do not latch; retry next tick
                # Adapters often return SendResult(success=False) instead of raising.
                if result is not None and getattr(result, "success", True) is False:
                    logger.warning(
                        "Session stall notify failed for %s: %s",
                        session_key,
                        getattr(result, "error", "send returned success=False"),
                    )
                    continue  # do not latch; retry next tick
                sent += 1
                notified_map[session_key] = True
            except Exception as exc:
                logger.warning(
                    "Session stall notify failed for %s: %s",
                    session_key,
                    exc,
                )
                # Do not latch — retry next watcher tick until delivery or episode clear.

        # Drop latches for sessions that no longer appear in any pending map.
        for key in list(notified_map.keys()):
            if key not in candidates:
                notified_map.pop(key, None)

        return sent

    async def _session_stall_watcher(self, interval: float = 30.0):
        """Periodic pending-inbound + stale-activity stall watchdog (#72016).

        Progress comes only from ``get_activity_summary()`` (#72039).
        Pending inbound is a notify policy gate, not a progress clock.
        Notify-only: does not kill the turn (contrast ``gateway_timeout`` /
        ``shutdown_watchdog``).
        """
        # Short initial delay so startup reconnect noise does not false-fire.
        from gateway.run import logger
        await asyncio.sleep(min(30.0, max(1.0, float(interval))))
        while self._running:
            try:
                timeout = self._session_stall_timeout_seconds()
                if timeout > 0:
                    await self._check_session_stalls(timeout)
            except Exception as exc:
                logger.debug("Session stall watcher error: %s", exc)
            # Interruptible sleep
            steps = max(1, int(float(interval)))
            for _ in range(steps):
                if not self._running:
                    break
                await asyncio.sleep(1)

    def _active_profile_name(self) -> str:
        """Return the profile name this gateway represents."""
        try:
            from hermes_cli.profiles import get_active_profile_name
            return get_active_profile_name() or "default"
        except Exception:
            return "default"

    # ── Kanban board watchers ───────────────────────────────────────────
    # The kanban notifier/dispatcher watcher loops + their helpers live in
    # GatewayKanbanWatchersMixin (gateway/kanban_watchers.py). They use only
    # self state, so inheriting the mixin keeps every self._kanban_* call site
    # working unchanged while lifting ~1,000 LOC out of this file.

    def _ensure_reconnect_watcher_running(self) -> None:
        """Ensure the platform reconnect watcher background task is alive.

        If the tracked reconnect watcher task has died (e.g. from exhausting
        its restart budget, or a terminal exception that _spawn_supervised
        could not recover), respawns it so platforms queued for reconnection
        are not permanently stranded. Called after queueing a retryable fatal
        error in _handle_adapter_fatal_error (#70344).
        """
        from gateway.run import logger
        if not getattr(self, "_running", False):
            return
        task = getattr(self, "_reconnect_watcher_task", None)
        if task is not None and not task.done():
            return  # already alive
        logger.warning(
            "Reconnect watcher task is dead (done=%s) — respawning",
            task.done() if task is not None else "N/A",
        )
        self._reconnect_watcher_task = self._spawn_supervised(
            self._platform_reconnect_watcher,
            "platform_reconnect_watcher",
            on_spawn=lambda t: setattr(self, "_reconnect_watcher_task", t),
        )

    async def _platform_reconnect_watcher(self) -> None:
        """Background task that periodically retries connecting failed platforms.

        Uses exponential backoff: 30s → 60s → 120s → 240s → 300s (cap).
        Retryable failures (network/DNS blips) keep retrying at the backoff
        cap indefinitely — they self-heal once connectivity returns, so a
        transient outage never requires manual intervention. Non-retryable
        failures (bad auth, etc.) drop out of the queue immediately. The
        circuit breaker (``_pause_failed_platform`` / ``/platform pause``)
        remains available for manual operator control via ``/platform list``
        and ``/platform resume <name>``, but is no longer triggered
        automatically — auto-pausing a recovered platform was the cause of
        bots silently staying dead after a transient DNS failure.
        """
        from gateway.run import _dispose_unused_adapter, _platform_has_bot_credential, _reconnect_backoff, _reconnect_needs_attention, logger
        await asyncio.sleep(10)  # initial delay — let startup finish
        while self._running:
            if not self._failed_platforms:
                # Nothing to reconnect — sleep and check again
                for _ in range(30):
                    if not self._running:
                        return
                    if self._failed_platforms:
                        break
                    await asyncio.sleep(1)
                continue

            now = time.monotonic()
            for platform in list(self._failed_platforms.keys()):
                if not self._running:
                    return
                info = self._failed_platforms.get(platform)
                if info is None:
                    # Removed concurrently (e.g. a manual /platform resume,
                    # or a reconnect that succeeded via a different path)
                    # between the snapshot above and this lookup. Not an
                    # error -- just nothing to do for it this pass.
                    continue
                # Skip paused platforms entirely — they need explicit
                # /platform resume to come back.
                if info.get("paused"):
                    continue
                # Long-lived retry-loop escalation (OOF-156): once a platform
                # has been continuously queued past the attention threshold,
                # flag it NEEDS_ATTENTION in runtime status so owners and
                # fleet monitoring see "this is not a blip" — a dead token,
                # revoked intent, or crash-looping sidecar otherwise presents
                # as ordinary "retrying" forever. Retries continue unchanged:
                # this is a signal, NOT a circuit breaker (auto-pause was
                # deliberately removed — see this docstring's history).
                if not info.get("attention_flagged") and _reconnect_needs_attention(info, now):
                    info["attention_flagged"] = True
                    queued_for = now - info.get("queued_at", now)
                    retrying_since_iso = (
                        datetime.now(timezone.utc) - timedelta(seconds=queued_for)
                    ).isoformat()
                    logger.warning(
                        "%s has been failing/reconnecting continuously for "
                        "%.1f hours (%d attempts) — flagging NEEDS_ATTENTION. "
                        "Retries continue, but this usually means a permanent "
                        "problem (revoked credentials, missing intents, broken "
                        "sidecar). Check `hermes status` / `/platform list`.",
                        platform.value,
                        queued_for / 3600.0,
                        info.get("attempts", 0),
                    )
                    self._update_platform_runtime_status(
                        platform.value,
                        platform_state="retrying",
                        needs_attention=True,
                        retrying_since=retrying_since_iso,
                    )
                if now < info["next_retry"]:
                    continue  # not time yet

                platform_config = info["config"]
                attempt = info["attempts"] + 1
                # Empty-token primary configs can never reconnect; drop them so
                # multiplex setups where a secondary profile owns the bot do
                # not spin forever (#64674).
                if not _platform_has_bot_credential(platform, platform_config):
                    logger.warning(
                        "Reconnect %s: no bot credential on queued config, "
                        "removing from retry queue",
                        platform.value,
                    )
                    del self._failed_platforms[platform]
                    continue
                logger.info(
                    "Reconnecting %s (attempt %d)...",
                    platform.value, attempt,
                )

                adapter = None
                try:
                    adapter = self._create_adapter(platform, platform_config)
                    if not adapter:
                        logger.warning(
                            "Reconnect %s: adapter creation returned None, removing from retry queue",
                            platform.value,
                        )
                        del self._failed_platforms[platform]
                        continue

                    adapter.set_message_handler(self._primary_message_handler())
                    adapter.set_fatal_error_handler(self._handle_adapter_fatal_error)
                    adapter.set_session_store(self.session_store)
                    adapter.set_busy_session_handler(self._handle_active_session_busy_message)
                    _set_reaction = getattr(adapter, "set_reaction_handler", None)
                    if callable(_set_reaction):
                        _set_reaction(self._handle_reaction_event)
                    adapter.set_topic_recovery_fn(self._recover_telegram_topic_thread_id)
                    adapter.set_authorization_check(self._make_adapter_auth_check(adapter.platform))
                    adapter.set_platform_event_handler(self._primary_platform_event_handler())
                    adapter._busy_text_mode = self._busy_text_mode

                    # Reconnect after an outage: preserve the platform's
                    # server-side update queue so messages sent while the bot
                    # was offline are delivered rather than dropped (#46621).
                    success = await self._connect_adapter_with_timeout(
                        adapter, platform, is_reconnect=True
                    )
                    if success:
                        self.adapters[platform] = adapter
                        self._sync_voice_mode_state_to_adapter(adapter)
                        # Wire voice input callback on reconnect as well (#60623).
                        if hasattr(adapter, "_voice_input_callback"):
                            adapter._voice_input_callback = self._handle_voice_channel_input
                        self.delivery_router.adapters = self.adapters
                        del self._failed_platforms[platform]
                        self._update_platform_runtime_status(
                            platform.value,
                            platform_state="connected",
                            error_code=None,
                            error_message=None,
                            needs_attention=False,
                            retrying_since=None,
                        )
                        logger.info("✓ %s reconnected successfully", platform.value)

                        # Rebuild channel directory with the new adapter
                        try:
                            from gateway.channel_directory import build_channel_directory
                            await build_channel_directory(self.adapters)
                        except Exception:
                            pass

                        # A platform that was offline at gateway startup never
                        # got its restart-interrupted sessions auto-resumed —
                        # the startup pass skips sessions whose adapter isn't
                        # connected yet. Now that it's back, retry the
                        # auto-resume scoped to this platform so recovery
                        # doesn't silently wait for a manual user message.
                        try:
                            self._schedule_resume_pending_sessions(platform=platform)
                        except Exception:
                            logger.debug(
                                "resume-pending reschedule after %s reconnect failed",
                                platform.value,
                                exc_info=True,
                            )
                    # Check if the failure is non-retryable
                    elif adapter.has_fatal_error and not adapter.fatal_error_retryable:
                        self._update_platform_runtime_status(
                            platform.value,
                            platform_state="fatal",
                            error_code=adapter.fatal_error_code,
                            error_message=adapter.fatal_error_message,
                        )
                        logger.warning(
                            "Reconnect %s: non-retryable error (%s), removing from retry queue",
                            platform.value, adapter.fatal_error_message,
                        )
                        # The adapter is about to be dropped from the queue
                        # without ever being installed on self.adapters, so
                        # nothing else will call disconnect() on it. We must
                        # dispose it here, otherwise the resource owners it
                        # constructed in __init__ (ResponseStore for
                        # APIServerAdapter, etc.) leak 2 fds each. The
                        # gateway hits the 2560-fd limit after ~12h of
                        # failed reconnects at the 300s backoff cap (#37011).
                        await _dispose_unused_adapter(adapter)
                        del self._failed_platforms[platform]
                    else:
                        self._update_platform_runtime_status(
                            platform.value,
                            platform_state="retrying",
                            error_code=adapter.fatal_error_code,
                            error_message=adapter.fatal_error_message or "failed to reconnect",
                        )
                        backoff = _reconnect_backoff(attempt)
                        info["attempts"] = attempt
                        info["next_retry"] = time.monotonic() + backoff
                        logger.info(
                            "Reconnect %s failed, next retry in %ds",
                            platform.value, backoff,
                        )
                        # Same fd-leak concern as the non-retryable branch
                        # above: the adapter failed to connect and is being
                        # thrown away. Without an explicit dispose call, the
                        # resources it opened in __init__ stay open until
                        # the next GC pass — and aiohttp/SQLite handles
                        # don't get GC'd promptly, so 2 fds/retry leak at
                        # 300s backoff cap = ~12 fds/hour (#37011).
                        await _dispose_unused_adapter(adapter)
                        # Retryable failures (network/DNS blips) keep retrying
                        # at the backoff cap indefinitely — they self-heal once
                        # connectivity returns. We do NOT auto-pause them: a
                        # transient outage must never require manual `/platform
                        # resume` to recover. Non-retryable failures (bad auth,
                        # etc.) already drop out of the queue via the
                        # `not fatal_error_retryable` branch above, so anything
                        # reaching here is by definition retryable.
                except Exception as e:
                    if adapter is not None:
                        # An exception escaping the connect call path
                        # (DNS timeout, aiohttp server.start() crash, etc.)
                        # leaves the adapter in the same unowned state as
                        # the two branches above. Dispose so __init__
                        # resources don't accumulate while the watcher
                        # keeps retrying.
                        await _dispose_unused_adapter(adapter)
                    self._update_platform_runtime_status(
                        platform.value,
                        platform_state="retrying",
                        error_code=None,
                        error_message=str(e),
                    )
                    backoff = _reconnect_backoff(attempt)
                    info["attempts"] = attempt
                    info["next_retry"] = time.monotonic() + backoff
                    logger.warning(
                        "Reconnect %s error: %s, next retry in %ds",
                        platform.value, e, backoff,
                    )
                    # A raised exception during reconnect (connect timeout, DNS
                    # resolution failure, etc.) is inherently transient — keep
                    # retrying at the backoff cap rather than auto-pausing.

            # Check every 10 seconds for platforms that need reconnection
            for _ in range(10):
                if not self._running:
                    return
                await asyncio.sleep(1)

    async def _cancel_secondary_profile_reconnect_tasks(self) -> None:
        """Cancel profile-scoped reconnects before tearing down their registry.

        A reconnect can be waiting in adapter setup while shutdown begins. It
        must not republish an adapter after the secondary registry is drained.
        Waiting is bounded by the same adapter-cleanup budget; if a task does
        not finish in time, the stopped runner state still prevents it from
        installing an adapter when it eventually resumes.
        """
        from gateway.run import logger
        pending = self._profile_failed_platforms
        if not isinstance(pending, dict):
            return
        current = asyncio.current_task()
        tasks: list[asyncio.Task] = []
        for profile_pending in pending.values():
            if not isinstance(profile_pending, dict):
                continue
            for task in profile_pending.values():
                if isinstance(task, asyncio.Task) and task is not current and not task.done():
                    tasks.append(task)
        for task in tasks:
            task.cancel()
        timeout = self._adapter_disconnect_timeout_secs()
        if tasks and timeout > 0:
            _done, unfinished = await asyncio.wait(tasks, timeout=timeout)
            if unfinished:
                logger.warning(
                    "Timed out waiting for %d secondary profile reconnect task(s) during shutdown",
                    len(unfinished),
                )
        pending.clear()

    def _start_systemd_watchdog(self) -> bool:
        """Start sd_notify only after a configured gateway is truly running."""
        if not self._running or self.config.systemd_watchdog_seconds <= 0:
            return False
        if self._systemd_watchdog is not None:
            return True

        from gateway.systemd_notify import SystemdWatchdog

        watchdog = SystemdWatchdog(config_enabled=True)
        if not watchdog.start():
            return False
        self._systemd_watchdog = watchdog
        watchdog.ready("Hermes Gateway running")
        return True

    async def _stop_systemd_watchdog(self) -> None:
        """Stop heartbeats before any potentially long shutdown drain."""
        watchdog = self._systemd_watchdog
        if watchdog is None:
            return
        self._systemd_watchdog = None
        await watchdog.stop()

    async def stop(
        self,
        *,
        restart: bool = False,
        detached_restart: bool = False,
        service_restart: bool = False,
    ) -> None:
        """Stop the gateway and disconnect all adapters."""
        # getattr-guard: shutdown-path tests build bare runners via
        # object.__new__ that lack the liveness-guard machinery.
        from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL, _INTERRUPT_REASON_GATEWAY_RESTART, _INTERRUPT_REASON_GATEWAY_SHUTDOWN, _hermes_home, _planned_restart_notification_path, _shutdown_gateway_health_export, atomic_json_write, logger
        _stop_guards = getattr(self, "_stop_loop_liveness_guards", None)
        if callable(_stop_guards):
            _stop_guards()
        if restart:
            self._restart_requested = True
            self._restart_detached = detached_restart
            self._restart_via_service = service_restart
        if self._stop_task is not None:
            await self._stop_task
            return

        async def _stop_impl() -> None:
            def _kill_tool_subprocesses(phase: str) -> None:
                """Kill tool subprocesses + tear down terminal envs + browsers.

                Called twice in the shutdown path: once eagerly after a
                drain timeout forces agent interrupt (so we reclaim bash/
                sleep children before systemd TimeoutStopSec escalates to
                SIGKILL on the cgroup — #8202), and once as a final
                catch-all at the end of _stop_impl() for the graceful
                path or anything respawned mid-teardown.

                All steps are best-effort; exceptions are swallowed so
                one subsystem's failure doesn't block the rest.
                """
                try:
                    from tools.process_registry import process_registry
                    _killed = process_registry.kill_all()
                    if _killed:
                        logger.info(
                            "Shutdown (%s): killed %d tool subprocess(es)",
                            phase, _killed,
                        )
                except Exception as _e:
                    logger.debug("process_registry.kill_all (%s) error: %s", phase, _e)
                try:
                    # Any cron job still dispatched at this instant just had
                    # its tool subprocess killed above (kill_all() has no
                    # per-job-ID targeting — it's a global sweep). Its agent
                    # thread is still alive in this process and may go on to
                    # produce a plausible-looking final response from the
                    # now-truncated tool output; mark the run interrupted so
                    # the scheduler can never report that as success (#60432).
                    # No-op when no cron job is in flight.
                    from cron.scheduler import mark_running_jobs_interrupted
                    _interrupted = mark_running_jobs_interrupted(
                        f"Gateway shutdown ({phase}) killed the job's tool "
                        "subprocess before the run finished."
                    )
                    if _interrupted:
                        logger.warning(
                            "Shutdown (%s): marked %d in-flight cron job(s) interrupted: %s",
                            phase, len(_interrupted), ", ".join(_interrupted),
                        )
                except Exception as _e:
                    logger.debug("mark_running_jobs_interrupted (%s) error: %s", phase, _e)
                try:
                    from tools.async_delegation import interrupt_all as _interrupt_async
                    _async_n = _interrupt_async(reason=f"gateway shutdown ({phase})")
                    if _async_n:
                        logger.info(
                            "Shutdown (%s): interrupted %d background delegation(s)",
                            phase, _async_n,
                        )
                except Exception as _e:
                    logger.debug("async interrupt_all (%s) error: %s", phase, _e)
                try:
                    from tools.terminal_tool import cleanup_all_environments
                    cleanup_all_environments()
                except Exception as _e:
                    logger.debug("cleanup_all_environments (%s) error: %s", phase, _e)
                try:
                    from tools.browser_tool import cleanup_all_browsers
                    cleanup_all_browsers()
                except Exception as _e:
                    logger.debug("cleanup_all_browsers (%s) error: %s", phase, _e)

            # Thread-based shutdown watchdog (#66892): asyncio timeouts cannot
            # recover a frozen loop. Arm a plain OS thread at the start of
            # stop(); if teardown never finishes within drain+grace it dumps
            # faulthandler stacks and os._exit so KeepAlive/systemd can revive.
            # Skip under pytest so stop()-driving unit tests don't get a
            # delayed hard-exit in the worker.
            _watchdog_done = threading.Event()
            self._shutdown_watchdog_done = _watchdog_done
            _stop_started_at_box: dict[str, float] = {}

            def _shutdown_watchdog_snapshot() -> dict:
                started = _stop_started_at_box.get("t")
                return {
                    "restart_requested": bool(self._restart_requested),
                    "draining": bool(self._draining),
                    "running": bool(self._running),
                    "active_agents": self._running_agent_count(),
                    "active_cron_jobs": self._active_cron_job_count(),
                    "active_api_runs": self._active_api_run_count(),
                    "restart_drain_timeout": self._restart_drain_timeout,
                    "watchdog_delay_s": resolve_shutdown_watchdog_delay(
                        self._restart_drain_timeout
                    ),
                    "phase_elapsed_s": (
                        time.monotonic() - started if started is not None else None
                    ),
                }

            if not os.environ.get("PYTEST_CURRENT_TEST"):
                arm_shutdown_watchdog(
                    resolve_shutdown_watchdog_delay(self._restart_drain_timeout),
                    done_event=_watchdog_done,
                    snapshot_fn=_shutdown_watchdog_snapshot,
                    exit_code=1,
                )

            try:
                await _stop_impl_body(
                    _kill_tool_subprocesses,
                    _stop_started_at_box,
                )
            finally:
                _watchdog_done.set()

        async def _stop_impl_body(_kill_tool_subprocesses, _stop_started_at_box) -> None:
            logger.info(
                "Stopping gateway%s...",
                " for restart" if self._restart_requested else "",
            )
            _stop_started_at = time.monotonic()
            _stop_started_at_box["t"] = _stop_started_at

            def _phase_elapsed() -> float:
                return time.monotonic() - _stop_started_at

            self._running = False
            self._clear_plugin_message_injector()
            self._draining = True

            stop_watchdog = getattr(self, "_stop_systemd_watchdog", None)
            if callable(stop_watchdog):
                await stop_watchdog()

            await self._cancel_secondary_profile_reconnect_tasks()

            # Notify all chats with active agents BEFORE draining.
            # Adapters are still connected here, so messages can be sent.
            await self._notify_active_sessions_of_shutdown()
            logger.info(
                "Shutdown phase: notify_active_sessions done at +%.2fs",
                _phase_elapsed(),
            )

            timeout = self._restart_drain_timeout

            # Pre-mark sessions as resume_pending BEFORE the drain wait.
            # If the process is killed by the service manager during the
            # drain, the durable marker is already written so the next
            # gateway boot can recover in-flight sessions (#27856).
            _pre_drain_keys: list[str] = []
            for _sk, _agent in list(self._running_agents.items()):
                if _agent is _AGENT_PENDING_SENTINEL:
                    continue
                try:
                    await self.async_session_store.mark_resume_pending(
                        _sk,
                        "restart_timeout" if self._restart_requested else "shutdown_timeout",
                    )
                    _pre_drain_keys.append(_sk)
                except Exception as _e:
                    logger.debug("pre-drain mark_resume_pending failed for %s: %s", _sk, _e)

            _cron_at_start = self._active_cron_job_count()
            _api_at_start = self._active_api_run_count()
            _drain_started_at = time.monotonic()
            active_agents, timed_out = await self._drain_active_agents(timeout)
            logger.info(
                "Shutdown phase: drain done at +%.2fs (drain took %.2fs, "
                "timed_out=%s, active_at_start=%d, active_now=%d, "
                "cron_at_start=%d, cron_now=%d, "
                "api_at_start=%d, api_now=%d)",
                _phase_elapsed(),
                time.monotonic() - _drain_started_at,
                timed_out,
                len(active_agents),
                self._running_agent_count(),
                _cron_at_start,
                self._active_cron_job_count(),
                _api_at_start,
                self._active_api_run_count(),
            )

            if not timed_out:
                # Drain completed gracefully — all running sessions finished.
                # Clear the pre-drain resume_pending markers so sessions that
                # completed during the drain window don't carry a stale flag.
                for _sk in _pre_drain_keys:
                    if _sk not in self._running_agents:
                        try:
                            await self.async_session_store.clear_resume_pending(_sk)
                        except Exception as _e:
                            logger.debug(
                                "clear_resume_pending after drain failed for %s: %s",
                                _sk, _e,
                            )

            if timed_out:
                logger.warning(
                    "Gateway drain timed out after %.1fs with %d active agent(s), "
                    "%d in-flight cron job(s), and %d api_server run(s); "
                    "interrupting remaining work.",
                    timeout,
                    self._running_agent_count(),
                    self._active_cron_job_count(),
                    self._active_api_run_count(),
                )
                # Mark forcibly-interrupted sessions as resume_pending BEFORE
                # interrupting the agents.  This preserves each session's
                # session_id + transcript so the next message on the same
                # session_key auto-resumes from the existing conversation
                # instead of getting routed through suspend_recently_active()
                # and converted into a fresh session.  Terminal escalation
                # for genuinely stuck sessions still flows through the
                # existing ``.restart_failure_counts`` stuck-loop counter
                # (incremented below, threshold 3), which sets
                # ``suspended=True`` and overrides resume_pending.
                #
                # Iterate self._running_agents (current) rather than the
                # drain-start ``active_agents`` snapshot — the snapshot
                # may include sessions that finished gracefully during
                # the drain window, and marking those falsely would give
                # them a stray restart-interruption system note on their
                # next turn even though their previous turn completed
                # cleanly.  Skip pending sentinels for the same reason
                # _interrupt_running_agents() does: their agent hasn't
                # started yet, there's nothing to interrupt, and the
                # session shouldn't carry a misleading resume flag.
                _resume_reason = (
                    "restart_timeout" if self._restart_requested else "shutdown_timeout"
                )
                for _sk, _agent in list(self._running_agents.items()):
                    if _agent is _AGENT_PENDING_SENTINEL:
                        continue
                    try:
                        await self.async_session_store.mark_resume_pending(_sk, _resume_reason)
                    except Exception as _e:
                        logger.debug(
                            "mark_resume_pending failed for %s: %s",
                            _sk, _e,
                        )
                self._interrupt_running_agents(
                    _INTERRUPT_REASON_GATEWAY_RESTART if self._restart_requested else _INTERRUPT_REASON_GATEWAY_SHUTDOWN
                )
                interrupt_deadline = asyncio.get_running_loop().time() + 5.0
                # Wait on API-server work too. The interrupt is cooperative:
                # without this the settle window closes the instant
                # _running_agents is empty, and an API turn that was just asked
                # to stop gets its tool subprocesses killed below before it can
                # unwind — the exact amputation this interrupt exists to avoid.
                while (
                    self._running_agents or self._active_api_run_count()
                ) and asyncio.get_running_loop().time() < interrupt_deadline:
                    self._update_runtime_status("draining")
                    await asyncio.sleep(0.1)

                # The interrupt above fires exactly once, but work can
                # materialize AFTER that one shot: a /v1/runs task admitted
                # before the drain populates _active_run_agents only once
                # _create_agent returns, and a _running_agents entry claimed
                # as _AGENT_PENDING_SENTINEL is promoted to a real agent by
                # track_agent() on its own schedule. Either way the settle
                # loop waited on work nothing signaled. If any is still live
                # at settle-loop exit, re-signal so a late-materializing
                # agent gets a cooperative interrupt instead of going
                # straight to the tool-subprocess kill.
                if self._running_agents or self._active_api_run_count():
                    self._interrupt_running_agents(
                        _INTERRUPT_REASON_GATEWAY_RESTART
                        if self._restart_requested
                        else _INTERRUPT_REASON_GATEWAY_SHUTDOWN
                    )
                    logger.debug(
                        "Re-signaled interrupt for work still live at settle-window exit"
                    )

                # Kill lingering tool subprocesses NOW, before we spend more
                # budget on adapter disconnect / session DB close.  Under
                # systemd (TimeoutStopSec bounded by drain_timeout+headroom),
                # deferring this to the end of stop() risks systemd escalating
                # to SIGKILL on the cgroup first — at which point bash/sleep
                # children left behind by an interrupted terminal tool get
                # killed by systemd instead of us (issue #8202).  The final
                # catch-all cleanup below still runs for the graceful path.
                _kill_tool_subprocesses("post-interrupt")
                logger.info(
                    "Shutdown phase: post-interrupt tool kill done at +%.2fs",
                    _phase_elapsed(),
                )

            if self._restart_requested and self._restart_detached:
                try:
                    await self._launch_detached_restart_command()
                except Exception as e:
                    logger.error("Failed to launch detached gateway restart: %s", e)

            await self._finalize_shutdown_agents(active_agents)

            # Also shut down memory providers on idle cached agents.
            # _finalize_shutdown_agents only handles agents that were
            # mid-turn at drain time; the _agent_cache may still hold
            # idle agents whose MemoryProviders never received
            # on_session_end().
            _cache_lock = getattr(self, "_agent_cache_lock", None)
            _cache = getattr(self, "_agent_cache", None)
            if _cache_lock is not None and _cache is not None:
                with _cache_lock:
                    _idle_agents = list(_cache.values())
                    _cache.clear()
                for _entry in _idle_agents:
                    _agent = (
                        _entry[0] if isinstance(_entry, tuple) else _entry
                    )
                    # Bounded + off-loop so a wedged memory provider on one
                    # idle agent can't hang shutdown indefinitely — that path
                    # is why SIGTERM failed to kill the process (#53175).
                    await self._cleanup_agent_resources_off_loop(
                        _agent, context="shutdown idle-cache"
                    )

            # Completion flush tasks can be sleeping in their fan-in window or
            # blocked in adapter delivery.  Cancel and await them while adapters
            # are still alive so every watcher receives a retryable result
            # before platform teardown begins.
            cancel_completion_batches = getattr(
                self, "_cancel_process_completion_batch_tasks", None
            )
            if cancel_completion_batches is not None:
                await cancel_completion_batches()

            for platform, adapter in list(self.adapters.items()):
                await self._bounded_adapter_teardown(adapter, platform)

            # Disconnect secondary-profile adapters (multiplex mode).
            for _prof, _amap in list(getattr(self, "_profile_adapters", {}).items()):
                for platform, adapter in list(_amap.items()):
                    await self._bounded_adapter_teardown(
                        adapter, platform, profile=_prof
                    )
                _amap.clear()
            if hasattr(self, "_profile_adapters"):
                self._profile_adapters.clear()
            logger.info(
                "Shutdown phase: all adapters disconnected at +%.2fs",
                _phase_elapsed(),
            )

            for _task in list(self._background_tasks):
                if _task is self._stop_task:
                    continue
                if _task is self._restart_task:
                    # The restart orchestration task is awaiting _stop_task
                    # right now; cancelling it would propagate CancelledError
                    # into this _stop_impl and skip _shutdown_event.set() /
                    # _exit_code = 75 (#12875).  It self-terminates anyway.
                    continue
                _task.cancel()
            self._background_tasks.clear()

            self.adapters.clear()
            for _session_key in list(self._running_agents):
                self._release_running_agent_state(_session_key)
            # Flush pending messages to disk before clearing (#72680).
            # When FTS5 corruption prevents message persistence, the
            # in-memory pending text is the only surviving copy.  Clearing
            # without flushing causes permanent data loss.
            try:
                from gateway.shutdown_flush import flush_pending_to_file
                flush_pending_to_file(dict(self._pending_messages), reason="shutdown")
            except Exception:
                pass
            # On the real runner these are live SessionState views whose
            # clear() resets one field per session — never a wholesale dict
            # swap, so a concurrent writer on another session can't lose its
            # entry.  Test fakes borrowing _stop_impl keep plain dicts.
            self._running_agents.clear()
            self._running_agents_ts.clear()
            if hasattr(self, "_active_session_leases"):
                self._active_session_leases.clear()
            self._pending_messages.clear()
            self._pending_approvals.clear()
            if hasattr(self, '_busy_ack_ts'):
                self._busy_ack_ts.clear()
            self._shutdown_event.set()

            # Global cleanup: kill any remaining tool subprocesses not tied
            # to a specific agent (catch-all for zombie prevention). On the
            # drain-timeout path we already did this earlier after agent
            # interrupt — this second call catches (a) the graceful path
            # where drain succeeded without interrupt, and (b) anything
            # that got respawned between the earlier call and adapter
            # disconnect (defense in depth; safe to call repeatedly).
            _kill_tool_subprocesses("final-cleanup")
            logger.info(
                "Shutdown phase: final-cleanup tool kill done at +%.2fs",
                _phase_elapsed(),
            )

            # Reap the process-global auxiliary-client cache once at the very
            # end of teardown.  Per-turn cleanup runs in _cleanup_agent_resources
            # for each active agent, but clients bound to worker-thread loops
            # that died with their ThreadPoolExecutor (notably cron ticks) only
            # get swept here.  Without this, long-running gateways accumulate
            # async httpx transports until they hit EMFILE on macOS's default
            # RLIMIT_NOFILE=256.  See #14210.
            try:
                from agent.auxiliary_client import shutdown_cached_clients
                shutdown_cached_clients()
            except Exception as _e:
                logger.debug("shutdown_cached_clients error: %s", _e)

            # Close SQLite session DBs so the WAL write lock is released.
            # Without this, --replace and similar restart flows leave the
            # old gateway's connection holding the WAL lock until Python
            # actually exits — causing 'database is locked' errors when
            # the new gateway tries to open the same file.
            # ``self`` holds the DB at ``_session_db`` (an AsyncSessionDB facade);
            # unwrap to the sync handle. ``session_store`` holds it at ``_db``.
            _self_db = getattr(self, "_session_db", None)
            _self_db = getattr(_self_db, "_db", _self_db)
            for _db in (_self_db, getattr(getattr(self, "session_store", None), "_db", None)):
                if _db is None or not hasattr(_db, "close"):
                    continue
                try:
                    _db.close()
                except Exception as _e:
                    logger.debug("SessionDB close error: %s", _e)
            GatewayRunner._shutdown_executor(self)
            logger.info(
                "Shutdown phase: SessionDB close done at +%.2fs",
                _phase_elapsed(),
            )

            from gateway.status import remove_pid_file, release_gateway_runtime_lock
            remove_pid_file()
            release_gateway_runtime_lock()

            # Write a clean-shutdown marker so the next startup knows this
            # wasn't a crash.  suspend_recently_active() only needs to run
            # after unexpected exits.  However, if the drain timed out and
            # agents were force-interrupted, their sessions may be in an
            # incomplete state (trailing tool response, no final assistant
            # message).  Skip the marker in that case so the next startup
            # suspends those sessions — giving users a clean slate instead
            # of resuming a half-finished tool loop.
            if not timed_out:
                try:
                    (_hermes_home / ".clean_shutdown").touch()
                except Exception:
                    pass
            else:
                logger.info(
                    "Skipping .clean_shutdown marker — drain timed out with "
                    "interrupted agents; next startup will suspend recently "
                    "active sessions."
                )

            # Track sessions that were active at shutdown for stuck-loop
            # detection (#7536).  On each restart, the counter increments
            # for sessions that were running.  If a session hits the
            # threshold (3 consecutive restarts while active), the next
            # startup auto-suspends it — breaking the loop.
            if active_agents:
                self._increment_restart_failure_counts(set(active_agents.keys()))

            if self._restart_requested and self._restart_command_source is None:
                try:
                    atomic_json_write(
                        _planned_restart_notification_path(),
                        {
                            "requested_at": time.time(),
                            "via_service": bool(self._restart_via_service),
                            "detached": bool(self._restart_detached),
                        },
                        indent=None,
                    )
                except Exception as e:
                    logger.debug("Failed to write planned restart notification marker: %s", e)

            if self._restart_requested and self._restart_via_service:
                self._launch_systemd_restart_shortcut()
                # Always exit with TEMPFAIL (75) on service-managed
                # restarts.  The shortcut helper above is best-effort and
                # commonly fails on real deployments: non-root gateway
                # units hit Polkit denials when invoking ``systemd-run
                # --system``, headless boxes have no user bus for
                # ``--user``, and operator-managed unit files may use
                # ``Restart=on-failure`` rather than ``Restart=always``.
                # Exit 75 paired with ``RestartForceExitStatus=75`` makes
                # systemd treat the planned restart as a controlled
                # failure and revive the unit via ``Restart=on-failure``,
                # regardless of whether the helper survived.  Without
                # this, a clean exit (0) on Linux left the gateway dead
                # until someone rebooted the host.  Only the planned code
                # (75) is whitelisted via ``RestartForceExitStatus``; a
                # genuine crash exits non-zero-but-not-75, so real crash
                # loops are still governed by the unit's normal
                # ``Restart=``/``RestartSec`` (and any StartLimit the
                # operator sets) rather than force-restarted here.
                self._exit_code = GATEWAY_SERVICE_RESTART_EXIT_CODE
                self._exit_reason = self._exit_reason or "Gateway restart requested"

            self._draining = False
            # Persist the terminal gateway_state. The default is "stopped",
            # but when this teardown was triggered by an UNEXPECTED external
            # signal (container/s6 SIGTERM on `docker restart` or image
            # upgrade, OOM-killer, bare `kill`) we instead persist "running"
            # to preserve the operator's run-intent across the restart.
            #
            # On Docker (s6-overlay), container_boot.py reads gateway_state
            # on the next boot and only auto-starts gateways whose last
            # state was "running" (_AUTOSTART_STATES). Persisting "stopped"
            # — or leaving the mid-shutdown "draining" marker in place — for
            # a routine `docker compose up --force-recreate` permanently
            # suppresses auto-start, so the messaging channels silently stay
            # dark until the operator manually restarts (issue #42675).
            #
            # An operator-initiated stop (`hermes gateway stop`,
            # systemd/launchd ExecStop, the s6 stop path, Ctrl+C) writes a
            # planned-stop marker BEFORE signalling, so it is classified as
            # a planned stop (not signal-initiated) and correctly persists
            # "stopped" — respecting the explicit intent. A restart also
            # persists "stopped" here; the restarting process brings the
            # gateway back up itself.
            if getattr(self, "_signal_initiated_shutdown", False) and not self._restart_requested:
                logger.info(
                    "Gateway stopped by an unexpected signal — persisting "
                    "gateway_state=running so container_boot auto-starts on "
                    "the next boot (issue #42675)"
                )
                self._update_runtime_status("running", self._exit_reason)
            else:
                self._update_runtime_status("stopped", self._exit_reason)
            _shutdown_gateway_health_export(self)
            logger.info("Gateway stopped (total teardown %.2fs)", _phase_elapsed())

        self._stop_task = asyncio.create_task(_stop_impl())
        await self._stop_task

    async def wait_for_shutdown(self) -> None:
        """Wait for shutdown signal."""
        await self._shutdown_event.wait()

    async def _start_secondary_profile_adapters(self) -> int:
        """Bring up adapters for every non-active profile this gateway serves.

        Returns the number of secondary adapters that connected. No-op (returns
        0) unless ``gateway.multiplex_profiles`` is on.

        Each profile's adapters are created and connected under that profile's
        HERMES_HOME + secret scope (``_profile_runtime_scope``), stored in
        ``self._profile_adapters[profile]``, and given a message handler that
        stamps ``source.profile`` before delegating to the shared
        ``_handle_message`` — so the agent turn resolves that profile's config,
        skills, and credentials. Same-platform credential collisions (two
        profiles polling the same bot token) are detected and refused here, the
        only point that sees every profile's resolved credentials together.
        """
        from gateway.run import MultiplexConfigError, SecondaryPortBindingConfigError, _multiplex_profile_homes, logger
        if not getattr(self.config, "multiplex_profiles", False):
            return 0

        try:
            from hermes_cli.profiles import get_active_profile_name
        except Exception:
            return 0

        active = get_active_profile_name() or "default"
        connected = 0
        # Resource claim -> profile that owns it. Credential claims prevent two
        # profiles polling the same account; listener claims prevent sidecars
        # with distinct credentials from binding the same endpoint.
        claimed: Dict[tuple, str] = {}
        for _plat, _ad in self.adapters.items():
            fp = self._adapter_credential_fingerprint(_ad)
            if fp is not None:
                claimed[(_plat, fp)] = active
            listener_claim = self._adapter_listener_claim(_plat, _ad)
            if listener_claim is not None:
                claimed[listener_claim] = active
        # A retryable primary still owns its configured credential and listener.
        # Reserve both while it is queued so a secondary cannot take the endpoint
        # before the reconnect watcher retries the primary adapter.
        for retry_info in getattr(self, "_failed_platforms", {}).values():
            for claim_name in ("credential_claim", "listener_claim"):
                retry_claim = retry_info.get(claim_name)
                if isinstance(retry_claim, tuple):
                    claimed[retry_claim] = active

        profile_homes = _multiplex_profile_homes(self.config)
        for profile_name, profile_home in profile_homes:
            if profile_name == active:
                continue  # handled by the primary startup loop
            try:
                connected += await self._start_one_profile_adapters(
                    profile_name, profile_home, claimed
                )
            except SecondaryPortBindingConfigError as e:
                logger.warning(
                    "Skipping secondary profile '%s' due to port-binding config error: %s",
                    profile_name,
                    e,
                )
            except MultiplexConfigError:
                raise
            except Exception as e:
                logger.error(
                    "Failed to start adapters for profile '%s': %s",
                    profile_name, e, exc_info=True,
                )

        # Record the authoritative served set in runtime status for `hermes status`.
        # "Served" means eligible for shared routing, HTTP prefixes, cron, and
        # profile runtime scope; it is intentionally broader than profiles with a
        # successfully connected secondary adapter (or any adapter configured).
        try:
            from gateway.status import write_runtime_status
            from gateway.pairing import PairingStore
            served = [active] + sorted(
                name for name, _home in profile_homes if name != active
            )
            # Per-profile PairingStores so authz_mixin can route pairing
            # checks to the right whitelist. The active profile gets a store
            # at its HERMES_HOME; additional served profiles resolve from
            # their own profile homes. See gateway.pairing.PairingStore.
            for name in served:
                if name and name not in self.pairing_stores:
                    self.pairing_stores[name] = (
                        self.pairing_store
                        if name == active
                        else PairingStore(profile=name)
                    )
            write_runtime_status(served_profiles=served)
        except Exception:
            logger.debug("could not record served_profiles", exc_info=True)

        return connected

    async def _start_one_profile_adapters(
        self, profile_name: str, profile_home: "Path", claimed: Dict[tuple, str]
    ) -> int:
        """Create+connect one profile's adapters under its runtime scope."""
        from gateway.run import MultiplexConfigError, SecondaryPortBindingConfigError, _load_gateway_runtime_config, _own_policy_open_startup_violation, _profile_runtime_scope, logger
        from gateway.config import load_gateway_config

        with _profile_runtime_scope(profile_home):
            profile_runtime_cfg = _load_gateway_runtime_config()
            from hermes_cli.plugins import discover_plugins

            discover_plugins()
            profile_cfg = load_gateway_config()
            violation = _own_policy_open_startup_violation(profile_cfg)
        self._snapshot_profile_busy_modes(profile_name, profile_runtime_cfg)
        if violation:
            raise MultiplexConfigError(
                f"Profile '{profile_name}' enables {violation}. "
                "Enable GATEWAY_ALLOW_ALL_USERS or the platform allow-all flag "
                "for that profile, or change dm_policy/group_policy away from "
                "'open'."
            )

        port_binding_platforms = sorted(
            platform.value
            for platform, platform_config in profile_cfg.platforms.items()
            if platform_config.enabled
            and _platform_binds_port(platform.value, platform_config.extra)
        )
        if port_binding_platforms:
            joined = ", ".join(port_binding_platforms)
            raise SecondaryPortBindingConfigError(
                f"Profile '{profile_name}' enables port-binding platform(s) "
                f"{joined}, but gateway.multiplex_profiles is on. The default "
                f"profile owns the single shared HTTP listener and serves every "
                f"profile through the /p/{profile_name}/ URL prefix. Remove "
                f"these platform entries from profile '{profile_name}'s config.yaml "
                f"or configure them only on the default profile."
            )

        profile_map = self._profile_adapters.setdefault(profile_name, {})
        connected = 0
        for platform, platform_config in profile_cfg.platforms.items():
            if not platform_config.enabled:
                continue
            # Relay is shared process-level ingress in multiplex mode. The
            # active profile owns the one connection; connector-stamped
            # source.profile routes inbound turns to secondary profiles.
            if (
                getattr(self.config, "multiplex_profiles", False)
                and platform is Platform.RELAY
            ):
                continue
            try:
                with _profile_runtime_scope(profile_home):
                    adapter = self._create_adapter(platform, platform_config)
            except Exception as e:
                logger.error(
                    "[MULTIPLEX] Profile '%s': _create_adapter('%s') raised %s",
                    profile_name,
                    platform.value,
                    e,
                    exc_info=True,
                )
                continue
            if not adapter:
                logger.warning(
                    "[MULTIPLEX] Profile '%s': skipping platform '%s' - adapter creation returned None",
                    profile_name,
                    platform.value,
                )
                continue

            # Same-token conflict detection — refuse a duplicate poll.
            credential_claim = self._adapter_credential_claim(platform, adapter)
            if credential_claim is not None:
                owner = claimed.get(credential_claim)
                if owner is not None:
                    logger.error(
                        "Profile '%s' and '%s' both configure %s with the same "
                        "credential — refusing to start the duplicate (one "
                        "credential cannot be consumed twice). Give each profile "
                        "its own %s credential.",
                        owner, profile_name, platform.value, platform.value,
                    )
                    # This adapter has not connected and therefore owns no
                    # resources to clean up. Calling disconnect here can mutate
                    # the shared platform state and, for a same-credential Photon
                    # adapter, shut down the primary profile's live sidecar.
                    continue

            listener_claim = self._adapter_listener_claim(platform, adapter)
            if listener_claim is not None:
                owner = claimed.get(listener_claim)
                if owner is not None:
                    bind, port = listener_claim[-2:]
                    logger.error(
                        "Profile '%s' and '%s' both configure %s sidecars on "
                        "%s:%s — refusing to start the duplicate listener. "
                        "Set platforms.%s.extra.sidecar_port to a distinct port "
                        "for profile '%s'.",
                        owner,
                        profile_name,
                        platform.value,
                        bind,
                        port,
                        platform.value,
                        profile_name,
                    )
                    # Like credential conflicts, this adapter never connected
                    # and owns no resources that should be disconnected.
                    continue

            self._configure_profile_adapter(adapter, profile_name, platform)

            try:
                with _profile_runtime_scope(profile_home):
                    success = await self._connect_initial_adapter_with_timeout(
                        adapter, platform
                    )
                if success:
                    profile_map[platform] = adapter
                    if credential_claim is not None:
                        claimed[credential_claim] = profile_name
                    if listener_claim is not None:
                        claimed[listener_claim] = profile_name
                    connected += 1
                    logger.info("✓ %s connected (profile: %s)", platform.value, profile_name)
                else:
                    logger.warning("✗ %s failed to connect (profile: %s)", platform.value, profile_name)
                    await self._safe_adapter_disconnect(adapter, platform)
            except Exception as e:
                logger.error("✗ %s error (profile: %s): %s", platform.value, profile_name, e)
                await self._safe_adapter_disconnect(adapter, platform)
        return connected

    def _configure_profile_adapter(
        self,
        adapter: BasePlatformAdapter,
        profile_name: str,
        platform: Platform,
    ) -> None:
        """Install the profile-scoped handlers shared by startup and reconnect."""
        adapter.set_message_handler(self._make_profile_message_handler(profile_name))
        adapter.set_fatal_error_handler(
            self._make_profile_fatal_error_handler(profile_name, platform)
        )
        adapter.set_session_store(self.session_store)
        adapter.set_busy_session_handler(
            self._make_profile_busy_session_handler(profile_name)
        )
        _set_reaction = getattr(adapter, "set_reaction_handler", None)
        if callable(_set_reaction):
            _set_reaction(self._handle_reaction_event)
        adapter.set_topic_recovery_fn(self._recover_telegram_topic_thread_id)
        adapter.set_authorization_check(
            self._make_adapter_auth_check(platform, profile_name=profile_name)
        )
        adapter.set_platform_event_handler(
            self._make_profile_platform_event_handler(profile_name)
        )
        text_modes = getattr(self, "_busy_text_modes_by_profile", None)
        adapter._busy_text_mode = (
            text_modes.get(profile_name, self._busy_text_mode)
            if isinstance(text_modes, dict)
            else self._busy_text_mode
        )

    async def _run_secondary_profile_reconnect(
        self, profile_name: str, platform: Platform
    ) -> None:
        """Reconnect a retryable secondary adapter under its own profile scope."""
        from gateway.run import _profile_runtime_scope, _reconnect_backoff, logger
        attempts = 0
        current_task = asyncio.current_task()
        try:
            while self._running:
                adapter = None
                try:
                    from hermes_cli.profiles import get_profile_dir
                    from gateway.config import load_gateway_config

                    profile_home = get_profile_dir(profile_name)
                    with _profile_runtime_scope(profile_home):
                        profile_config = load_gateway_config().platforms.get(platform)
                        if profile_config is None or not profile_config.enabled:
                            return
                        adapter = self._create_adapter(platform, profile_config)
                        if adapter is None:
                            logger.warning(
                                "Secondary %s reconnect skipped: adapter unavailable (profile: %s)",
                                platform.value,
                                profile_name,
                            )
                            return
                        self._configure_profile_adapter(
                            adapter, profile_name, platform
                        )
                        success = await self._connect_adapter_with_timeout(
                            adapter, platform, is_reconnect=True
                        )

                    if success and self._running:
                        profile_map = self._profile_adapters.setdefault(profile_name, {})
                        if platform not in profile_map:
                            profile_map[platform] = adapter
                            self._sync_voice_mode_state_to_adapter(adapter)
                            logger.info(
                                "✓ %s reconnected (profile: %s)",
                                platform.value,
                                profile_name,
                            )
                            return
                        # A newer reconnect already won the slot while this
                        # attempt was awaiting connect; do not replace it.
                        await self._safe_adapter_disconnect(adapter, platform)
                        return

                    # Shutdown can begin while connect() is in flight. Do not
                    # republish a newly connected adapter after the registry has
                    # been drained; release its partial resources instead.
                    if success:
                        await self._safe_adapter_disconnect(adapter, platform)
                        return

                    await self._safe_adapter_disconnect(adapter, platform)
                    if (
                        getattr(adapter, "has_fatal_error", False)
                        and not getattr(adapter, "fatal_error_retryable", True)
                    ):
                        return
                except asyncio.CancelledError:
                    if adapter is not None:
                        await self._safe_adapter_disconnect(adapter, platform)
                    raise
                except Exception:
                    if adapter is not None:
                        await self._safe_adapter_disconnect(adapter, platform)
                    logger.debug(
                        "Secondary %s reconnect attempt failed (profile: %s)",
                        platform.value,
                        profile_name,
                        exc_info=True,
                    )

                if not self._running:
                    return
                attempts += 1
                backoff = _reconnect_backoff(attempts)
                logger.info(
                    "Secondary %s reconnect retry in %ds (profile: %s)",
                    platform.value,
                    backoff,
                    profile_name,
                )
                await asyncio.sleep(backoff)
        finally:
            pending = self._profile_failed_platforms
            if isinstance(pending, dict):
                profile_pending = pending.get(profile_name)
                task = profile_pending.get(platform) if isinstance(profile_pending, dict) else None
                if not isinstance(task, asyncio.Task) or task is current_task:
                    if isinstance(profile_pending, dict):
                        profile_pending.pop(platform, None)
                        if not profile_pending:
                            pending.pop(profile_name, None)

    def _schedule_secondary_profile_reconnect(
        self, profile_name: str, platform: Platform, adapter: BasePlatformAdapter
    ) -> None:
        """Schedule one runner-owned reconnect without sharing primary secrets."""
        if not self._running or not adapter.fatal_error_retryable:
            return
        pending = self._profile_failed_platforms
        if not isinstance(pending, dict):
            pending = {}
            self._profile_failed_platforms = pending
        profile_pending = pending.setdefault(profile_name, {})
        if platform in profile_pending:
            return
        task = asyncio.create_task(
            self._run_secondary_profile_reconnect(profile_name, platform),
            name=f"secondary-reconnect:{profile_name}:{platform.value}",
        )
        profile_pending[platform] = task
        background_tasks = getattr(self, "_background_tasks", None)
        if not isinstance(background_tasks, set):
            background_tasks = set()
            self._background_tasks = background_tasks
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    def _make_profile_fatal_error_handler(
        self, profile_name: str, platform: Platform
    ) -> Callable[[BasePlatformAdapter], Awaitable[None]]:
        """Route a secondary-profile fatal error to that profile's reconnect slot."""
        async def _handler(adapter: BasePlatformAdapter) -> None:
            await self._handle_profile_adapter_fatal_error(profile_name, platform, adapter)

        return _handler

    async def _handle_profile_adapter_fatal_error(
        self,
        profile_name: str,
        platform: Platform,
        adapter: BasePlatformAdapter,
    ) -> None:
        """Remove a failed multiplexed adapter without touching the primary slot.

        Secondary adapters are owned by ``_profile_adapters`` rather than
        ``self.adapters``. The primary-only fatal handler intentionally ignores
        them; without this route, a fatal secondary Discord client stayed live
        forever after its liveness sampler stopped.
        """
        from gateway.run import logger
        profile_map = getattr(self, "_profile_adapters", {}).get(profile_name)
        if not isinstance(profile_map, dict) or profile_map.get(platform) is not adapter:
            logger.debug(
                "Ignoring stale fatal error from secondary %s adapter (profile: %s)",
                platform.value,
                profile_name,
            )
            return
        profile_map.pop(platform, None)
        await self._safe_adapter_disconnect(adapter, platform)
        if not self._running:
            return
        self._schedule_secondary_profile_reconnect(profile_name, platform, adapter)
        logger.error(
            "Fatal %s adapter error for multiplexed profile %s (%s)",
            platform.value,
            profile_name,
            adapter.fatal_error_code or "unknown",
        )
        # Reconnect is scoped to the profile's own config and secret mapping;
        # never rebuild a secondary adapter with the default profile's credentials.

    def _make_profile_message_handler(self, profile_name: str):
        """Return a message handler that stamps source.profile then delegates.

        Auth runs inside ``_handle_message`` *before* the agent-turn scope is
        installed. For secondary profiles under multiplex, wrap the whole
        handler in ``_profile_runtime_scope`` so allowlists/tokens from that
        profile's ``.env`` are visible to ``get_secret`` / authz.
        """
        from gateway.run import _profile_runtime_scope
        from hermes_cli.profiles import get_profile_dir

        try:
            profile_home = get_profile_dir(profile_name)
        except Exception:
            profile_home = None

        async def _handler(event):
            try:
                if getattr(event, "source", None) is not None and not event.source.profile:
                    event.source.profile = profile_name
            except Exception:
                pass
            if profile_home is not None:
                with _profile_runtime_scope(profile_home):
                    return await self._handle_message(event)
            return await self._handle_message(event)

        return _handler

    def _make_profile_busy_session_handler(self, profile_name: str):
        """Stamp an owning adapter's profile before resolving busy policy."""
        async def _handler(event, _session_key):
            try:
                if getattr(event, "source", None) is not None and not event.source.profile:
                    event.source.profile = profile_name
            except Exception:
                pass
            routed_session_key = self._session_key_for_source(event.source)
            return await self._handle_active_session_busy_message(
                event, routed_session_key
            )

        return _handler

    def _make_default_profile_message_handler(self):
        """Scope a multiplexed default-profile message from ingress onward."""
        from gateway.run import _profile_runtime_scope, get_hermes_home
        profile_home = Path(get_hermes_home())

        async def _handler(event):
            with _profile_runtime_scope(profile_home):
                return await self._handle_message(event)

        return _handler

    def _primary_message_handler(self):
        """Return the correctly scoped handler for a primary adapter."""
        if getattr(self.config, "multiplex_profiles", False):
            return self._make_default_profile_message_handler()
        return self._handle_message

    async def _handle_gateway_platform_event(self, event: dict, source) -> None:
        """Authorize and publish one normalized adapter event to plugin hooks."""
        from gateway.run import logger
        try:
            from hermes_cli.lifecycle import has_hook, invoke_hook

            if not has_hook("gateway_platform_event"):
                return
            if not self._is_user_authorized(source):
                return
            invoke_hook("gateway_platform_event", **event)
        except Exception:
            # Observer failures must never break the adapter's update loop.
            logger.debug("gateway_platform_event hook dispatch failed", exc_info=True)

    def _make_profile_platform_event_handler(self, profile_name: str):
        """Bind platform-event auth and hook dispatch to one multiplex profile."""
        from gateway.run import _profile_runtime_scope
        from hermes_cli.profiles import get_profile_dir

        try:
            profile_home = get_profile_dir(profile_name)
        except Exception:
            profile_home = None

        async def _handler(event, source):
            if getattr(source, "profile", None) is None:
                source.profile = profile_name
            if profile_home is not None:
                with _profile_runtime_scope(profile_home):
                    return await self._handle_gateway_platform_event(event, source)
            return await self._handle_gateway_platform_event(event, source)

        return _handler

    def _make_default_profile_platform_event_handler(self):
        """Scope primary-transport events to their routed multiplex profile."""

        from gateway.run import _profile_runtime_scope
        async def _handler(event, source):
            with _profile_runtime_scope(self._resolve_profile_home_for_source(source)):
                return await self._handle_gateway_platform_event(event, source)

        return _handler

    def _primary_platform_event_handler(self):
        if getattr(self.config, "multiplex_profiles", False):
            return self._make_default_profile_platform_event_handler()
        return self._handle_gateway_platform_event

    @staticmethod
    def _adapter_credential_claim(
        platform: Platform, adapter: Any
    ) -> Optional[tuple]:
        """Return the exclusive credential resource claimed by an adapter."""
        from gateway.run import GatewayRunner
        fingerprint = GatewayRunner._adapter_credential_fingerprint(adapter)
        if fingerprint is None:
            return None
        return (platform, fingerprint)

    @staticmethod
    def _adapter_listener_claim(platform: Platform, adapter: Any) -> Optional[tuple]:
        """Return the exclusive listener resource claimed by an adapter.

        Photon sidecars are per-profile processes. Even when two profiles use
        different project credentials, their sidecars cannot share a bind and
        port. Represent that endpoint as a claim so multiplex startup rejects
        the later adapter before either ``connect()`` or ``disconnect()`` can
        disturb the first profile.
        """
        if getattr(platform, "value", None) != "photon":
            return None
        bind = getattr(adapter, "_sidecar_bind", None)
        port = getattr(adapter, "_sidecar_port", None)
        if not isinstance(bind, str) or not bind.strip():
            return None
        try:
            port = int(port)
        except (TypeError, ValueError):
            return None
        return ("listener", "photon", bind.strip().lower(), port)

    @staticmethod
    def _adapter_credential_fingerprint(adapter: Any) -> Optional[str]:
        """Return a stable, log-safe fingerprint of an adapter's credential.

        Used only to detect two profiles claiming the same platform credential.
        Returns a salted hash (never the credential itself) of the adapter's
        primary credential, or None when no credential is discoverable (in
        which case we don't attempt conflict detection for it).
        """
        token = None
        for attr in (
            "token",
            "bot_token",
            "_token",
            "api_token",
            "_bot_token",
            # Photon/Spectrum authenticates with project credentials instead
            # of a bot token. Including its secret keeps multiplexed profiles
            # from spawning competing sidecars for the same account and port.
            "_project_secret",
        ):
            val = getattr(adapter, attr, None)
            if isinstance(val, str) and val.strip():
                token = val.strip()
                break
        # Many adapters (e.g. Discord) store the token on their `config`
        # sub-object rather than directly on the adapter. Without this lookup
        # those adapters all return None here, the same-token conflict check
        # is silently skipped, and every profile's adapter for that platform
        # starts polling the same bot token — producing a per-message race
        # for which adapter answers. See test_reads_config_token.
        if not token:
            cfg = getattr(adapter, "config", None)
            if cfg is not None:
                for attr in ("token", "bot_token"):
                    val = getattr(cfg, attr, None)
                    if isinstance(val, str) and val.strip():
                        token = val.strip()
                        break
        if not token:
            config = getattr(adapter, "config", None)
            val = getattr(config, "token", None)
            if isinstance(val, str) and val.strip():
                token = val.strip()
        if not token:
            return None
        import hashlib
        return hashlib.sha256(("hermes-mux:" + token).encode("utf-8")).hexdigest()[:16]

    def _create_adapter(
        self, 
        platform: Platform, 
        config: Any
    ) -> Optional[BasePlatformAdapter]:
        """Create the appropriate adapter for a platform.

        Checks the platform_registry first (plugin adapters), then falls
        through to the built-in if/elif chain for core platforms.
        """
        from gateway.run import logger
        if hasattr(config, "extra") and isinstance(config.extra, dict):
            config.extra.setdefault(
                "group_sessions_per_user",
                self.config.group_sessions_per_user,
            )
            config.extra.setdefault(
                "thread_sessions_per_user",
                getattr(self.config, "thread_sessions_per_user", False),
            )

        # ── Plugin-registered platforms (checked first) ───────────────────
        try:
            from gateway.platform_registry import platform_registry
            if platform_registry.is_registered(platform.value):
                adapter = platform_registry.create_adapter(platform.value, config)
                if adapter is not None:
                    # Inject a back-reference to the gateway runner so every
                    # adapter can (a) deliver cross-platform admin alerts and
                    # (b) resolve inbound profile routing through
                    # ``runner._profile_name_for_source``. Unconditional:
                    # ``BasePlatformAdapter`` declares ``gateway_runner``, so
                    # this reaches ALL platforms (not just the ones that
                    # pre-declared it), making profile routing platform-generic.
                    adapter.gateway_runner = self
                    return adapter
                # Registered but failed to instantiate — don't silently fall
                # through to built-ins (there are none for plugin platforms).
                logger.error(
                    "Platform '%s' is registered but adapter creation failed "
                    "(check dependencies and config)",
                    platform.value,
                )
                return None
        except Exception as e:
            logger.debug("Platform registry lookup for '%s' failed: %s", platform.value, e)
        # Fall through to built-in adapters below

        if platform == Platform.WHATSAPP_CLOUD:
            from gateway.platforms.whatsapp_cloud import (
                WhatsAppCloudAdapter,
                check_whatsapp_cloud_requirements,
            )
            if not check_whatsapp_cloud_requirements():
                logger.warning(
                    "WhatsApp Cloud: aiohttp/httpx missing — reinstall hermes-agent"
                )
                return None
            return WhatsAppCloudAdapter(config)
        
        elif platform == Platform.SIGNAL:
            from gateway.platforms.signal import (
                SignalAdapter,
                check_signal_requirements,
                validate_signal_config,
            )
            if not check_signal_requirements():
                logger.warning("Signal: runtime requirements not met")
                return None
            if not validate_signal_config(config):
                logger.warning("Signal: SIGNAL_HTTP_URL or SIGNAL_ACCOUNT not configured")
                return None
            return SignalAdapter(config)

        elif platform == Platform.WEIXIN:
            from gateway.platforms.weixin import WeixinAdapter, check_weixin_requirements
            if not check_weixin_requirements():
                logger.warning("Weixin: aiohttp/cryptography not installed")
                return None
            return WeixinAdapter(config)

        elif platform == Platform.API_SERVER:
            from gateway.platforms.api_server import APIServerAdapter, check_api_server_requirements
            if not check_api_server_requirements():
                logger.warning("API Server: aiohttp not installed")
                return None
            adapter = APIServerAdapter(config)
            adapter.gateway_runner = self
            return adapter

        elif platform == Platform.WEBHOOK:
            from gateway.platforms.webhook import WebhookAdapter, check_webhook_requirements
            if not check_webhook_requirements():
                logger.warning("Webhook: aiohttp not installed")
                return None
            adapter = WebhookAdapter(config)
            adapter.gateway_runner = self  # For cross-platform delivery
            return adapter

        elif platform == Platform.MSGRAPH_WEBHOOK:
            from gateway.platforms.msgraph_webhook import (
                MSGraphWebhookAdapter,
                check_msgraph_webhook_requirements,
            )
            if not check_msgraph_webhook_requirements():
                logger.warning("MSGraph webhook: aiohttp not installed")
                return None
            return MSGraphWebhookAdapter(config)

        elif platform == Platform.BLUEBUBBLES:
            from gateway.platforms.bluebubbles import BlueBubblesAdapter, check_bluebubbles_requirements
            if not check_bluebubbles_requirements():
                logger.warning("BlueBubbles: aiohttp/httpx missing or BLUEBUBBLES_SERVER_URL/BLUEBUBBLES_PASSWORD not configured")
                return None
            return BlueBubblesAdapter(config)

        elif platform == Platform.QQBOT:
            from gateway.platforms.qqbot import QQAdapter, check_qq_requirements
            if not check_qq_requirements():
                logger.warning("QQBot: aiohttp/httpx missing or QQ_APP_ID/QQ_CLIENT_SECRET not configured")
                return None
            return QQAdapter(config)

        elif platform == Platform.YUANBAO:
            from gateway.platforms.yuanbao import YuanbaoAdapter, WEBSOCKETS_AVAILABLE
            if not WEBSOCKETS_AVAILABLE:
                logger.warning("Yuanbao: websockets not installed. Run: pip install websockets")
                return None
            return YuanbaoAdapter(config)

        return None

    def _make_adapter_auth_check(
        self,
        platform: Platform,
        profile_name: Optional[str] = None,
    ) -> Callable[[str, Optional[str], Optional[str]], bool]:
        """Build a platform-bound auth callback for adapter use.

        Adapters that fetch external context (e.g. Slack
        ``conversations.replies``) call this through
        ``BasePlatformAdapter._is_sender_authorized`` to mark non-allowlisted
        senders as unverified in LLM context, mitigating indirect prompt
        injection from third parties in shared threads/channels.

        The returned callback delegates to :meth:`_is_user_authorized` so the
        full auth chain — platform allowlists, group allowlists, pairing
        store, allow-all flags — stays the single source of truth.

        ``profile_name`` binds the callback to the secondary adapter's own
        multiplex profile, so its ``SessionSource`` resolves that profile's
        secret scope instead of falling back to the active profile.
        """
        def check(
            user_id: str,
            chat_type: Optional[str] = None,
            chat_id: Optional[str] = None,
        ) -> bool:
            if not user_id:
                return False
            source = SessionSource(
                platform=platform,
                chat_id=chat_id or "",
                chat_type=chat_type or "group",
                user_id=user_id,
                profile=profile_name,
            )
            return self._is_user_authorized(source)
        return check






    async def _deliver_platform_notice(self, source, content: str) -> None:
        """Deliver a setup/operational notice using platform-specific privacy rules."""
        from gateway.run import _is_slack_ignored_channel, logger
        adapter = self._adapter_for_source(source)
        if not adapter:
            return

        config = getattr(self, "config", None)
        if (
            config
            and getattr(source, "platform", None) == Platform.SLACK
            and _is_slack_ignored_channel(config, getattr(source, "chat_id", None))
        ):
            logger.info(
                "Skipping Slack platform notice for configured ignored channel %s",
                getattr(source, "chat_id", None),
            )
            return

        notice_delivery = "public"
        if config and hasattr(config, "get_notice_delivery"):
            notice_delivery = config.get_notice_delivery(source.platform)

        metadata = self._thread_metadata_for_source(source)
        if notice_delivery == "private" and getattr(source, "user_id", None):
            try:
                result = await adapter.send_private_notice(
                    source.chat_id,
                    source.user_id,
                    content,
                    metadata=metadata,
                )
                if getattr(result, "success", False):
                    return
            except Exception:
                logger.debug(
                    "[%s] send_private_notice failed, falling back to public",
                    getattr(source, "platform", "?"),
                    exc_info=True,
                )

        await adapter.send(source.chat_id, content, metadata=metadata)

    async def _resolve_async_delegation_session(
        self,
        session_entry: SessionEntry,
        pinned_session_id: str,
    ) -> Optional[SessionEntry]:
        """Resolve an async completion to its verified owning gateway session.

        A compression rotation ends the physical parent row while continuing
        the same logical conversation in a child.  Follow that lineage, but
        never let a late completion override an unrelated /new or restored
        route.  Unknown ownership remains fail-closed; the result is still
        available in the delegation records.
        """
        from gateway.run import _USER_BOUNDARY_END_REASONS, logger
        session_db = cast(Any, self._session_db)
        if session_db is None:
            logger.warning(
                "Async-delegation completion has no session database; "
                "dropping injection (#55578 fail-closed)."
            )
            return None

        pinned_row = None
        try:
            pinned_row = await session_db.get_session(pinned_session_id)
        except Exception:
            logger.debug(
                "Async-delegation parent lookup failed for %s",
                pinned_session_id,
                exc_info=True,
            )

        if pinned_row is None:
            logger.warning(
                "Async-delegation completion has unknown spawning session %s; "
                "dropping injection (#55578 fail-closed).",
                pinned_session_id,
            )
            return None

        target_session_id = pinned_session_id
        follows_compression = False
        if pinned_row.get("ended_at"):
            _end_reason = str(pinned_row.get("end_reason") or "")
            if _end_reason in _USER_BOUNDARY_END_REASONS:
                logger.warning(
                    "Async-delegation completion pinned to user-closed session %s "
                    "(end_reason=%r); dropping injection instead of resurrecting it "
                    "(#55578 fail-closed).",
                    pinned_session_id,
                    _end_reason,
                )
                return None
            if _end_reason != "compression":
                # Idle/timeout/lifecycle end (scale-to-zero norm): the chat
                # route remains valid and ``session_entry`` IS the routing
                # key's current session for this same chat, so deliver the
                # finished work there instead of dropping it. This is the
                # delivery leg _classify_completion_target promises when it
                # returns "deliver" for non-boundary ends — without it the
                # pre-flight verdict and this resolver disagree, and the
                # durable row is acked at adapter acceptance then silently
                # dropped here (falsely-acknowledged permanent loss;
                # staging incident 2026-08-09 defect #2).
                logger.info(
                    "Async-delegation completion pinned to %s-ended session %s; "
                    "retargeting to the chat's current session %s.",
                    _end_reason or "idle",
                    pinned_session_id,
                    session_entry.session_id,
                )
                return session_entry

            follows_compression = True
            try:
                target_session_id = await session_db.get_compression_tip(
                    pinned_session_id
                )
            except Exception:
                logger.debug(
                    "Async-delegation compression-tip lookup failed for %s",
                    pinned_session_id,
                    exc_info=True,
                )
                target_session_id = None

            if not target_session_id or target_session_id == pinned_session_id:
                logger.warning(
                    "Async-delegation completion pinned to compressed session %s "
                    "without a continuation; dropping injection.",
                    pinned_session_id,
                )
                return None

            try:
                tip_row = await session_db.get_session(target_session_id)
            except Exception:
                tip_row = None
            if tip_row is None or tip_row.get("ended_at"):
                logger.warning(
                    "Async-delegation compression continuation %s is %s; "
                    "dropping injection.",
                    target_session_id,
                    "unknown" if tip_row is None else "ended",
                )
                return None

            route_owns_lineage = session_entry.session_id in {
                pinned_session_id,
                target_session_id,
            }
            if not route_owns_lineage:
                # A long-running delegation may survive multiple compression
                # rotations.  Accept an intermediate stale route only when its
                # own verified compression tip is the same live target.
                try:
                    route_row = await session_db.get_session(session_entry.session_id)
                    route_tip = (
                        await session_db.get_compression_tip(session_entry.session_id)
                        if route_row is not None
                        and route_row.get("ended_at")
                        and route_row.get("end_reason") == "compression"
                        else None
                    )
                except Exception:
                    route_tip = None
                route_owns_lineage = route_tip == target_session_id

            if not route_owns_lineage:
                logger.warning(
                    "Async-delegation completion for compression lineage %s -> %s "
                    "does not own current route %s; dropping injection.",
                    pinned_session_id,
                    target_session_id,
                    session_entry.session_id,
                )
                return None

        if target_session_id == session_entry.session_id:
            return session_entry

        prior_session_id = session_entry.session_id
        if follows_compression:
            switched = await self.async_session_store.advance_compression_session(
                session_entry.session_key,
                prior_session_id,
                target_session_id,
            )
        else:
            switched = await self.async_session_store.switch_session(
                session_entry.session_key,
                target_session_id,
            )
        if switched is None:
            logger.warning(
                "Async-delegation completion could not bind routing key %s to "
                "owning session %s; dropping injection.",
                session_entry.session_key,
                target_session_id,
            )
            return None

        logger.info(
            "Pinned async-delegation completion to owning session %s "
            "(was %s) for routing key %s (#57498)",
            target_session_id,
            prior_session_id,
            session_entry.session_key,
        )
        return switched

    # ------------------------------------------------------------------
    # Mid-run (busy-session) slash command dispatch — "Guard 2".
    #
    # Replaces the historical hand-written per-command if-chain: each
    # command's mid-run behavior is declared on its CommandDef
    # (busy_policy / busy_handler in hermes_cli/commands.py) and resolved
    # here through a single handler table. Reply strings are byte-identical
    # to the old chain.
    # ------------------------------------------------------------------

    # Command-specific mid-run reject texts (busy_policy == "reject" with a
    # busy_handler naming an entry here). All other rejected commands get
    # the generic catch-all text in _dispatch_busy_slash_command.
    _BUSY_REJECT_TEXT: Dict[str, str] = {
        "model": "Agent is running — wait or /stop first, then switch models.",
        "codex-runtime": ("Agent is running — wait or /stop first, then "
                          "change runtime."),
        "moa": "Agent is running — wait or /stop first, then run /moa.",
    }

    async def _dispatch_busy_slash_command(
        self, event: MessageEvent, cmd_def, quick_key: str, source,
    ):
        """Dispatch a recognized slash command while an agent is running.

        Resolution order:
          1. ``busy_handler`` — special mid-run variant (e.g. /goal's
             control-verb whitelist, /queue's FIFO enqueue, /model's
             custom reject text).
          2. ``busy_policy == "dispatch"`` — the command's normal handler.
          3. Catch-all busy-reject text. Rejecting is required rather than
             falling through to interrupt + discard: commands like /model,
             /reasoning, /voice, /insights, /title, /resume, /retry,
             /undo, /compress, /usage, /reload-mcp, /sethome, /reset (all
             registered as Discord slash commands) would interrupt the
             agent AND get silently discarded by the slash-command safety
             net, producing a zero-char response. See #5057, #6252, #10370.
        """
        from gateway.run import logger
        name = cmd_def.name
        policy = getattr(cmd_def, "busy_policy", "reject")
        handler_key = getattr(cmd_def, "busy_handler", None)

        if handler_key:
            special = {
                "start": self._busy_start_command,
                "stop": self._busy_stop_command,
                "new": self._busy_new_command,
                "queue": self._busy_queue_command,
                "steer": self._busy_steer_command,
                "egress": self._busy_egress_command,
                "goal": self._busy_goal_command,
                "loop": self._busy_loop_command,
            }.get(handler_key)
            if special is not None:
                return await special(event, quick_key, source)
            reject_text = self._BUSY_REJECT_TEXT.get(handler_key)
            if reject_text is not None:
                return reject_text

        if policy in ("dispatch", "interrupt_then_dispatch"):
            plain = {
                "status": self._handle_status_command,
                "context": self._handle_context_command,
                "restart": self._handle_restart_command,
                "approve": self._handle_approve_command,
                "deny": self._handle_deny_command,
                "pause": self._handle_pause_command,
                "agents": self._handle_agents_command,
                "background": self._handle_background_command,
                "kanban": self._handle_kanban_command,
                "subgoal": self._handle_subgoal_command,
                "heartbeat": self._handle_heartbeat_command,
                "yolo": self._handle_yolo_command,
                "verbose": self._handle_verbose_command,
                "footer": self._handle_footer_command,
                "help": self._handle_help_command,
                "commands": self._handle_commands_command,
                "profile": self._handle_profile_command,
                "update": self._handle_update_command,
                "version": self._handle_version_command,
            }.get(name)
            if plain is not None:
                return await plain(event)
            logger.warning(
                "busy_policy=%s for /%s has no mid-run handler — "
                "falling back to busy-reject", policy, name,
            )

        # Catch-all: any other recognized slash command reached the
        # running-agent guard. Reject gracefully rather than falling
        # through to interrupt + discard.
        return (
            f"⏳ Agent is running — `/{name}` can't run "
            f"mid-turn. Wait for the current response or `/stop` first."
        )

    async def _handle_pause_command(self, event: MessageEvent):
        """`/pause [reason]` engages the global emergency stop; `/pause off`
        (aliases: resume/stop) lifts it.

        This is the in-band resume path for messaging-only operators — the
        estop gate above deliberately lets recognized slash commands through
        while paused so a user without host-shell access is never locked out.
        """
        from agent import estop

        args = (event.get_command_args() or "").strip()
        if args.lower() in {"off", "resume", "stop", "disengage"}:
            if estop.disengage():
                return "▶️ Resumed — new work is accepted again."
            return "Hermes wasn't paused."
        state = estop.get_state()
        if state is not None and not args:
            reason = state.get("reason")
            suffix = f" (reason: {reason})" if reason else ""
            return (
                f"⏸️ Hermes is already paused{suffix}. "
                "Use `/pause off` to resume."
            )
        estop.engage(reason=args or None)
        suffix = f" (reason: {args})" if args else ""
        return (
            f"⏸️ Paused{suffix}. New cron/kanban/gateway work is on hold; "
            "in-flight work finishes normally. Use `/pause off` to resume."
        )

    async def _busy_start_command(self, event: MessageEvent, quick_key: str, source):
        # Telegram sends /start for bot launches/deep-links. Treat it as a
        # platform ping, not a user command: no help dump, no agent
        # interrupt, no queued text.
        from gateway.run import logger
        logger.info("Ignoring /start platform ping for active session %s", quick_key)
        return ""

    async def _busy_egress_command(self, event: MessageEvent, quick_key: str, source):
        from hermes_cli.proxy_cli import format_status_text

        return format_status_text()

    async def _busy_stop_command(self, event: MessageEvent, quick_key: str, source):
        # /stop must hard-kill the session when an agent is running.
        # A soft interrupt (agent.interrupt()) doesn't help when the agent
        # is truly hung — the executor thread is blocked and never checks
        # _interrupt_requested.  Force-clean _running_agents so the session
        # is unlocked and subsequent messages are processed normally.
        from gateway.run import _INTERRUPT_REASON_STOP, logger
        await self._interrupt_and_clear_session(
            quick_key,
            source,
            interrupt_reason=_INTERRUPT_REASON_STOP,
            invalidation_reason="stop_command",
        )
        logger.info("STOP for session %s — agent interrupted, session lock released", quick_key)
        return EphemeralReply(t("gateway.stop.stopped"))

    async def _busy_new_command(self, event: MessageEvent, quick_key: str, source):
        # /reset and /new must bypass the running-agent guard so they
        # actually dispatch as commands instead of being queued as user
        # text (which would be fed back to the agent with the same
        # broken history — #2170).  Interrupt the agent first, then
        # clear the adapter's pending queue so the stale "/reset" text
        # doesn't get re-processed as a user message after the
        # interrupt completes.
        # Clear any pending messages so the old text doesn't replay
        from gateway.run import _INTERRUPT_REASON_RESET
        await self._interrupt_and_clear_session(
            quick_key,
            source,
            interrupt_reason=_INTERRUPT_REASON_RESET,
            invalidation_reason="new_command",
        )
        # Clean up the running agent entry so the reset handler
        # doesn't think an agent is still active.
        return await self._handle_reset_command(event)

    async def _busy_queue_command(self, event: MessageEvent, quick_key: str, source):
        # /queue <prompt> — queue without interrupting.
        # Semantics: each /queue invocation produces its own full agent
        # turn, processed in FIFO order after the current run (and any
        # earlier /queue items) finishes.  Messages are NOT merged.
        queued_text = event.get_command_args().strip()
        # Preserve media/reply payloads: a /queue carrying a photo,
        # document, or reply context is valid even with no prompt text
        # (e.g. "/queue" as the caption of an image). Dropping these
        # fields silently lost the attachment when the queued turn ran.
        has_media = bool(getattr(event, "media_urls", None))
        if not queued_text and not has_media:
            return "Usage: /queue <prompt>"
        adapter = self._adapter_for_source(source)
        if adapter:
            queued_event = MessageEvent(
                text=queued_text,
                message_type=event.message_type if has_media else MessageType.TEXT,
                source=event.source,
                raw_message=event.raw_message,
                message_id=event.message_id,
                media_urls=list(getattr(event, "media_urls", []) or []),
                media_types=list(getattr(event, "media_types", []) or []),
                reply_to_message_id=event.reply_to_message_id,
                reply_to_text=event.reply_to_text,
                reply_to_author_id=event.reply_to_author_id,
                reply_to_author_name=event.reply_to_author_name,
                reply_to_is_own_message=event.reply_to_is_own_message,
                auto_skill=event.auto_skill,
                channel_prompt=event.channel_prompt,
                channel_context=event.channel_context,
                internal=event.internal,
                timestamp=event.timestamp,
            )
            self._enqueue_fifo(quick_key, queued_event, adapter)
        depth = self._queue_depth(quick_key, adapter=self._adapter_for_source(source))
        if depth <= 1:
            return "Queued for the next turn."
        return f"Queued for the next turn. ({depth} queued)"

    async def _busy_steer_command(self, event: MessageEvent, quick_key: str, source):
        # /steer <prompt> — inject mid-run after the next tool call.
        # Unlike /queue (turn boundary), /steer lands BETWEEN tool-call
        # iterations inside the same agent run, by appending to the
        # last tool result's content. No interrupt, no new user turn,
        # no role-alternation violation.
        from gateway.run import _AGENT_PENDING_SENTINEL, logger
        steer_text = event.get_command_args().strip()
        if not steer_text:
            return "Usage: /steer <prompt>"
        _steer_state = self._peek_session_state(quick_key)
        running_agent = _steer_state.turn.agent if _steer_state else None
        if running_agent is _AGENT_PENDING_SENTINEL:
            # Agent hasn't started yet — queue as turn-boundary fallback.
            adapter = self._adapter_for_source(source)
            if adapter:
                queued_event = MessageEvent(
                    text=steer_text,
                    message_type=MessageType.TEXT,
                    source=event.source,
                    message_id=event.message_id,
                    channel_prompt=event.channel_prompt,
                    channel_context=event.channel_context,
                )
                self._enqueue_fifo(quick_key, queued_event, adapter)
            return "Agent still starting — /steer queued for the next turn."
        if running_agent and hasattr(running_agent, "steer"):
            try:
                accepted = running_agent.steer(steer_text)
            except Exception as exc:
                logger.warning("Steer failed for session %s: %s", quick_key, exc)
                return f"⚠️ Steer failed: {exc}"
            if accepted:
                preview = steer_text[:60] + ("..." if len(steer_text) > 60 else "")
                return f"⏩ Steer queued — arrives after the next tool call: '{preview}'"
            return "Steer rejected (empty payload)."
        # Running agent is missing or lacks steer() — fall back to queue.
        adapter = self._adapter_for_source(source)
        if adapter:
            queued_event = MessageEvent(
                text=steer_text,
                message_type=MessageType.TEXT,
                source=event.source,
                message_id=event.message_id,
                channel_prompt=event.channel_prompt,
                channel_context=event.channel_context,
            )
            self._enqueue_fifo(quick_key, queued_event, adapter)
        return "No active agent — /steer queued for the next turn."

    async def _busy_goal_command(self, event: MessageEvent, quick_key: str, source):
        # /goal is safe mid-run for status/pause/clear/wait (inspection
        # and control-plane only — doesn't interrupt the running turn).
        # Setting a new goal text mid-run is rejected with the same
        # "wait or /stop" message as /model so we don't race a second
        # continuation prompt against the current turn.
        _goal_arg = (event.get_command_args() or "").strip().lower()
        _goal_verb = _goal_arg.split(None, 1)[0] if _goal_arg else ""
        # Exact-match control verbs (unchanged semantics), plus the
        # wait/unwait barrier verbs which take a pid argument and the
        # gate management verb (inspection/mutation of the gate list only —
        # gates run at turn boundary, so editing them mid-run is safe).
        _is_control = (
            not _goal_arg
            or _goal_arg in {"status", "pause", "resume", "clear", "stop", "done", "unwait"}
            or _goal_verb in {"wait", "gate"}
        )
        if _is_control:
            return await self._handle_goal_command(event)
        return "Agent is running — use /goal status / pause / clear / wait mid-run, or /stop before setting a new goal."

    async def _busy_loop_command(self, event: MessageEvent, quick_key: str, source):
        # /loop mirrors /goal: control verbs are safe mid-run (state
        # only — read at the next idle boundary); setting a new loop
        # mid-run is rejected so we don't race the current turn.
        _loop_arg = (event.get_command_args() or "").strip().lower()
        if not _loop_arg or _loop_arg in {"status", "pause", "resume", "stop", "clear", "cancel", "help", "--help", "-h"}:
            return await self._handle_loop_command(event)
        return "Agent is running — use /loop status / pause / stop mid-run, or /stop before setting a new loop."

    async def _handle_message(self, event: MessageEvent) -> Optional[str]:
        """
        Handle an incoming message from any platform.
        
        This is the core message processing pipeline:
        1. Check user authorization
        2. Check for commands (/new, /reset, etc.)
        3. Check for running agent and interrupt if needed
        4. Get or create session
        5. Build context for agent
        6. Run agent conversation
        7. Return response
        """
        from gateway.run import _AGENT_PENDING_SENTINEL, _build_media_placeholder, _check_unavailable_skill, _float_env, _hermes_home, _is_slack_ignored_channel, logger
        source = event.source

        # 🔴 Cross-session leak guard. This handler runs inside a per-message
        # asyncio task created via create_task(), which snapshots the spawning
        # context with copy_context(). If a *concurrent* message had already
        # bound its session via set_session_vars() when this task was created,
        # we inherited ITS HERMES_SESSION_* ContextVars. Until we bind our own
        # (a few steps down, in _set_session_env), any subprocess spawned here
        # would read the foreign session's identity via the subprocess-env
        # bridge — the _UNSET-strip guard there can't help because the vars are
        # set-to-foreign, not _UNSET. Reset to _UNSET now so that window strips
        # safe (no session) instead of leaking the sibling's. See
        # gateway/session_context.reset_session_vars + the inheritance test.
        try:
            from gateway.session_context import reset_session_vars
            reset_session_vars()
        except Exception:
            logger.debug("reset_session_vars failed at handler entry", exc_info=True)

        # Most adapters resolve profile routes in build_source(), before they
        # hand us the event. A few internal/voice paths construct SessionSource
        # directly, so resolve those here as the shared fail-closed ingress gate
        # before authorization, hooks, or session side effects.
        if (
            getattr(getattr(self, "config", None), "multiplex_profiles", False)
            and not getattr(source, "profile", None)
            and getattr(source, "profile_route_rejected", False) is not True
        ):
            from gateway.profile_routing import ProfileRouteRejected

            try:
                source.profile = self._profile_name_for_source(source)
            except ProfileRouteRejected:
                source.profile_route_rejected = True

        # SessionSource owns a strict boolean marker. Require the literal value
        # so duck-typed test/internal sources with dynamic attributes are not
        # mistaken for an explicit matched-route rejection.
        if getattr(source, "profile_route_rejected", False) is True:
            logger.warning(
                "Dropping inbound message because its explicit profile route "
                "targets an unserved profile"
            )
            return None

        # Internal events (e.g. background-process completion notifications)
        # are system-generated and must skip user authorization.
        is_internal = bool(getattr(event, "internal", False))

        # Ignored-channel guard runs FIRST — before startup-restore queueing,
        # plugin hooks, auth, and session setup — so a configured ignored
        # channel can never reach pairing/auth/session state (#51899).
        # getattr: bare test runners construct GatewayRunner via
        # object.__new__ without config (see AGENTS.md pitfall on
        # object.__new__ test pattern).
        if (
            not is_internal
            and getattr(source, "platform", None) == Platform.SLACK
            and _is_slack_ignored_channel(
                getattr(self, "config", None), getattr(source, "chat_id", None)
            )
        ):
            logger.info(
                "Dropping Slack message from configured ignored channel %s",
                getattr(source, "chat_id", None),
            )
            return None

        if (
            getattr(self, "_startup_restore_in_progress", False)
            and not is_internal
            and not getattr(event, "_hermes_startup_restore_replay", False)
        ):
            self._queue_startup_restore_event(event)
            return None

        # scale-to-zero (Phase 0, 0.B/F13): stamp the gateway-scoped last-inbound
        # clock for real (user-originated) inbound only. Internal/system events
        # (background-process completions, startup-restore replays) are NOT
        # traffic — counting them would keep a genuinely idle gateway awake. This
        # clock is what the idle predicate (gateway/scale_to_zero.is_idle) reads.
        if not is_internal:
            self._scale_to_zero_note_real_inbound()

        # Fire pre_gateway_dispatch plugin hook for user-originated messages.
        # Plugins receive the MessageEvent and may return a dict influencing flow:
        #   {"action": "skip",    "reason": ...}    -> drop (no reply, plugin handled)
        #   {"action": "rewrite", "text":  ...}     -> replace event.text, continue
        #   {"action": "allow"}   /   None          -> normal dispatch
        # Hook runs BEFORE auth so plugins can handle unauthorized senders
        # (e.g. customer handover ingest) without triggering the pairing flow.
        if not is_internal:
            try:
                from hermes_cli.lifecycle import invoke_hook as _invoke_hook
                _hook_results = _invoke_hook(
                    "pre_gateway_dispatch",
                    event=event,
                    gateway=self,
                    # getattr: bare-runner tests build GatewayRunner via
                    # object.__new__ without __init__ (pitfall #17), and the
                    # hook must not fail dispatch over a missing attribute.
                    session_store=getattr(self, "session_store", None),
                )
            except Exception as _hook_exc:
                logger.warning("pre_gateway_dispatch invocation failed: %s", _hook_exc)
                _hook_results = []

            for _result in _hook_results:
                if not isinstance(_result, dict):
                    continue
                _action = _result.get("action")
                if _action == "skip":
                    logger.info(
                        "pre_gateway_dispatch skip: reason=%s platform=%s chat=%s",
                        _result.get("reason"),
                        source.platform.value if source.platform else "unknown",
                        source.chat_id or "unknown",
                    )
                    return None
                if _action == "rewrite":
                    _new_text = _result.get("text")
                    if isinstance(_new_text, str):
                        event = dataclasses.replace(event, text=_new_text)
                        source = event.source
                    break
                if _action == "allow":
                    break

        if is_internal:
            pass
        elif source.user_id is None:
            # Messages with no user identity (Telegram service messages,
            # channel forwards, anonymous admin posts, sender_chat) can't
            # be paired, but they can still be authorized via a
            # chat-scoped allowlist (e.g. TELEGRAM_GROUP_ALLOWED_CHATS
            # authorizes every member of the listed chat regardless of
            # sender). Defer to _is_user_authorized so that path runs.
            if not self._is_user_authorized(source):
                logger.debug("Ignoring message with no user_id from %s", source.platform.value)
                return None
        elif not self._is_user_authorized(source):
            logger.warning("Unauthorized user: %s (%s) on %s", source.user_id, source.user_name, source.platform.value)
            # In DMs: offer pairing code. In groups: silently ignore.
            if (
                source.chat_type == "dm"
                and self._get_unauthorized_dm_behavior(
                    source.platform,
                    profile=source.profile,
                )
                == "pair"
            ):
                platform_name = source.platform.value if source.platform else "unknown"
                pairing_store = self._pairing_store_for(source)
                if pairing_store is None:
                    logger.error(
                        "Cannot offer pairing code on %s: no pairing store",
                        platform_name,
                    )
                    return None
                # Rate-limit ALL pairing responses (code or rejection) to
                # prevent spamming the user with repeated messages when
                # multiple DMs arrive in quick succession.
                if pairing_store._is_rate_limited(platform_name, source.user_id):
                    return None
                code = pairing_store.generate_code(
                    platform_name, source.user_id, source.user_name or ""
                )
                if code:
                    adapter = self._adapter_for_source(source)
                    if adapter:
                        store_profile = getattr(pairing_store, "profile", None)
                        profile_arg = (
                            f"-p {store_profile} "
                            if isinstance(store_profile, str)
                            and store_profile
                            and store_profile != "default"
                            else ""
                        )
                        await adapter.send(
                            source.chat_id,
                            f"Hi~ I don't recognize you yet!\n\n"
                            f"Here's your pairing code: `{code}`\n\n"
                            f"Ask the bot owner to run:\n"
                            f"`hermes {profile_arg}pairing approve "
                            f"{platform_name} {code}`"
                        )
                else:
                    adapter = self._adapter_for_source(source)
                    if adapter:
                        await adapter.send(
                            source.chat_id,
                            "Too many pairing requests right now~ "
                            "Please try again later!"
                        )
                    # Record rate limit so subsequent messages are silently ignored
                    pairing_store._record_rate_limit(platform_name, source.user_id)
            return None

        # Global emergency stop (`hermes pause`): give new turns a brief
        # paused notice instead of starting an agent run. Internal events
        # (background-process completions from IN-FLIGHT work) bypass the
        # gate — pause stops NEW work, it never kills or orphans running
        # work. Placed after auth so unauthorized senders keep the normal
        # silent/pairing behavior and can't probe pause state.
        #
        # Passthroughs (pause blocks new AGENT turns, not control traffic):
        #   * recognized slash commands — /status, /help, /new, /approve and
        #     friends must keep working while paused, and /pause off is the
        #     in-band resume path for messaging-only users;
        #   * replies owned by IN-FLIGHT work — a pending detached-update
        #     prompt, clarify, slash-confirm, or dangerous-command approval,
        #     plus any message steering a session whose agent is already
        #     running. Swallowing those would stall work the pause promised
        #     not to touch.
        if not is_internal:
            try:
                from agent.estop import paused_reply as _estop_paused_reply
                _paused_notice = _estop_paused_reply()
            except ImportError:
                _paused_notice = None
            if _paused_notice is not None:
                _estop_allow = False
                _estop_cmd = None
                try:
                    _estop_cmd = event.get_command()
                except Exception:
                    _estop_cmd = None
                if _estop_cmd:
                    try:
                        from hermes_cli.commands import (
                            resolve_command as _resolve_estop_cmd,
                        )
                        _estop_allow = _resolve_estop_cmd(_estop_cmd) is not None
                    except Exception:
                        _estop_allow = False
                if not _estop_allow:
                    try:
                        _estop_key = self._session_key_for_source(source)
                        _estop_state = self._peek_session_state(_estop_key)
                        if (
                            _estop_state is not None
                            and _estop_state.persistent.update_prompt_pending
                        ):
                            _estop_allow = True
                        if not _estop_allow and self._is_session_running(_estop_key):
                            # Steering / interrupting in-flight work (which
                            # also covers pending clarify + tool approvals
                            # held by the running agent).
                            _estop_allow = True
                        if not _estop_allow:
                            from tools import slash_confirm as _estop_confirm_mod
                            if _estop_confirm_mod.get_pending(_estop_key):
                                _estop_allow = True
                        if not _estop_allow:
                            from tools.approval import (
                                has_blocking_approval as _estop_has_approval,
                            )
                            if _estop_has_approval(_estop_key):
                                _estop_allow = True
                    except Exception:
                        pass
                if not _estop_allow:
                    logger.info(
                        "Gateway turn paused by global emergency stop (platform=%s chat=%s)",
                        getattr(getattr(source, "platform", None), "value", "unknown"),
                        getattr(source, "chat_id", None) or "unknown",
                    )
                    return _paused_notice

        # Intercept messages that are responses to a pending /update prompt.
        # The update process (detached) wrote .update_prompt.json; the watcher
        # forwarded it to the user; now the user's reply goes back via
        # .update_response so the update process can continue.
        #
        # IMPORTANT: recognized slash commands must bypass this interception.
        # Otherwise control/session commands like /new or /help get silently
        # consumed as update answers instead of being dispatched normally.
        _quick_key = self._session_key_for_source(source)
        allow_gateway_control = event.allow_gateway_control
        _up_state = self._peek_session_state(_quick_key)
        if (
            allow_gateway_control
            and _up_state is not None
            and _up_state.persistent.update_prompt_pending
        ):
            raw = (event.text or "").strip()
            # Accept /approve and /deny as shorthand for yes/no
            cmd = event.get_command()
            if cmd in {"approve", "yes"}:
                response_text = "y"
            elif cmd in {"deny", "no"}:
                response_text = "n"
            else:
                _recognized_cmd = None
                if cmd:
                    try:
                        from hermes_cli.commands import resolve_command as _resolve_update_cmd
                    except Exception:
                        _resolve_update_cmd = None
                    if _resolve_update_cmd is not None:
                        try:
                            _cmd_def = _resolve_update_cmd(cmd)
                            _recognized_cmd = _cmd_def.name if _cmd_def else None
                        except Exception:
                            _recognized_cmd = None
                if _recognized_cmd:
                    response_text = ""
                else:
                    response_text = raw
            if response_text:
                response_path = _hermes_home / ".update_response"
                prompt_path = _hermes_home / ".update_prompt.json"
                try:
                    tmp = response_path.with_suffix(".tmp")
                    tmp.write_text(response_text, encoding="utf-8")
                    tmp.replace(response_path)
                    prompt_path.unlink(missing_ok=True)
                except OSError as e:
                    logger.warning("Failed to write update response: %s", e)
                    return f"✗ Failed to send response to update process: {e}"
                _up_state.persistent.update_prompt_pending = False
                label = response_text if len(response_text) <= 20 else response_text[:20] + "…"
                return f"✓ Sent `{label}` to the update process."
            # Recognized slash command during a pending update prompt:
            # unblock the detached update subprocess by writing a blank
            # response so ``_gateway_prompt`` returns the prompt's default
            # (typically a safe "n" / skip) and exits cleanly instead of
            # blocking on stdin until the 30-minute watcher timeout.
            # The slash command then falls through to normal dispatch.
            if _recognized_cmd:
                response_path = _hermes_home / ".update_response"
                prompt_path = _hermes_home / ".update_prompt.json"
                try:
                    tmp = response_path.with_suffix(".tmp")
                    tmp.write_text("", encoding="utf-8")
                    tmp.replace(response_path)
                    prompt_path.unlink(missing_ok=True)
                    logger.info(
                        "Recognized /%s during pending update prompt for %s; "
                        "cancelled prompt with default and dispatching command",
                        _recognized_cmd,
                        _quick_key,
                    )
                except OSError as e:
                    logger.warning(
                        "Failed to write cancel response for pending update prompt: %s",
                        e,
                    )
                _up_state.persistent.update_prompt_pending = False

        # Intercept messages that are responses to a pending clarify.
        # Open-ended prompts and "Other" responses are captured as free text;
        # direct replies to multi-choice prompts are accepted too ("2" maps
        # to the second option, arbitrary text becomes a custom answer). Slash
        # commands still bypass this path so /stop and friends keep working.
        _clarify_mod = None
        try:
            from tools import clarify_gateway as _clarify_mod
            _pending_clarify = _clarify_mod.get_pending_for_session(
                _quick_key, include_choice_prompts=True,
            )
        except Exception:
            _pending_clarify = None
        if (
            allow_gateway_control
            and _pending_clarify is not None
            and _clarify_mod is not None
        ):
            _clarify_has_audio = bool(self._pending_event_audio_paths(event))
            _raw_clarify_reply = await self._prepare_clarify_reply_text(event)
            if _clarify_has_audio and not _raw_clarify_reply:
                logger.info(
                    "Gateway retained pending clarify after voice transcription "
                    "produced no usable text (session=%s, id=%s)",
                    _quick_key,
                    _pending_clarify.clarify_id,
                )
                return ""
            # Skip slash commands — the user clearly wanted to issue a
            # command, not answer the clarify.  Leave the clarify pending
            # so the user can retry; if it times out, the agent unblocks
            # with an empty response.
            if _raw_clarify_reply and not _raw_clarify_reply.startswith("/"):
                _resolved = _clarify_mod.resolve_text_response_for_session(
                    _quick_key, _raw_clarify_reply,
                )
                if _resolved:
                    logger.info(
                        "Gateway intercepted clarify text response (session=%s, id=%s)",
                        _quick_key, _pending_clarify.clarify_id,
                    )
                    # The clarify callback pauses the platform typing/status
                    # indicator while waiting so Slack users can type their
                    # answer. The active agent resumes as soon as this reply
                    # resolves the wait, so re-enable its indicator here too.
                    # Without this, Slack stays silent until the independent
                    # long-running heartbeat fires (three minutes by default).
                    _clarify_adapter = self._adapter_for_source(source)
                    if _clarify_adapter:
                        try:
                            _clarify_adapter.resume_typing_for_chat(source.chat_id)
                        except Exception:
                            logger.debug(
                                "Failed to resume typing after clarify response",
                                exc_info=True,
                            )
                    # Acknowledge with empty string so adapters that emit
                    # the agent's response don't double-post.  The agent
                    # itself will produce the next user-facing message.
                    return ""

        # Intercept messages that are responses to a pending /reload-mcp
        # (or future) slash-confirm prompt.  Recognized confirm replies are
        # /approve, /always, /cancel (plus short aliases).  Anything else
        # falls through to normal dispatch — a stale pending confirm does
        # NOT block other commands.
        #
        # Important: if a dangerous-command approval is ALSO pending (agent
        # blocked inside tools/approval.py), the tool approval takes
        # precedence — /approve there unblocks the waiting tool thread.
        # Slash-confirm only catches /approve when no tool approval is live.
        from tools import slash_confirm as _slash_confirm_mod
        _pending_confirm = _slash_confirm_mod.get_pending(_quick_key)
        _tool_approval_live = False
        try:
            from tools.approval import has_blocking_approval
            _tool_approval_live = has_blocking_approval(_quick_key)
        except Exception:
            _tool_approval_live = False
        if allow_gateway_control and _pending_confirm and not _tool_approval_live:
            _raw_reply = (event.text or "").strip()
            # Accept bang-prefixed replies (`!always`, `!cancel`) verbatim.
            # Slack/Matrix instruction text shows the `!` prefix (typed `/`
            # is blocked in Slack threads), but the adapters only rewrite
            # `!<known-command>` — `always`/`cancel` are confirm keywords,
            # not registered commands, so the `!` survives to here.
            _norm_reply = _raw_reply.lstrip("!/").lower()
            _cmd_reply = event.get_command()
            _confirm_choice = None
            if _cmd_reply in {"approve", "yes", "ok", "confirm"}:
                _confirm_choice = "once"
            elif _cmd_reply in {"always", "remember"}:
                _confirm_choice = "always"
            elif _cmd_reply in {"cancel", "no", "deny", "nevermind"}:
                _confirm_choice = "cancel"
            elif _norm_reply in {"approve", "approve once", "once"}:
                _confirm_choice = "once"
            elif _norm_reply in {"always", "always approve"}:
                _confirm_choice = "always"
            elif _norm_reply in {"cancel", "nevermind", "no"}:
                _confirm_choice = "cancel"
            if _confirm_choice is not None:
                _resolved = await _slash_confirm_mod.resolve(
                    _quick_key, _pending_confirm.get("confirm_id"), _confirm_choice,
                )
                return _resolved or ""
            # Stale pending + unrelated command: drop the pending state so
            # the confirm doesn't block normal usage indefinitely.  The user
            # clearly moved on.
            _slash_confirm_mod.clear_if_stale(_quick_key)

        # PRIORITY handling when an agent is already running for this session.
        # Default behavior is to interrupt immediately so user text/stop messages
        # are handled with minimal latency.
        #
        # Special case: Telegram/photo bursts often arrive as multiple near-
        # simultaneous updates. Do NOT interrupt for photo-only follow-ups here;
        # let the adapter-level batching/queueing logic absorb them.

        # Staleness eviction: detect leaked locks from hung/crashed handlers.
        # With inactivity-based timeout, active tasks can run for hours, so
        # wall-clock age alone isn't sufficient.  Evict only when the agent
        # has been *idle* beyond the inactivity threshold (or when the agent
        # object has no activity tracker and wall-clock age is extreme).
        _raw_stale_timeout = _float_env("HERMES_AGENT_TIMEOUT", 1800)
        _quick_state = self._peek_session_state(_quick_key)
        _stale_ts = _quick_state.turn.started_ts if _quick_state else 0
        if _quick_state is not None and _quick_state.turn.agent is not None and _stale_ts:
            _stale_age = time.time() - _stale_ts
            _stale_agent = _quick_state.turn.agent
            # Never evict the pending sentinel — it was just placed moments
            # ago during the async setup phase before the real agent is
            # created.  Sentinels have no get_activity_summary(), so the
            # idle check below would always evaluate to inf >= timeout and
            # immediately evict them, racing with the setup path.
            _stale_idle = float("inf")  # assume idle if we can't check
            _stale_detail = ""
            if _stale_agent and hasattr(_stale_agent, "get_activity_summary"):
                try:
                    _sa = _stale_agent.get_activity_summary()
                    _stale_idle = _sa.get("seconds_since_activity", float("inf"))
                    _stale_detail = (
                        f" | last_activity={_sa.get('last_activity_desc', 'unknown')} "
                        f"({_stale_idle:.0f}s ago) "
                        f"| iteration={_sa.get('api_call_count', 0)}/{_sa.get('max_iterations', 0)}"
                    )
                except Exception:
                    pass
            # Evict if: agent is idle beyond timeout, OR wall-clock age is
            # extreme (10x timeout or 2h, whichever is larger — catches
            # cases where the agent object was garbage-collected).
            _wall_ttl = max(_raw_stale_timeout * 10, 7200) if _raw_stale_timeout > 0 else float("inf")
            _should_evict = (
                _stale_agent is not _AGENT_PENDING_SENTINEL
                and (
                    (_raw_stale_timeout > 0 and _stale_idle >= _raw_stale_timeout)
                    or _stale_age > _wall_ttl
                )
            )
            if _should_evict:
                logger.warning(
                    "Evicting stale _running_agents entry for %s "
                    "(age: %.0fs, idle: %.0fs, timeout: %.0fs)%s",
                    _quick_key, _stale_age, _stale_idle,
                    _raw_stale_timeout, _stale_detail,
                )
                self._invalidate_session_run_generation(
                    _quick_key,
                    reason="stale_running_agent_eviction",
                )
                self._release_running_agent_state(_quick_key)

        if self._is_session_running(_quick_key):
            # Resolve the command once; every command's mid-run behavior is
            # declared on its CommandDef (busy_policy / busy_handler in
            # hermes_cli/commands.py) and dispatched through the single
            # resolver _dispatch_busy_slash_command below — no per-command
            # if-chain here.
            from hermes_cli.commands import resolve_command as _resolve_cmd_inner
            _evt_cmd = event.get_command()
            _cmd_def_inner = _resolve_cmd_inner(_evt_cmd) if _evt_cmd else None

            # /status and /context are intentionally pre-gate so users
            # always see session state.
            if _cmd_def_inner and _cmd_def_inner.name == "status":
                return await self._handle_status_command(event)
            if _cmd_def_inner and _cmd_def_inner.name == "context":
                return await self._handle_context_command(event)

            # Slash command access control on the running-agent fast-path.
            # Mirrors the cold-path gate further below so non-admin users
            # can't bypass gating just because an agent happens to be busy.
            # /status above is intentionally pre-gate so users always see
            # session state. /help and /whoami fall under the always-allowed
            # floor inside _check_slash_access.
            if _evt_cmd and _cmd_def_inner is not None:
                _denied = self._check_slash_access(source, _cmd_def_inner.name)
                if _denied is not None:
                    return _denied

            # Any recognized slash command: dispatch according to its
            # declared busy_policy (dispatch / interrupt_then_dispatch /
            # reject). Unrecognized commands and plain text fall through
            # to the interrupt/queue logic below.
            if _cmd_def_inner:
                return await self._dispatch_busy_slash_command(
                    event, _cmd_def_inner, _quick_key, source,
                )

            if event.message_type == MessageType.PHOTO:
                logger.debug("PRIORITY photo follow-up for session %s — queueing without interrupt", _quick_key)
                adapter = self._adapter_for_source(source)
                if adapter:
                    merge_pending_message_event(adapter._pending_messages, _quick_key, event)
                return None

            effective_busy_input_mode = self._effective_busy_input_mode(source)
            _telegram_followup_grace = float(
                os.getenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "3.0")
            )
            _grace_state = self._peek_session_state(_quick_key)
            _started_at = _grace_state.turn.started_ts if _grace_state else 0
            if (
                source.platform == Platform.TELEGRAM
                and event.message_type == MessageType.TEXT
                and _telegram_followup_grace > 0
                and _started_at
                and (time.time() - _started_at) <= _telegram_followup_grace
            ):
                logger.debug(
                    "Telegram follow-up arrived %.2fs after run start for %s — queueing without interrupt",
                    time.time() - _started_at,
                    _quick_key,
                )
                adapter = self._adapter_for_source(source)
                if adapter:
                    if effective_busy_input_mode == "queue":
                        self._enqueue_fifo(_quick_key, event, adapter)
                    else:
                        merge_pending_message_event(
                            adapter._pending_messages,
                            _quick_key,
                            event,
                            merge_text=True,
                        )
                return None

            _ra_state = self._peek_session_state(_quick_key)
            running_agent = _ra_state.turn.agent if _ra_state else None
            if running_agent is _AGENT_PENDING_SENTINEL:
                # Agent is being set up but not ready yet.
                if event.get_command() == "stop":
                    # Force-clean the sentinel so the session is unlocked.
                    self._release_running_agent_state(_quick_key)
                    logger.info("HARD STOP (pending) for session %s — sentinel cleared", _quick_key)
                    return EphemeralReply("⚡ Force-stopped. The agent was still starting — session unlocked.")
                # Queue the message so it will be picked up after the
                # agent starts.
                adapter = self._adapter_for_source(source)
                if adapter:
                    merge_pending_message_event(
                        adapter._pending_messages,
                        _quick_key,
                        event,
                        merge_text=True,
                    )
                return None
            if self._draining:
                queue_during_drain = self._queue_during_drain_enabled(
                    effective_busy_input_mode
                )
                if queue_during_drain:
                    self._queue_or_replace_pending_event(_quick_key, event)
                return (
                    f"⏳ Gateway {self._status_action_gerund()} — queued for the next turn after it comes back."
                    if queue_during_drain
                    else f"⏳ Gateway is {self._status_action_gerund()} and is not accepting another turn right now."
                )
            if effective_busy_input_mode == "queue":
                logger.debug("PRIORITY queue follow-up for session %s", _quick_key)
                self._queue_or_replace_pending_event(_quick_key, event)
                return None
            if effective_busy_input_mode == "steer":
                # Steer mode: inject text into the running agent mid-run via
                # agent.steer().  Falls back to queue semantics if the payload
                # is empty, the agent lacks steer(), or steer() rejects.
                steer_text = (event.text or "").strip()
                steered = False
                if (
                    event.message_type == MessageType.TEXT
                    and not event.media_urls
                    and not event.media_types
                    and steer_text
                    and hasattr(running_agent, "steer")
                ):
                    try:
                        steered = bool(running_agent.steer(steer_text))
                    except Exception as exc:
                        logger.warning("PRIORITY steer failed for session %s: %s", _quick_key, exc)
                        steered = False
                if steered:
                    logger.debug("PRIORITY steer for session %s", _quick_key)
                    return None
                logger.debug("PRIORITY steer-fallback-to-queue for session %s", _quick_key)
                self._queue_or_replace_pending_event(_quick_key, event)
                return None
            # #30170 — Subagent protection (PRIORITY path). Same rationale
            # as ``_handle_active_session_busy_message``: an interrupt
            # cascades through ``_active_children`` and aborts in-flight
            # delegate_task work. Demote to queue semantics when the
            # parent is currently driving subagents so a conversational
            # follow-up doesn't destroy minutes of subagent progress.
            # /stop reaches its dedicated handler above, so the operator
            # still has a clean escape hatch.
            if self._agent_has_active_subagents(running_agent):
                logger.info(
                    "PRIORITY interrupt demoted to queue for session %s "
                    "because the running agent has active subagents (#30170)",
                    _quick_key,
                )
                self._queue_or_replace_pending_event(_quick_key, event)
                return None
            # #56391 — Compression protection (PRIORITY path). Same
            # rationale as ``_handle_active_session_busy_message``: context
            # compression is interrupt-protected (#23975), but an interrupt
            # here starts a new turn against the pre-rotation parent
            # session while the still-running compression later rotates
            # the id out from under it, forking orphaned compression
            # siblings. Demote to queue semantics so the follow-up waits
            # for the in-flight compression + rotation to land.
            if await self._session_has_compression_in_flight(_quick_key):
                logger.info(
                    "PRIORITY interrupt demoted to queue for session %s "
                    "because context compression is in flight (#56391)",
                    _quick_key,
                )
                self._queue_or_replace_pending_event(_quick_key, event)
                return None
            # Text-only corrections redirect the live turn (preserving
            # displayed context) when the runtime supports it; media/voice and
            # older runtimes fall back to the proven interrupt path below.
            if (
                event.message_type == MessageType.TEXT
                and not event.media_urls
                and not event.media_types
                and getattr(running_agent, "_supports_active_turn_redirect", False)
                is True
                and hasattr(running_agent, "redirect")
            ):
                try:
                    if running_agent.redirect((event.text or "").strip()):
                        logger.debug("PRIORITY redirect for session %s", _quick_key)
                        return None
                except Exception as exc:
                    logger.warning(
                        "PRIORITY redirect failed for session %s: %s",
                        _quick_key,
                        exc,
                    )
            logger.debug("PRIORITY interrupt for session %s", _quick_key)
            _interrupt_text = event.text
            _media_urls = getattr(event, "media_urls", None) or []
            if self._pending_event_audio_paths(event):
                _interrupt_text, _ = await self._transcribe_and_echo_pending_voice(
                    event,
                    self._adapter_for_source(source),
                    source,
                    event.text or "",
                    log_context="Voice-priority-interrupt",
                )
            elif not _interrupt_text and _media_urls:
                _interrupt_text = _build_media_placeholder(event)
            running_agent.interrupt(_interrupt_text)
            # NOTE: self._pending_messages was write-only (never consumed).
            # The actual interrupt message is delivered via adapter._pending_messages
            # which is read by _run_agent. Removed to prevent unbounded growth.
            return None

        # Check for commands
        command = event.get_command()

        from hermes_cli.commands import (
            GATEWAY_KNOWN_COMMANDS,
            is_gateway_known_command,
            resolve_command as _resolve_cmd,
        )

        # Resolve aliases to canonical name so dispatch and hook names
        # don't depend on the exact alias the user typed.
        _cmd_def = _resolve_cmd(command) if command else None
        canonical = _cmd_def.name if _cmd_def else command

        # Expand alias quick commands before built-in dispatch so targets like
        # /model openai/gpt-5.5 --provider openrouter reach the /model handler.
        # Preserve built-in precedence; aliases only need early handling when
        # the typed command is not already known.
        if command and _cmd_def is None:
            if isinstance(self.config, dict):
                quick_commands = self.config.get("quick_commands", {}) or {}
            else:
                quick_commands = getattr(self.config, "quick_commands", {}) or {}
            if isinstance(quick_commands, dict) and command in quick_commands:
                qcmd = quick_commands[command]
                if qcmd.get("type") == "alias":
                    target = (qcmd.get("target") or "").strip()
                    if target:
                        target = target if target.startswith("/") else f"/{target}"
                        target_command = target.lstrip("/")
                        user_args = event.get_command_args().strip()
                        event.text = f"{target} {user_args}".strip()
                        command = target_command.split()[0] if target_command else target_command
                        _cmd_def = _resolve_cmd(command) if command else None
                        canonical = _cmd_def.name if _cmd_def else command

        # Per-platform slash command access control. Only kicks in when the
        # operator has set ``allow_admin_from`` for the source's scope (DM
        # vs group). When unset → backward-compat: every allowed user can
        # run every command. When set → non-admins can run only commands in
        # ``user_allowed_commands`` (plus the always-allowed floor: /help,
        # /whoami). Plain chat is unaffected — only slash commands gate.
        if command and canonical and is_gateway_known_command(canonical):
            _denied = self._check_slash_access(source, canonical)
            if _denied is not None:
                return _denied

        # pre_command observer hook (#64204): fires for every recognized
        # slash command BEFORE core handling, mirroring the CLI fire-site in
        # cli.py process_command. Observer-only in v1 (returns ignored).
        #
        # Placement matters: this cold-path dispatch is only reached when NO
        # agent is running for the session. The running-agent intercept path
        # above (/stop, /approve, busy_policy dispatch via
        # _dispatch_busy_slash_command) deliberately does NOT fire this hook —
        # those are control-plane operations on an in-flight run, and giving
        # plugins an observation (and eventually veto) point there would let
        # a slow or hostile plugin interfere with the operator's escape
        # hatches for a live agent.
        if command and is_gateway_known_command(canonical):
            try:
                from hermes_cli.plugins import fire_pre_command_hook
                fire_pre_command_hook(
                    surface="gateway",
                    command=str(canonical),
                    alias_used=str(command),
                    args_raw=event.get_command_args().strip(),
                    session_key=_quick_key,
                    platform=source.platform.value if source.platform else "",
                )
            except Exception as _pre_cmd_err:
                logger.debug(
                    "pre_command hook dispatch failed (non-fatal): %s",
                    _pre_cmd_err,
                )

        # Fire the ``command:<canonical>`` hook for any recognized slash
        # command — built-in OR plugin-registered. Handlers can return a
        # dict with ``{"decision": "deny" | "handled" | "rewrite", ...}``
        # to intercept dispatch before core handling runs. This replaces
        # the previous fire-and-forget emit(): return values are now
        # honored, but handlers that return nothing behave exactly as
        # before (telemetry-style hooks keep working).
        if command and is_gateway_known_command(canonical):
            raw_args = event.get_command_args().strip()
            hook_ctx = {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "command": canonical,
                "raw_command": command,
                "args": raw_args,
                "raw_args": raw_args,
            }
            try:
                hook_results = await self.hooks.emit_collect(
                    f"command:{canonical}", hook_ctx
                )
            except Exception as _hook_err:
                logger.debug(
                    "command:%s hook dispatch failed (non-fatal): %s",
                    canonical, _hook_err,
                )
                hook_results = []

            for hook_result in hook_results:
                if not isinstance(hook_result, dict):
                    continue
                decision = str(hook_result.get("decision", "")).strip().lower()
                if not decision or decision == "allow":
                    continue
                if decision == "deny":
                    message = hook_result.get("message")
                    if isinstance(message, str) and message:
                        return message
                    return f"Command `/{command}` was blocked by a hook."
                if decision == "handled":
                    message = hook_result.get("message")
                    return message if isinstance(message, str) and message else None
                if decision == "rewrite":
                    new_command = str(
                        hook_result.get("command_name", "")
                    ).strip().lstrip("/")
                    if not new_command:
                        continue
                    new_args = str(hook_result.get("raw_args", "")).strip()
                    event.text = f"/{new_command} {new_args}".strip()
                    command = event.get_command()
                    _cmd_def = _resolve_cmd(command) if command else None
                    canonical = _cmd_def.name if _cmd_def else command
                    break

        if canonical == "pause":
            return await self._handle_pause_command(event)

        if canonical == "new":
            if await asyncio.to_thread(self._is_telegram_topic_root_lobby, source):
                return self._telegram_topic_root_new_message()
            async def _do_reset():
                return await self._handle_reset_command(event)
            return await self._maybe_confirm_destructive_slash(
                event=event,
                command="new",
                title="/new",
                detail=(
                    "This starts a fresh session and discards the current "
                    "conversation history."
                ),
                execute=_do_reset,
            )

        if canonical == "topic":
            return await self._handle_topic_command(event)
        
        if canonical == "help":
            return await self._handle_help_command(event)

        if canonical == "start":
            logger.info("Ignoring /start platform ping for session %s", _quick_key)
            return ""

        if canonical == "commands":
            return await self._handle_commands_command(event)
        
        if canonical == "profile":
            return await self._handle_profile_command(event)

        if canonical == "whoami":
            return await self._handle_whoami_command(event)

        if canonical == "status":
            return await self._handle_status_command(event)

        if canonical == "egress":
            from hermes_cli.proxy_cli import format_status_text

            return format_status_text()

        if canonical == "context":
            return await self._handle_context_command(event)

        if canonical == "agents":
            return await self._handle_agents_command(event)

        if canonical == "platform":
            return await self._handle_platform_command(event)

        if canonical == "restart":
            return await self._handle_restart_command(event)
        
        if canonical == "stop":
            return await self._handle_stop_command(event)
        
        if canonical == "reasoning":
            return await self._handle_reasoning_command(event)

        if canonical == "memory":
            return await self._handle_memory_command(event)

        if canonical == "skills":
            return await self._handle_skills_command(event)

        if canonical == "learn":
            # Open-ended: rewrite the turn to a standards-guided prompt and fall
            # through to normal agent processing. The live agent gathers the
            # sources the user described (dirs via read_file, URLs via
            # web_extract, this conversation, pasted text) and authors the skill
            # via skill_manage. Mirrors the /blueprint fall-through so role
            # alternation is preserved. No engine, works on any backend.
            from agent.learn_prompt import build_learn_prompt

            _learn_req = event.get_command_args().strip()
            _ack = (
                "Learning a skill from what you described…"
                if _learn_req
                else "Learning a skill from this conversation…"
            )
            try:
                adapter = self._adapter_for_source(source)
                if adapter:
                    _ack_meta = self._thread_metadata_for_source(source)
                    await adapter.send(str(source.chat_id), _ack, metadata=_ack_meta)
            except Exception:
                logger.debug("learn ack send failed", exc_info=True)
            try:
                event.text = build_learn_prompt(_learn_req)
                # fall through to agent processing
            except Exception:
                return "Could not start /learn — please try again."

        if canonical == "init":
            # /init: rewrite the turn to a guidance-laden prompt and fall
            # through to normal agent processing (same fall-through as /learn
            # so role alternation is preserved). The live agent scans the
            # project with its own read-only tools and writes/updates
            # AGENTS.md via write_file. No engine, works on any backend.
            from hermes_cli.init_command import build_init_prompt_for_cwd

            _init_notes = event.get_command_args().strip()
            try:
                _init_prompt = build_init_prompt_for_cwd(extra=_init_notes)
            except Exception:
                return "Could not start /init — please try again."
            _ack = (
                "Updating AGENTS.md from a project scan…"
                if "UPDATE the existing AGENTS.md" in _init_prompt
                else "Generating AGENTS.md from a project scan…"
            )
            try:
                adapter = self._adapter_for_source(source)
                if adapter:
                    _ack_meta = self._thread_metadata_for_source(source)
                    await adapter.send(str(source.chat_id), _ack, metadata=_ack_meta)
            except Exception:
                logger.debug("init ack send failed", exc_info=True)
            event.text = _init_prompt
            # fall through to agent processing

        if canonical == "fast":
            return await self._handle_fast_command(event)

        if canonical == "verbose":
            return await self._handle_verbose_command(event)

        if canonical == "footer":
            return await self._handle_footer_command(event)

        if canonical == "yolo":
            return await self._handle_yolo_command(event)

        if canonical == "approvals":
            return await self._handle_approvals_command(event)

        if canonical == "model":
            return await self._handle_model_command(event)

        if canonical == "codex-runtime":
            return await self._handle_codex_runtime_command(event)

        if canonical == "personality":
            return await self._handle_personality_command(event)

        if canonical == "kanban":
            return await self._handle_kanban_command(event)

        if canonical == "suggestions":
            return await self._handle_suggestions_command(event)

        if canonical == "blueprint":
            _blueprint_result = await self._handle_blueprint_command(event)
            _blueprint_seed = getattr(_blueprint_result, "agent_seed", None)
            if _blueprint_seed:
                # Blueprint matched — rewrite the turn to the seed and fall
                # through to _handle_message_with_agent so the agent asks the
                # user for each slot value conversationally and then calls the
                # cronjob tool (the /steer fall-through pattern). The seed
                # enters as a normal user turn, preserving role alternation.
                # Send the "Setting up X…" ack first so the user gets the same
                # immediate feedback CLI users see, instead of silence until
                # the agent's first question.
                _ack = getattr(_blueprint_result, "text", "") or ""
                if _ack:
                    try:
                        adapter = self._adapter_for_source(source)
                        if adapter:
                            _ack_meta = self._thread_metadata_for_source(source)
                            await adapter.send(str(source.chat_id), _ack, metadata=_ack_meta)
                    except Exception:
                        logger.debug("blueprint ack send failed", exc_info=True)
                try:
                    event.text = _blueprint_seed
                except Exception:
                    return getattr(_blueprint_result, "text", "") or None
            else:
                return getattr(_blueprint_result, "text", "") or None

        if canonical == "retry":
            return await self._handle_retry_command(event)
        
        if canonical == "undo":
            async def _do_undo():
                return await self._handle_undo_command(event)
            _undo_n = 1
            _undo_raw = event.get_command_args().strip()
            if _undo_raw:
                try:
                    _undo_n = max(1, int(_undo_raw.split()[0]))
                except (ValueError, IndexError):
                    _undo_n = 1
            _undo_detail = (
                "This removes the last user/assistant exchange from history."
                if _undo_n == 1
                else f"This removes the last {_undo_n} user turns from history."
            )
            return await self._maybe_confirm_destructive_slash(
                event=event,
                command="undo",
                title="/undo",
                detail=_undo_detail,
                execute=_do_undo,
            )
        
        if canonical == "sethome":
            return await self._handle_set_home_command(event)

        if canonical == "compress":
            return await self._handle_compress_command(event)

        if canonical == "usage":
            return await self._handle_usage_command(event)

        if canonical == "topup":
            return await self._handle_topup_command(event)

        if canonical == "insights":
            return await self._handle_insights_command(event)

        if canonical == "reload-mcp":
            return await self._handle_reload_mcp_command(event)

        if canonical == "reload-skills":
            return await self._handle_reload_skills_command(event)

        if canonical == "bundles":
            return await self._handle_bundles_command(event)

        if canonical == "approve":
            return await self._handle_approve_command(event)

        if canonical == "deny":
            return await self._handle_deny_command(event)

        if canonical == "update":
            return await self._handle_update_command(event)

        if canonical == "version":
            return await self._handle_version_command(event)

        if canonical == "debug":
            return await self._handle_debug_command(event)

        if canonical == "title":
            return await self._handle_title_command(event)

        if canonical == "resume":
            return await self._handle_resume_command(event)

        if canonical == "sessions":
            return await self._handle_sessions_command(event)

        if canonical == "branch":
            return await self._handle_branch_command(event)

        if canonical == "rollback":
            return await self._handle_rollback_command(event)

        if canonical == "diff":
            return await self._handle_diff_command(event)

        if canonical == "background":
            return await self._handle_background_command(event)

        if canonical == "queue":
            queue_payload = event.get_command_args().strip()
            if not queue_payload:
                return "Usage: /queue <prompt>"
            try:
                event.text = queue_payload
            except Exception:
                pass

        if canonical == "steer":
            # No active agent — /steer has no tool call to inject into.
            # Strip the prefix so downstream treats it as a normal user
            # message. If the payload is empty, surface the usage hint.
            steer_payload = event.get_command_args().strip()
            if not steer_payload:
                return "Usage: /steer <prompt>  (no agent is running; sending as a normal message)"
            try:
                event.text = steer_payload
            except Exception:
                pass
            # Do NOT return — fall through to _handle_message_with_agent
            # at the end of this function so the rewritten text is sent
            # to the agent as a regular user turn.

        if canonical == "goal":
            return await self._handle_goal_command(event)

        if canonical == "loop":
            return await self._handle_loop_command(event)

        if canonical == "heartbeat":
            return await self._handle_heartbeat_command(event)
        if canonical == "refine":
            return await self._handle_refine_command(event)

        if canonical == "moa":
            # /moa is one-shot sugar only: run a single prompt through the
            # default MoA preset, then restore the prior model. To *switch* to a
            # MoA preset for the session, pick it from the model picker (MoA
            # presets surface as a virtual "Mixture of Agents" provider).
            from hermes_cli.moa_config import (
                moa_usage,
                normalize_moa_config,
            )
            from hermes_cli.config import load_config

            moa_payload = event.get_command_args().strip()
            if not moa_payload:
                return moa_usage()
            try:
                cfg = load_config()
                moa_cfg = normalize_moa_config(cfg.get("moa") if isinstance(cfg, dict) else {})
            except Exception:
                moa_cfg = normalize_moa_config({})
            preset = moa_cfg["default_preset"]
            try:
                event.text = moa_payload
                _moa_state = self._session_state(_quick_key)
                event._moa_restore_override = _moa_state.conversation.model_override
                _moa_state.conversation.model_override = {
                    "provider": "moa",
                    "model": preset,
                    "base_url": "moa://local",
                    "api_key": "moa-virtual-provider",
                    "api_mode": "chat_completions",
                }
                self._evict_cached_agent(_quick_key)
                event._moa_disable_after_turn = True
            except Exception:
                return "Failed to prepare MoA turn."

        if canonical == "subgoal":
            return await self._handle_subgoal_command(event)

        if canonical == "voice":
            return await self._handle_voice_command(event)

        if self._draining:
            return f"⏳ Gateway is {self._status_action_gerund()} and is not accepting new work right now."

        # User-defined quick commands (bypass agent loop, no LLM call)
        if command:
            if isinstance(self.config, dict):
                quick_commands = self.config.get("quick_commands", {}) or {}
            else:
                quick_commands = getattr(self.config, "quick_commands", {}) or {}
            if not isinstance(quick_commands, dict):
                quick_commands = {}
            if command in quick_commands:
                # Quick commands are slash capabilities too — and type:exec
                # ones run a shell command in the gateway process. The early
                # gate above only fires for registry-known commands, so quick
                # commands (never in the registry) would otherwise reach this
                # dispatch sink unchecked. Apply the same admin/user policy to
                # the raw typed name here so non-admins can't invoke admin-only
                # quick commands. (#44727)
                _denied = self._check_slash_access(source, command)
                if _denied is not None:
                    return _denied
                qcmd = quick_commands[command]
                if qcmd.get("type") == "exec":
                    exec_cmd = qcmd.get("command", "")
                    if exec_cmd:
                        try:
                            # Sanitize env to prevent credential leakage —
                            # quick commands run in the gateway process which
                            # has all API keys in os.environ.
                            from tools.environments.local import build_subprocess_env
                            sanitized_env = build_subprocess_env()
                            proc = await asyncio.create_subprocess_shell(
                                exec_cmd,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                                env=sanitized_env,
                            )
                            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                            output = (stdout or stderr).decode().strip()
                            # Redact any remaining sensitive patterns in output
                            if output:
                                from agent.redact import redact_sensitive_text
                                output = redact_sensitive_text(output)
                            return output if output else "Command returned no output."
                        except asyncio.TimeoutError:
                            return "Quick command timed out (30s)."
                        except Exception as e:
                            return f"Quick command error: {e}"
                    else:
                        return f"Quick command '/{command}' has no command defined."
                elif qcmd.get("type") == "alias":
                    target = (qcmd.get("target") or "").strip()
                    if target:
                        target = target if target.startswith("/") else f"/{target}"
                        target_command = target.lstrip("/")
                        user_args = event.get_command_args().strip()
                        event.text = f"{target} {user_args}".strip()
                        command = target_command.split()[0] if target_command else target_command
                        # Fall through to normal command dispatch below
                    else:
                        return f"Quick command '/{command}' has no target defined."
                else:
                    return f"Quick command '/{command}' has unsupported type (supported: 'exec', 'alias')."

        # Plugin-registered slash commands
        if command:
            try:
                from hermes_cli.plugins import get_plugin_command_handler
                # Normalize underscores to hyphens so Telegram's underscored
                # autocomplete form matches plugin commands registered with
                # hyphens. See hermes_cli/commands.py:_build_telegram_menu.
                plugin_handler = get_plugin_command_handler(command.replace("_", "-"))
                if plugin_handler:
                    user_args = event.get_command_args().strip()
                    result = plugin_handler(user_args)
                    if asyncio.iscoroutine(result):
                        result = await result
                    return str(result) if result else None
            except Exception as e:
                logger.warning("Plugin command dispatch failed: %s", e)

        # Skill slash commands: /skill-name loads the skill and sends to agent.
        # resolve_skill_command_key() handles the Telegram underscore/hyphen
        # round-trip so /claude_code from Telegram autocomplete still resolves
        # to the claude-code skill.
        if command:
            # Skill bundles take precedence over individual skill commands —
            # /<bundle> loads multiple skills at once. Mirrors CLI dispatch.
            _bundle_handled = False
            try:
                from agent.skill_bundles import (
                    build_bundle_invocation_message,
                    resolve_bundle_command_key,
                )
                bundle_key = resolve_bundle_command_key(command)
                if bundle_key is not None:
                    user_instruction = event.get_command_args().strip()
                    # Pass the platform explicitly: bundle skill loading
                    # bypasses get_skill_commands()' scan-time disabled
                    # filter, and the gateway serves multiple platforms in
                    # one process, so env-var platform resolution can't be
                    # trusted here. Mirrors the stacked-skill gate (#58888).
                    _bundle_plat = source.platform.value if source.platform else None
                    bundle_result = build_bundle_invocation_message(
                        bundle_key, user_instruction, task_id=_quick_key,
                        platform=_bundle_plat,
                    )
                    if bundle_result:
                        msg, _loaded, missing = bundle_result
                        event.text = msg
                        _bundle_handled = True
                        if missing:
                            logger.info(
                                "Bundle %s skipped missing skills: %s",
                                bundle_key, ", ".join(missing),
                            )
                        # Fall through to normal message processing with bundle content
            except Exception as exc:
                logger.warning("Bundle dispatch failed: %s", exc)

        if command and not locals().get("_bundle_handled", False):
            try:
                from agent.skill_commands import (
                    get_skill_commands,
                    build_skill_invocation_message,
                    resolve_skill_command_key,
                )
                skill_cmds = get_skill_commands()
                cmd_key = resolve_skill_command_key(command)
                if cmd_key is not None:
                    # Check per-platform disabled status before executing.
                    # get_skill_commands() only applies the *global* disabled
                    # list at scan time; per-platform overrides need checking
                    # here because the cache is process-global across platforms.
                    _skill_name = skill_cmds[cmd_key].get("name", "")
                    _plat = source.platform.value if source.platform else None
                    if _plat and _skill_name:
                        from agent.skill_utils import get_disabled_skill_names as _get_plat_disabled
                        if _skill_name in _get_plat_disabled(platform=_plat):
                            return (
                                f"The **{_skill_name}** skill is disabled for {_plat}.\n"
                                f"Enable it with: `hermes skills config`"
                            )
                    user_instruction = event.get_command_args().strip()
                    # Stacked slash-skill invocations: `/skill-a /skill-b do
                    # XYZ` loads every leading skill (up to 5), not just the
                    # first. Inspired by Claude Code v2.1.199. Mirrors CLI.
                    try:
                        from agent.skill_commands import (
                            build_stacked_skill_invocation_message as _build_stacked,
                            split_stacked_skill_commands,
                        )
                        extra_keys, stacked_instruction = (
                            split_stacked_skill_commands(user_instruction)
                        )
                    except Exception:
                        _build_stacked = None
                        extra_keys, stacked_instruction = [], user_instruction
                    if extra_keys and _plat:
                        # split_stacked_skill_commands() only resolves that
                        # each extra token is a KNOWN skill command — like
                        # get_skill_commands() itself, it has no per-platform
                        # view. Re-check every stacked skill (not just the
                        # leading one above) against the same disabled list,
                        # or a skill an operator disabled for this platform
                        # still gets its full content loaded via the stack.
                        from agent.skill_utils import get_disabled_skill_names as _get_plat_disabled
                        _plat_disabled = _get_plat_disabled(platform=_plat)
                        _disabled_extra = [
                            skill_cmds.get(k, {}).get("name", "")
                            for k in extra_keys
                            if skill_cmds.get(k, {}).get("name", "") in _plat_disabled
                        ]
                        if _disabled_extra:
                            return (
                                f"The **{', '.join(_disabled_extra)}** skill(s) in this "
                                f"stacked invocation are disabled for {_plat}.\n"
                                f"Enable them with: `hermes skills config`"
                            )
                    if extra_keys and _build_stacked is not None:
                        stacked_result = _build_stacked(
                            [cmd_key, *extra_keys],
                            stacked_instruction,
                            task_id=_quick_key,
                        )
                        if stacked_result:
                            msg, _loaded, _missing = stacked_result
                            event.text = msg
                            # Fall through to normal message processing
                        else:
                            return f"Failed to load stacked skills for /{command}."
                    else:
                        msg = build_skill_invocation_message(
                            cmd_key, user_instruction, task_id=_quick_key
                        )
                        if msg:
                            event.text = msg
                            # Fall through to normal message processing with skill content
                else:
                    # Not an active skill — check if it's a known-but-disabled or
                    # uninstalled skill and give actionable guidance.
                    _unavail_msg = _check_unavailable_skill(command)
                    if _unavail_msg:
                        return _unavail_msg
                    # Genuinely unrecognized /command: not a built-in, not a
                    # plugin, not a skill, not a known-inactive skill. Warn
                    # the user instead of silently forwarding it to the LLM
                    # as free text (which leads to silent-failure behavior
                    # like the model inventing a delegate_task call).
                    # Normalize to hyphenated form before checking known
                    # built-ins (command may be an alias target set by the
                    # quick-command block above, so _cmd_def can be stale).
                    if command.replace("_", "-") not in GATEWAY_KNOWN_COMMANDS:
                        logger.warning(
                            "Unrecognized slash command /%s from %s — "
                            "replying with unknown-command notice",
                            command,
                            source.platform.value if source.platform else "?",
                        )
                        return (
                            f"Unknown command `/{command}`. "
                            f"Type /commands to see what's available, "
                            f"or resend without the leading slash to send "
                            f"as a regular message."
                        )
            except Exception as e:
                logger.debug("Skill command check failed (non-fatal): %s", e)
        
        # Pending exec approvals are handled by /approve and /deny commands above.
        # No bare text matching — "yes" in normal conversation must not trigger
        # execution of a dangerous command.

        if not is_internal and await asyncio.to_thread(
            self._is_telegram_topic_root_lobby, source
        ):
            # Debounce the lobby reminder so a user who forgets about
            # topic mode and fires ten prompts doesn't get ten copies.
            if self._should_send_telegram_lobby_reminder(source):
                return self._telegram_topic_root_lobby_message()
            return None

        # ── External-drain new-turn gate (Phase 2) ────────────────────
        # When NAS has engaged an external drain (.drain_request.json present,
        # observed by _drain_control_watcher), refuse to START a new turn so
        # the in-flight set can only fall to zero — eliminating the TOCTOU race
        # (D4a: stop accepting new turns FIRST, then NAS polls until
        # active_agents==0). In-flight turns are untouched; this only blocks the
        # claim of a NEW session slot. Internal/system events (restart-recovery
        # replays, background-process completions) bypass the gate — they are
        # not user-initiated new work and must still flow during a drain.
        # Reversible: once the marker is removed the gate opens again.
        if self._external_drain_active and not is_internal:
            logger.info(
                "Refusing new turn for session %s — external drain active.",
                _quick_key,
            )
            return (
                "⏳ This agent is draining for a maintenance action and isn't "
                "accepting new turns right now. It'll be back in a moment — "
                "please resend shortly."
            )

        # ── Claim this session before any await ───────────────────────
        # Between here and _run_agent registering the real AIAgent, there
        # are numerous await points (hooks, vision enrichment, STT,
        # session hygiene compression).  Without this sentinel a second
        # message arriving during any of those yields would pass the
        # "already running" guard and spin up a duplicate agent for the
        # same session — corrupting the transcript.
        _active_session_lease, _limit_message = self._claim_active_session_slot(
            _quick_key,
            source,
        )
        if _limit_message is not None:
            logger.info(
                "Rejecting new active session %s: max_concurrent_sessions reached",
                _quick_key,
            )
            return _limit_message
        _claim_state = self._session_state(_quick_key)
        if _active_session_lease is not None:
            _claim_state.turn.lease = _active_session_lease
        _claim_state.turn.agent = _AGENT_PENDING_SENTINEL
        _claim_state.turn.started_ts = time.time()
        self._persist_active_agents()
        _run_generation = self._begin_session_run_generation(_quick_key)

        try:
            try:
                _agent_result = await self._handle_message_with_agent(
                    event, source, _quick_key, _run_generation
                )
            except TurnLeaseTimeoutError as exc:
                # This is a rejected message, not a completed agent turn. Return
                # before the /goal judge below so it cannot consume the resend
                # notice and enqueue a synthetic continuation loop.
                logger.error(
                    "Rejecting turn for routing key %s on session %s after "
                    "turn-lease timeout; transcript load was not started and "
                    "the user must resend",
                    _quick_key,
                    exc.session_id,
                )
                return (
                    "⏳ Another turn is still running on this session. To "
                    "protect the transcript, this message was not processed. "
                    "Wait for the active turn to finish, then resend it."
                )
            # Goal continuation: after the agent returns a final response
            # for this turn, check any standing /goal — the judge will
            # either mark it done, pause it (budget), or enqueue a
            # continuation prompt back through the adapter FIFO so the
            # next turn makes more progress. Wrapped in try/except so a
            # broken judge never breaks normal message handling.
            try:
                _final_text = ""
                if isinstance(_agent_result, dict):
                    _final_text = str(_agent_result.get("final_response") or "")
                elif isinstance(_agent_result, str):
                    _final_text = _agent_result
                # Skip for empty responses (interrupted / errored) — the
                # judge would almost always say "continue" and we'd loop
                # on error. Let the user drive the next turn.
                if _final_text.strip():
                    try:
                        session_entry = await self.async_session_store.get_or_create_session(
                            source,
                            touch_activity=not is_internal,
                        )
                    except Exception:
                        session_entry = None
                    if session_entry is not None:
                        await self._post_turn_goal_continuation(
                            session_entry=session_entry,
                            source=source,
                            final_response=_final_text,
                        )
                        # /loop tick completion: if this turn was a loop
                        # wakeup, evaluate it (LOOP_COMPLETE marker, --until
                        # judge, caps) and schedule the next tick.
                        await self._post_turn_loop_completion(
                            session_entry=session_entry,
                            source=source,
                            final_response=_final_text,
                        )
            except Exception as _goal_exc:
                logger.debug("goal continuation hook failed: %s", _goal_exc)
            return _agent_result
        finally:
            # MoA one-shot restore must run on EVERY exit path, not just
            # success. The restore data lives on the per-turn event object
            # (_moa_restore_override), which is discarded once the event goes
            # out of scope — so if _handle_message_with_agent raises, a restore
            # in the try block would be skipped and the MoA override would leak
            # permanently (every later message silently fans out through MoA).
            # Putting it in finally guarantees the revert on success, exception,
            # and interrupt alike.
            self._restore_moa_one_shot(event, _quick_key)
            self._restore_pending_one_turn_model_override(_quick_key)
            # Normal completion/exception/interrupt owns and clears this exact
            # durable marker.  SIGKILL/OOM skips finally, leaving the marker for
            # the next unclean startup's recovery pass.
            await self._clear_durable_active_turn(event)
            # Unconditional release covers every exit path. _release_running_agent_state
            # is idempotent (pop-on-absent is harmless) and, called without a
            # run_generation guard, always clears the slot regardless of which
            # generation it holds. This evicts the zombie left when session_reset
            # bumps the generation (N -> N+1) mid-flight: gen-N's guarded release
            # inside _run_agent returns False, and the old sentinel-only check here
            # missed the leftover real agent — locking the session out forever (#28686).
            self._release_running_agent_state(_quick_key)
            # Turn lease (#64934): release THIS turn's lease token — keyed by
            # (routing key, run generation) so this unwind can only ever free
            # the lease its own turn acquired, never a newer turn's.
            self._release_turn_lease(_quick_key, _run_generation)

    def _restore_moa_one_shot(self, event: "MessageEvent", quick_key: str) -> None:
        """Revert a ``/moa <prompt>`` one-shot model override after its turn.

        Called from the ``finally`` of the message-handling path so the revert
        fires whether the turn succeeded, raised, or was interrupted. A no-op
        unless ``event._moa_disable_after_turn`` is set. ``_moa_restore_override``
        carries the prior per-session override (``None`` means the user had no
        override, so the MoA override is cleared outright).
        """
        if not getattr(event, "_moa_disable_after_turn", False):
            return
        try:
            _restore = getattr(event, "_moa_restore_override", None)
            self._session_state(quick_key).conversation.model_override = _restore
            self._evict_cached_agent(quick_key)
        except Exception:
            pass

    def _restore_pending_one_turn_model_override(self, session_key: str) -> None:
        """Restore a per-session model override after ``/model --once`` runs."""
        from gateway.run import logger
        if not session_key:
            return
        try:
            _otr_state = self._peek_session_state(session_key)
            snapshot = _otr_state.conversation.one_turn_restore if _otr_state else None
            if _otr_state is not None:
                _otr_state.conversation.one_turn_restore = None
            if not snapshot:
                return
            self._restore_session_model_override(session_key, snapshot)
        except Exception:
            logger.debug("Failed to restore one-turn model override", exc_info=True)

    async def _prepare_inbound_message_text(
        self,
        *,
        event: MessageEvent,
        source: SessionSource,
        history: List[Dict[str, Any]],
        session_key: Optional[str] = None,
    ) -> Optional[str]:
        """Prepare inbound event text for the agent.

        Keep the normal inbound path and the queued follow-up path on the same
        preprocessing pipeline so sender attribution, image enrichment, STT,
        document notes, reply context, and @ references all behave the same.

        Side effect: buffers per-session native image paths when the active
        model supports native vision AND the user has images attached. The
        caller consumes and clears that session-scoped buffer at the
        ``run_conversation`` site to build a multimodal user turn. When the
        list is empty, the ``_enrich_message_with_vision`` text path has
        already run and images are represented in-text.
        """
        from gateway.run import _build_document_context_note, _event_media_is_audio, _event_media_is_image, _event_media_is_stt_input, _event_media_is_video, _load_gateway_config, logger
        history = history or []
        _pending_stt_prepared = hasattr(event, "_gateway_pending_stt_text")
        message_text = (
            getattr(event, "_gateway_pending_stt_text", None)
            if _pending_stt_prepared
            else event.text
        ) or ""
        _group_sessions_per_user = getattr(self.config, "group_sessions_per_user", True)
        _thread_sessions_per_user = getattr(self.config, "thread_sessions_per_user", False)
        # Prefer the already resolved session key from the caller so this write
        # key matches the consume key at the run_conversation site. Fall back
        # to deriving it here for tests and legacy standalone callers.
        session_key = session_key or self._session_key_for_source(source)
        # Reset only this session's per-call buffer; other sessions may be
        # concurrently preparing multimodal turns on the same runner.
        self._consume_pending_native_image_paths(session_key)

        _is_shared_multi_user = is_shared_multi_user_session(
            source,
            group_sessions_per_user=_group_sessions_per_user,
            thread_sessions_per_user=_thread_sessions_per_user,
        )
        if _is_shared_multi_user and source.user_name:
            # source.user_name is the platform display name — attacker-
            # influenceable on any platform that lets participants set their
            # own name. Neutralize embedded newlines/control chars before
            # interpolating it into every message in the shared session, or
            # a hostile name can masquerade as a fake markdown section
            # (mirrors the same field's treatment in
            # build_session_context_prompt via _format_untrusted_prompt_value).
            _safe_user_name = neutralize_untrusted_inline_text(source.user_name)
            # On Slack, expose the current author's verifiable user ID next to
            # the display name (#17916): "mention me again" requests need a
            # trusted `<@U...>` target for the CURRENT speaker — display names
            # are ambiguous and historical mentions may point at someone else.
            # The user_id comes from the Slack event envelope (not
            # user-editable text), so it does not need neutralization.
            if source.platform == Platform.SLACK and source.user_id:
                _safe_user_name = (
                    f"{_safe_user_name} | Slack user <@{source.user_id}>"
                )
            message_text = f"[{_safe_user_name}] {message_text}"

        # Prepend channel context from history backfill (if any).  This
        # happens after sender-prefix so the prefix only applies to the
        # trigger message, not the backfill block.
        if getattr(event, "channel_context", None):
            message_text = f"{event.channel_context}\n\n[New message]\n{message_text}"

        # Declare at outer scope so the audio-file-paths handling block below
        # remains safe when ``event.media_urls`` is empty (no inner block runs).
        audio_file_paths: list[str] = []
        video_paths: list[str] = []

        if event.media_urls:
            image_paths = []
            audio_paths = []
            for i, path in enumerate(event.media_urls):
                mtype = event.media_types[i] if i < len(event.media_types) else ""
                # Classify images per-attachment: trust this attachment's own
                # MIME, and only honour the message-level PHOTO type when the
                # per-attachment MIME is unknown. Otherwise a document (or any
                # non-image) sent alongside an image in the same message gets
                # mis-routed here as an image and the provider 400s.
                if _event_media_is_image(event, i):
                    image_paths.append(path)
                # MessageType.AUDIO = audio file attachment (e.g. .mp3, .m4a) — never STT
                # MessageType.VOICE = voice message (Opus/OGG) — always STT
                if event.message_type == MessageType.AUDIO:
                    audio_file_paths.append(path)
                elif not _pending_stt_prepared and _event_media_is_stt_input(event, i):
                    audio_paths.append(path)
                if mtype.startswith("video/") or (not mtype and event.message_type == MessageType.VIDEO):
                    video_paths.append(path)

            if image_paths:
                # Decide routing: native (attach pixels) vs text (vision_analyze
                # pre-run + prepend description).  See agent/image_routing.py.
                # Offload to a worker thread: the decision does blocking network
                # I/O — a models.dev fetch on cache miss, and the Ollama
                # ``/api/show`` capability probe for local servers — whose
                # request timeout would otherwise stall the whole gateway event
                # loop (every session) while a single image is routed.
                _img_mode = await asyncio.to_thread(
                    self._decide_image_input_mode,
                    source=source,
                    session_key=session_key,
                )
                if _img_mode == "native":
                    # Defer attachment to the run_conversation call site.
                    self._session_state(
                        session_key
                    ).persistent.native_image_paths = list(image_paths)
                    logger.info(
                        "Image routing: native (model supports vision). %d image(s) will be attached inline.",
                        len(image_paths),
                    )
                else:
                    logger.info(
                        "Image routing: text (mode=%s). Pre-analyzing %d image(s) via vision_analyze.",
                        _img_mode, len(image_paths),
                    )
                    # Vision enrichment runs before AIAgent.run_conversation(),
                    # so bind this session's resolved runtime explicitly rather
                    # than consulting process-global compatibility mirrors.
                    vision_runtime = None
                    try:
                        turn_model, runtime_kwargs = self._resolve_session_agent_runtime(
                            source=source,
                            session_key=session_key,
                        )
                        vision_runtime = dict(runtime_kwargs or {})
                        vision_runtime["model"] = turn_model
                    except Exception:
                        logger.debug(
                            "vision enrichment: session runtime resolution failed",
                            exc_info=True,
                        )

                    from agent.auxiliary_client import scoped_runtime_main

                    with scoped_runtime_main(vision_runtime):
                        message_text = await self._enrich_message_with_vision(
                            message_text,
                            image_paths,
                        )

            if audio_paths:
                message_text, _successful_transcripts = await self._enrich_message_with_transcription(
                    message_text,
                    audio_paths,
                )
                # Echo each successful transcript back to the user immediately
                # when configured. Lets users verify STT quality in real-time,
                # while allowing quiet STT for users who only want the agent to
                # receive the transcription.
                if _successful_transcripts and self._should_echo_stt_transcripts():
                    _echo_adapter = self._adapter_for_source(source)
                    _echo_meta = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))
                    if _echo_adapter:
                        for _tx in _successful_transcripts:
                            try:
                                await _echo_adapter.send(
                                    source.chat_id,
                                    f'🎙️ "{_tx}"',
                                    metadata=_echo_meta,
                                )
                            except Exception as _echo_exc:
                                logger.debug(
                                    "Transcript echo failed (non-fatal): %s", _echo_exc,
                                )
                # NOTE: Previously, when transcription failed (e.g. no STT
                # provider configured), the gateway also emitted a hardcoded
                # English notice via `_stt_adapter.send()`. That bypassed the
                # LLM and produced two replies — one pre-canned English clip
                # (which TTS then spoke aloud, in the wrong language) and one
                # correct, localized LLM reply from the enriched message text.
                # The enrichment step now leaves a single neutral marker in the
                # prompt, so the LLM produces one coherent reply in the user's
                # language. The hardcoded send has therefore been removed.

        if audio_file_paths:
            from tools.credential_files import to_agent_visible_cache_path as _to_agent_path
            for _apath in audio_file_paths:
                _basename = os.path.basename(_apath)
                _parts = _basename.split("_", 2)
                _display = _parts[2] if len(_parts) >= 3 else _basename
                _display = re.sub(r'[^\w.\- ]', '_', _display)
                _agent_path = _to_agent_path(_apath)
                _note = (
                    f"[The user sent an audio file attachment: '{_display}'. "
                    f"It is saved at: {_agent_path}. "
                    f"Its content is not inlined here. If the user's request involves "
                    f"what the audio contains, transcribe or process it yourself — for "
                    f"example by passing the path to a transcription or media tool — "
                    f"instead of asking the user to describe it. Only ask what to do "
                    f"with it if their intent is genuinely unclear.]"
                )
                message_text = f"{_note}\n\n{message_text}"

        if video_paths:
            from tools.credential_files import to_agent_visible_cache_path as _to_agent_path
            for _vpath in video_paths:
                _basename = os.path.basename(_vpath)
                _parts = _basename.split("_", 2)
                _display = _parts[2] if len(_parts) >= 3 else _basename
                _display = re.sub(r'[^\w.\- ]', '_', _display)
                _agent_path = _to_agent_path(_vpath)
                _note = (
                    f"[The user sent a video attachment: '{_display}'. "
                    f"It is saved at: {_agent_path}. "
                    f"Its content is not inlined here. If the user's request involves "
                    f"what the video contains, inspect or process it yourself — for "
                    f"example by passing the path to a video analysis or media tool — "
                    f"instead of asking the user to describe it. Only ask what to do "
                    f"with it if their intent is genuinely unclear.]"
                )
                message_text = f"{_note}\n\n{message_text}"

        if event.media_urls:
            import mimetypes as _mimetypes
            from tools.credential_files import to_agent_visible_cache_path

            _TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
            for i, path in enumerate(event.media_urls):
                # Per-attachment document handling. Skip anything already routed
                # as image / audio / video by the buckets above — only genuine
                # non-media files get a path-pointing context note. This makes a
                # document mixed into a PHOTO/VOICE message (whole-message type
                # != DOCUMENT) still reach the agent as a readable cached file,
                # instead of being silently dropped because the message-level
                # type wasn't DOCUMENT.
                if (
                    _event_media_is_image(event, i)
                    or _event_media_is_audio(event, i)
                    or _event_media_is_video(event, i)
                ):
                    continue
                mtype = event.media_types[i] if i < len(event.media_types) else ""
                if mtype in {"", "application/octet-stream"}:
                    _ext = os.path.splitext(path)[1].lower()
                    if _ext in _TEXT_EXTENSIONS:
                        mtype = "text/plain"
                    else:
                        guessed, _ = _mimetypes.guess_type(path)
                        if guessed:
                            mtype = guessed
                        else:
                            mtype = "application/octet-stream"
                # Any accepted file gets a path-pointing context note — we accept
                # all file types now, so a non-text/non-application MIME (font/*,
                # model/*, etc.) must still tell the agent the file exists.

                basename = os.path.basename(path)
                parts = basename.split("_", 2)
                display_name = parts[2] if len(parts) >= 3 else basename
                display_name = re.sub(r'[^\w.\- ]', '_', display_name)

                # Translate host cache path to in-container path if running under Docker backend.
                # This ensures the agent receives a path it can open inside its sandbox, as the
                # cache directories are auto-mounted at /root/.hermes/cache/* by get_cache_directory_mounts().
                agent_path = to_agent_visible_cache_path(path)

                context_note = _build_document_context_note(display_name, agent_path, mtype)
                message_text = f"{context_note}\n\n{message_text}"

        # Discord: surface the triggering message id per-turn on the user
        # message rather than in the cached system prompt. message_id changes
        # every turn, so baking it into build_session_context_prompt() would
        # bust the agent-cache signature and rebuild the AIAgent every message
        # (destroying prompt caching). The static IDs block points the agent
        # here; the volatile id rides the per-turn user content.
        if (
            source is not None
            and getattr(source, "platform", None) == Platform.DISCORD
            and getattr(event, "message_id", None)
        ):
            from gateway.session import _discord_tools_loaded as _disc_tools_loaded
            if _disc_tools_loaded():
                message_text = (
                    f"[Triggering message id: `{event.message_id}` — use as "
                    f"`message_id` for reply/react/pin via the discord tools.]\n\n"
                    f"{message_text}"
                )

        if getattr(event, "reply_to_text", None) and event.reply_to_message_id:
            # Always inject the reply-to pointer — even when the quoted text
            # already appears in history. The prefix isn't deduplication, it's
            # disambiguation: it tells the agent *which* prior message the user
            # is referencing. History can contain the same or similar text
            # multiple times, and without an explicit pointer the agent has to
            # guess (or answer for both subjects). Token overhead is minimal.
            reply_snippet = event.reply_to_text[:500]
            if getattr(event, "reply_to_is_own_message", False):
                message_text = (
                    f'[Replying to your previous message: "{reply_snippet}"]\n\n'
                    f"{message_text}"
                )
            else:
                message_text = f'[Replying to: "{reply_snippet}"]\n\n{message_text}'

        if "@" in message_text:
            try:
                from agent.context_references import preprocess_context_references_async
                from agent.model_metadata import get_model_context_length_async

                _msg_cwd = os.environ.get("TERMINAL_CWD", os.path.expanduser("~"))
                _msg_config_ctx = None
                _msg_cfg = None
                _msg_model_cfg = {}
                _msg_custom_providers = []
                try:
                    _msg_cfg = _load_gateway_config()
                    _msg_model_cfg = _msg_cfg.get("model", {})
                    if isinstance(_msg_model_cfg, dict):
                        _msg_raw_ctx = _msg_model_cfg.get("context_length")
                        if _msg_raw_ctx is not None:
                            _msg_config_ctx = int(_msg_raw_ctx)
                    try:
                        from hermes_cli.config import get_compatible_custom_providers

                        _msg_custom_providers = get_compatible_custom_providers(_msg_cfg)
                    except Exception:
                        _msg_custom_providers = _msg_cfg.get("custom_providers") or []
                except Exception:
                    pass
                # Resolve the session's actual model/provider/base_url the
                # same way the hygiene compression block does (~11080).
                # GatewayRunner has no self._model/self._base_url attrs
                # (that was copy-pasted from HermesCLI, which does carry
                # self.model/self.base_url), so using them here always raised
                # AttributeError, silently caught below, meaning this feature
                # never ran.
                _msg_model, _msg_runtime = self._resolve_session_agent_runtime(
                    source=source,
                    session_key=session_key,
                    user_config=_msg_cfg,
                )
                _msg_base_url = _msg_runtime.get("base_url") or ""
                # A global model.context_length belongs to the configured
                # model, not a session /model or channel override. Prefer a
                # matching per-custom-provider model limit when available.
                _msg_configured_model = (
                    _msg_model_cfg.get("default") or _msg_model_cfg.get("model")
                    if isinstance(_msg_model_cfg, dict)
                    else _msg_model_cfg
                )
                if _msg_model != _msg_configured_model:
                    _msg_config_ctx = None
                if _msg_config_ctx is not None and isinstance(_msg_model_cfg, dict):
                    try:
                        from hermes_cli.route_identity import should_clear_context_pin_async

                        if await should_clear_context_pin_async(
                            None,  # model match already checked above
                            None,
                            _msg_model_cfg.get("base_url"),
                            _msg_base_url,
                            _msg_model_cfg.get("provider"),
                            _msg_runtime.get("provider"),
                        ):
                            _msg_config_ctx = None
                    except Exception:
                        _msg_config_ctx = None
                if _msg_custom_providers and _msg_base_url:
                    try:
                        from hermes_cli.config import get_custom_provider_context_length

                        _msg_custom_ctx = get_custom_provider_context_length(
                            model=_msg_model,
                            base_url=_msg_base_url,
                            custom_providers=_msg_custom_providers,
                        )
                        if _msg_custom_ctx:
                            _msg_config_ctx = _msg_custom_ctx
                    except Exception:
                        pass
                _msg_ctx_len = await get_model_context_length_async(
                    _msg_model,
                    base_url=_msg_base_url,
                    api_key=_msg_runtime.get("api_key") or "",
                    config_context_length=_msg_config_ctx,
                    provider=_msg_runtime.get("provider") or "",
                    custom_providers=_msg_custom_providers,
                )
                _ctx_result = await preprocess_context_references_async(
                    message_text,
                    cwd=_msg_cwd,
                    context_length=_msg_ctx_len,
                    allowed_root=_msg_cwd,
                )
                if _ctx_result.blocked:
                    _adapter = self._adapter_for_source(source)
                    if _adapter:
                        await _adapter.send(
                            source.chat_id,
                            "\n".join(_ctx_result.warnings) or "Context injection refused.",
                        )
                    return None
                if _ctx_result.expanded:
                    message_text = _ctx_result.message
            except Exception as exc:
                logger.warning("@ context reference expansion failed: %s", exc)
                logger.debug("@ context reference expansion failure detail", exc_info=True)

        return message_text

    async def _prepare_profile_scoped_inbound_message_text(
        self,
        *,
        event: MessageEvent,
        source: SessionSource,
        history: List[Dict[str, Any]],
        session_key: Optional[str] = None,
    ) -> Optional[str]:
        """Run inbound preprocessing under the routed profile when multiplexed."""
        from gateway.run import _profile_runtime_scope
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            with _profile_runtime_scope(self._resolve_profile_home_for_source(source)):
                return await self._prepare_inbound_message_text(
                    event=event,
                    source=source,
                    history=history,
                    session_key=session_key,
                )
        return await self._prepare_inbound_message_text(
            event=event,
            source=source,
            history=history,
            session_key=session_key,
        )

    async def _prepare_clarify_reply_text(self, event) -> str:
        """Return raw text or successful voice transcripts for a clarify reply."""
        if not self._pending_event_audio_paths(event):
            return (event.text or "").strip()

        _, successful_transcripts = await self._transcribe_pending_audio_event_once(
            event, "",
        )
        return "\n\n".join(
            transcript.strip()
            for transcript in successful_transcripts
            if transcript.strip()
        )

    def _consume_pending_native_image_paths(self, session_key: str) -> List[str]:
        state = self._peek_session_state(session_key)
        if state is None or not state.persistent.native_image_paths:
            return []
        paths = list(state.persistent.native_image_paths)
        state.persistent.native_image_paths = []
        return paths

    def _cache_session_source(self, session_key: str, source) -> None:
        from gateway.run import logger
        if not session_key or source is None:
            return
        cached_sources = getattr(self, "_session_sources", None)
        if cached_sources is None:
            cached_sources = OrderedDict()
            self._session_sources = cached_sources
        try:
            cached_sources[session_key] = dataclasses.replace(source)
        except Exception:
            logger.debug("Failed to cache live session source for %s", session_key, exc_info=True)
            return
        # LRU: mark as most-recently-used and trim to max size.
        try:
            cached_sources.move_to_end(session_key)
            max_size = getattr(self, "_session_sources_max", 512)
            while len(cached_sources) > max_size:
                cached_sources.popitem(last=False)
        except Exception:
            pass

    @property
    def async_session_store(self) -> AsyncSessionStore:
        """Return the single async facade for this runner's SessionStore."""
        facade = getattr(self, "_async_session_store", None)
        if facade is None or facade._store is not self.session_store:
            facade = AsyncSessionStore(self.session_store)
            self._async_session_store = facade
        return facade

    async def _mark_durable_active_turn(
        self,
        event: "MessageEvent",
        session_key: str,
    ) -> bool:
        """Persist the exact resolved routing key for this running turn."""
        from gateway.run import logger
        try:
            token = await self.async_session_store.mark_turn_active(session_key)
        except Exception as exc:
            logger.warning(
                "Could not persist active-turn marker for %s: %s",
                session_key,
                exc,
            )
            return False
        if not token:
            return False
        # Private event attributes are process-local ownership state.  Keep the
        # token out of public metadata, transcripts, and platform payloads.
        setattr(event, "_gateway_active_turn_session_key", session_key)
        setattr(event, "_gateway_active_turn_token", token)
        return True

    async def _clear_durable_active_turn(self, event: "MessageEvent") -> bool:
        """Best-effort CAS clear of the marker owned by *event*."""
        from gateway.run import logger
        session_key = getattr(event, "_gateway_active_turn_session_key", None)
        token = getattr(event, "_gateway_active_turn_token", None)
        try:
            if not session_key or not token:
                return False
            last_error: Optional[Exception] = None
            for attempt in range(1, 4):
                try:
                    return bool(
                        await self.async_session_store.clear_turn_active(
                            session_key, token
                        )
                    )
                except Exception as exc:
                    last_error = exc
                    if attempt < 3:
                        logger.debug(
                            "Retrying active-turn marker cleanup for %s (%d/3): %s",
                            session_key,
                            attempt,
                            exc,
                        )
            # Never let marker cleanup block in-memory agent/lease release.  A
            # stale marker is bounded by the configured agent timeout and the
            # clean-start orphan-marker discard path.
            logger.warning(
                "Could not clear active-turn marker for %s after 3 attempts: %s",
                session_key,
                last_error,
            )
            return False
        finally:
            for attr in (
                "_gateway_active_turn_session_key",
                "_gateway_active_turn_token",
            ):
                try:
                    delattr(event, attr)
                except AttributeError:
                    pass

    def _install_plugin_message_injector(self) -> None:
        """Publish this live gateway's plugin message scheduler."""
        from hermes_cli.plugins import get_plugin_manager

        get_plugin_manager().set_gateway_message_injector(
            self,
            self._schedule_plugin_message_injection,
        )

    def _clear_plugin_message_injector(self) -> None:
        """Remove this runner's scheduler without clobbering a newer owner."""
        from hermes_cli.plugins import get_plugin_manager

        get_plugin_manager().clear_gateway_message_injector(self)

    def _schedule_plugin_message_injection(
        self,
        *,
        session_key: str,
        content: str,
        plugin_id: str,
    ) -> bool:
        """Schedule a plugin-triggered turn on the live gateway loop."""
        from gateway.run import logger
        loop = getattr(self, "_gateway_loop", None)
        if not getattr(self, "_running", False) or loop is None or loop.is_closed():
            return False

        coro = self._dispatch_plugin_message_injection(
            session_key=session_key,
            content=content,
            plugin_id=plugin_id,
        )
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is loop:
            try:
                future = loop.create_task(coro)
            except Exception:
                coro.close()
                logger.warning(
                    "Plugin message injection scheduling failed",
                    exc_info=True,
                )
                return False
            self._background_tasks.add(future)
            future.add_done_callback(self._background_tasks.discard)
        else:
            future = safe_schedule_threadsafe(
                coro,
                loop,
                logger=logger,
                log_message="Plugin message injection scheduling failed",
                log_level=logging.WARNING,
            )
            if future is None:
                return False

        def _log_result(completed) -> None:
            try:
                accepted = completed.result()
            except (asyncio.CancelledError, concurrent.futures.CancelledError):
                return
            except Exception:
                logger.warning(
                    "Plugin message injection failed: plugin=%s session=%s",
                    plugin_id,
                    session_key,
                    exc_info=True,
                )
                return
            if not accepted:
                logger.warning(
                    "Plugin message injection was not routed: plugin=%s session=%s",
                    plugin_id,
                    session_key,
                )

        future.add_done_callback(_log_result)
        return True

    async def _dispatch_plugin_message_injection(
        self,
        *,
        session_key: str,
        content: str,
        plugin_id: str,
    ) -> bool:
        """Route a plugin-triggered turn through the session's live adapter."""
        from gateway.run import logger
        if not getattr(self, "_running", False) or getattr(self, "_draining", False):
            return False

        entry = await self.async_session_store.lookup_by_session_key(session_key)
        if entry is None or entry.origin is None:
            return False
        if not getattr(self, "_running", False) or getattr(self, "_draining", False):
            return False

        source = dataclasses.replace(entry.origin)
        try:
            if not self._is_user_authorized(
                source,
                allow_adapter_delegation=False,
            ):
                logger.warning(
                    "Plugin message injection denied by current gateway authorization: "
                    "plugin=%s session=%s",
                    plugin_id,
                    session_key,
                )
                return False
        except Exception:
            logger.warning(
                "Plugin message injection authorization check failed: "
                "plugin=%s session=%s",
                plugin_id,
                session_key,
                exc_info=True,
            )
            return False

        adapter = self._adapter_for_source(source)
        if adapter is None:
            return False

        event = MessageEvent(
            text=content,
            message_type=MessageType.TEXT,
            source=source,
            internal=True,
            allow_gateway_control=False,
            metadata={
                "hermes_plugin_id": plugin_id,
                "hermes_plugin_injection": True,
                "gateway_session_key": session_key,
                "gateway_session_id": entry.session_id,
                "gateway_session_strict": True,
            },
        )
        await adapter.handle_message(event)
        logger.info(
            "Plugin message injection dispatched: plugin=%s session=%s session_id=%s",
            plugin_id,
            session_key,
            entry.session_id,
        )
        return True

    def _get_cached_session_source(self, session_key: str):
        if not session_key:
            return None
        cached_sources = getattr(self, "_session_sources", None)
        if not cached_sources:
            return None
        source = cached_sources.get(session_key)
        if source is not None:
            try:
                cached_sources.move_to_end(session_key)
            except Exception:
                pass
        return source

    async def _handle_message_with_agent(self, event, source, _quick_key: str, run_generation: int):
        """Inner handler that runs under the _running_agents sentinel guard."""
        from gateway.run import _GATEWAY_HYGIENE_PLATFORM, _float_env, _hermes_home, _home_target_env_var, _hygiene_cooldown_for_failure, _is_gateway_hidden_reasoning_incomplete_turn, _load_gateway_config, _message_timestamps_enabled, _normalize_empty_agent_response, _platform_config_key, _record_hygiene_cooldown, _reset_hygiene_failure_streak, _resolve_gateway_display_bool, _resolve_gateway_model, _sanitize_gateway_final_response, _seed_hygiene_system_prompt, _should_clear_resume_pending_after_turn, _stamp_hygiene_compression_provenance, hygiene_compaction_recovered, logger
        _msg_start_time = time.time()
        _platform_name = source.platform.value if hasattr(source.platform, "value") else str(source.platform)
        _msg_preview = (event.text or "")[:80].replace("\n", " ")
        _reply_id = getattr(event, "reply_to_message_id", None)
        _reply_txt = (getattr(event, "reply_to_text", None) or "")[:80].replace("\n", " ")
        logger.info(
            "inbound message: platform=%s user=%s chat=%s msg=%r reply_to_id=%s reply_to_text=%r",
            _platform_name, source.user_name or source.user_id or "unknown",
            source.chat_id or "unknown", _msg_preview, _reply_id, _reply_txt,
        )

        # Get or create session
        # Topic-mode DMs: rewrite a stale/foreign thread_id to the user's
        # last-active topic so a cross-topic Reply or stripped plain reply
        # doesn't fragment the conversation across sessions.
        recovered = await asyncio.to_thread(self._recover_telegram_topic_thread_id, source)
        if recovered is not None:
            logger.info(
                "telegram topic recovery: chat=%s user=%s %r -> %s",
                source.chat_id, source.user_id, source.thread_id, recovered,
            )
            source = dataclasses.replace(source, thread_id=recovered)
            try:
                event.source = source
            except Exception:
                pass

        event_metadata = getattr(event, "metadata", None) or {}
        expected_session_key = str(
            event_metadata.get("gateway_session_key") or ""
        ).strip()
        if expected_session_key:
            derived_session_key = self._session_key_for_source(source)
            if derived_session_key != expected_session_key:
                logger.warning(
                    "Dropping internally routed event after route recovery: "
                    "expected session=%s derived=%s",
                    expected_session_key,
                    derived_session_key,
                )
                return

        strict_session = bool(event_metadata.get("gateway_session_strict"))
        pinned_session_id = str(event_metadata.get("gateway_session_id") or "").strip()
        if strict_session:
            session_entry = await self.async_session_store.lookup_by_session_key(
                expected_session_key
            )
            if (
                session_entry is None
                or not pinned_session_id
                or session_entry.session_id != pinned_session_id
            ):
                logger.warning(
                    "Dropping internally routed event: expected session id=%s is no "
                    "longer current for key=%s",
                    pinned_session_id or "missing",
                    expected_session_key or "missing",
                )
                return
        else:
            # Internal wakes must observe reset policy without becoming user
            # activity themselves. Otherwise periodic Kanban/process
            # notifications keep the stable routing key alive across every
            # daily/idle boundary.
            session_entry = await self.async_session_store.get_or_create_session(
                source,
                touch_activity=not bool(getattr(event, "internal", False)),
            )
        session_key = session_entry.session_key
        if not strict_session and pinned_session_id:
            resolved_entry = await self._resolve_async_delegation_session(
                session_entry,
                pinned_session_id,
            )
            if resolved_entry is None:
                return
            session_entry = resolved_entry
        self._cache_session_source(session_key, source)
        if await asyncio.to_thread(self._is_telegram_topic_lane, source):
            try:
                binding = (await self._session_db.get_telegram_topic_binding(
                    chat_id=str(source.chat_id),
                    thread_id=str(source.thread_id),
                )) if self._session_db else None
            except Exception:
                logger.debug("Failed to read Telegram topic binding", exc_info=True)
                binding = None
            if binding:
                bound_session_id = str(binding.get("session_id") or "")
                # Heal bindings that point at a pre-compression parent: walk
                # the compression-continuation chain forward to its tip so the
                # next message resumes the compressed child instead of
                # reloading the oversized parent transcript (#20470/#29712/
                # #33414). Returns the input unchanged when the session isn't
                # a compression parent, so this is cheap and safe.
                if bound_session_id and self._session_db is not None:
                    try:
                        canonical_session_id = await self._session_db.get_compression_tip(
                            bound_session_id,
                        )
                    except Exception:
                        logger.debug(
                            "compression-tip lookup failed for %s",
                            bound_session_id, exc_info=True,
                        )
                        canonical_session_id = bound_session_id
                    if (
                        canonical_session_id
                        and canonical_session_id != bound_session_id
                    ):
                        bound_session_id = canonical_session_id
                if bound_session_id and bound_session_id != session_entry.session_id:
                    # Route the override through SessionStore so the session_key
                    # → session_id mapping is persisted to disk and the previous
                    # lane session is ended cleanly. Mutating session_entry in
                    # place here created a split-brain state where the JSON
                    # index pointed at one id but code downstream used another.
                    switched = await self.async_session_store.switch_session(session_key, bound_session_id)
                    if switched is not None:
                        session_entry = switched
                # If the stored binding pointed at a parent, rewrite it to the
                # canonical descendant now that we've followed the chain.
                if (
                    bound_session_id
                    and bound_session_id != str(binding.get("session_id") or "")
                ):
                    await asyncio.to_thread(
                        self._sync_telegram_topic_binding,
                        source, session_entry, reason="compression-tip-walk",
                    )
            else:
                try:
                    await asyncio.to_thread(self._record_telegram_topic_binding, source, session_entry)
                except Exception:
                    logger.debug("Failed to record Telegram topic binding", exc_info=True)
        # Capture and immediately consume was_auto_reset so it does not
        # re-fire on subsequent messages — preventing the cleanup from
        # wiping model/reasoning overrides set between turns (Closes #48031).
        _was_auto_reset = getattr(session_entry, "was_auto_reset", False)
        if _was_auto_reset:
            # Treat auto-reset as a full conversation boundary — clear every
            # conversation-scoped per-session dict in one funnel call so the
            # fresh session does not inherit the previous conversation's
            # model/reasoning overrides, a queued "/model switched" note, or
            # a stale resolved-model cache (#48031, #58403). See
            # _CONVERSATION_SCOPED_STATE.
            self._clear_conversation_scope(session_key, reason="auto_reset")
            # Evict the cached agent so the fresh session does not inherit the
            # previous conversation's context_compressor._previous_summary —
            # the cache is keyed on the stable session_key, so an auto-reset
            # otherwise reuses the old agent and leaks prior history into new
            # compaction summaries. Mirrors /reset and the compression-exhausted
            # path (#9893). Covers daily/idle/suspended auto-reset.
            self._evict_cached_agent(session_key)
            session_entry.was_auto_reset = False
        
        # Emit session:start for new or auto-reset sessions
        _is_new_session = (
            session_entry.created_at == session_entry.updated_at
            or _was_auto_reset
            or getattr(session_entry, "is_fresh_reset", False)
        )
        # Consume the is_fresh_reset flag immediately so it doesn't leak
        # onto subsequent messages in the same session (issue #6508).
        if getattr(session_entry, "is_fresh_reset", False):
            session_entry.is_fresh_reset = False
        if _is_new_session:
            await self.hooks.emit("session:start", {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "session_id": session_entry.session_id,
                "session_key": session_key,
            })
        
        # Build session context
        context = build_session_context(source, self.config, session_entry)
        
        # Set session context variables for tools (task-local, concurrency-safe)
        _session_env_tokens = self._set_session_env(context)
        
        # Read privacy.redact_pii from config (re-read per message)
        _redact_pii = False
        persist_user_message = None
        persist_user_timestamp = None
        # Synthetic self-injected turns (async-delegation batch completions,
        # background watch notifications, resume wake-ups) arrive as
        # MessageEvent(internal=True). Persist their user row typed with
        # display_kind="internal_notification" so transcripts/UIs can render
        # them as timeline notices instead of user bubbles (#82888). Role and
        # content are untouched — display_kind is a DB-only sidecar stripped
        # from every provider-bound payload (see conversation_loop's
        # api_msg.pop("display_kind")).
        persist_user_display_kind = (
            "internal_notification" if getattr(event, "internal", False) else None
        )
        try:
            _pcfg = _load_gateway_config()
            _redact_pii = bool((_pcfg.get("privacy") or {}).get("redact_pii", False))
        except Exception:
            pass

        # Build the context prompt to inject.  The render is pinned per
        # session, keyed by a hash of the exact renderer inputs
        # (_ephemeral_change_key).  A key hit reuses the pinned bytes verbatim
        # so the composed system prompt cannot drift turn-over-turn; a key
        # miss (thread rename, /sethome, redact_pii flip, ...) re-renders
        # once — the only legitimate cache busts.
        context_prompt = self._pinned_session_context_prompt(
            context, _redact_pii, session_key
        )

        # Per-turn must-deliver notes.  These used to be appended to
        # context_prompt (the ephemeral system prompt), which guaranteed a
        # turn1→turn2 system-prompt diff and a full agent rebuild.  They now
        # ride the current user message via the api_content sidecar instead
        # (staged below, consumed in run_sync → build_turn_context).
        turn_sidecar_notes: List[str] = []

        # If the previous session expired and was auto-reset, deliver a notice
        # so the agent knows this is a fresh conversation (not an intentional /reset).
        if _was_auto_reset:
            reset_reason = getattr(session_entry, 'auto_reset_reason', None) or 'idle'
            if reset_reason == "suspended":
                context_note = "[System note: The user's previous session was stopped and suspended. This is a fresh conversation with no prior context.]"
            elif reset_reason == "daily":
                context_note = "[System note: The user's session was automatically reset by the daily schedule. This is a fresh conversation with no prior context.]"
            elif reset_reason == "resume_pending_expired":
                context_note = "[System note: The previous gateway session could not be recovered after a restart (API recovery timed out). This is a fresh conversation — use /resume to restore history if needed.]"
            else:
                context_note = "[System note: The user's previous session expired due to inactivity. This is a fresh conversation with no prior context.]"
            # Slack/Discord channels/threads are long-lived: point the agent at
            # the specific prior same-channel session so it recalls that context
            # via session_search instead of an unrelated recent session.  Returns
            # None (appends nothing) for other platforms or when there's no prior
            # activity to recall.  Deterministic — no extra API/DB calls (#36220).
            try:
                continuity_note = build_channel_continuity_note(session_entry, source)
            except Exception:
                continuity_note = None
            if continuity_note:
                context_note = context_note + "\n\n" + continuity_note
            turn_sidecar_notes.append(context_note)

            # Send a user-facing notification explaining the reset, unless:
            # - notifications are disabled in config
            # - the platform is excluded (e.g. api_server, webhook)
            # - the expired session had no activity (nothing was cleared)
            try:
                policy = self.session_store.config.get_reset_policy(
                    platform=source.platform,
                    session_type=getattr(source, 'chat_type', 'dm'),
                )
                platform_name = source.platform.value if source.platform else ""
                had_activity = getattr(session_entry, 'reset_had_activity', False)
                # Suspended and restart-recovery-expired sessions always notify
                # regardless of policy.notify — the user had an active session
                # that was silently replaced, so they need to know they can
                # /resume it.  Idle/daily resets respect the policy flag.
                should_notify = reset_reason in {"suspended", "resume_pending_expired"} or (
                    policy.notify
                    and had_activity
                    and platform_name not in policy.notify_exclude_platforms
                )
                if should_notify:
                    adapter = self._adapter_for_source(source)
                    if adapter:
                        if reset_reason == "suspended":
                            reason_text = "previous session was stopped or interrupted"
                        elif reset_reason == "resume_pending_expired":
                            reason_text = "gateway restart recovery timed out"
                        elif reset_reason == "daily":
                            reason_text = f"daily schedule at {policy.at_hour}:00"
                        else:
                            hours = policy.idle_minutes // 60
                            mins = policy.idle_minutes % 60
                            duration = f"{hours}h" if not mins else f"{hours}h {mins}m" if hours else f"{mins}m"
                            reason_text = f"inactive for {duration}"
                        notice = (
                            f"◐ Session automatically reset ({reason_text}). "
                            f"Conversation history cleared.\n"
                            f"Use /resume to browse and restore a previous session.\n"
                            f"Adjust reset timing in config.yaml under session_reset."
                        )
                        try:
                            session_info = await asyncio.to_thread(
                                self._reset_notice_session_info, source
                            )
                            if session_info:
                                notice = f"{notice}\n\n{session_info}"
                        except Exception:
                            pass
                        await adapter.send(
                            source.chat_id, notice,
                            metadata=self._thread_metadata_for_source(source),
                        )
            except Exception as e:
                logger.debug("Auto-reset notification failed (non-fatal): %s", e)

            # was_auto_reset is already consumed in the cleanup block above
            # (single source of truth); only the reset reason needs clearing here.
            session_entry.auto_reset_reason = None

        # Auto-load skill(s) for topic/channel bindings (Telegram DM Topics,
        # Discord channel_skill_bindings).  Supports a single name or ordered list.
        # Only inject on NEW sessions — ongoing conversations already have the
        # skill content in their conversation history from the first message.
        _auto = getattr(event, "auto_skill", None)
        if _is_new_session and _auto:
            _skill_names = [_auto] if isinstance(_auto, str) else list(_auto)
            try:
                from agent.skill_commands import _load_skill_payload, _build_skill_message
                _combined_parts: list[str] = []
                _loaded_names: list[str] = []
                for _sname in _skill_names:
                    _loaded = _load_skill_payload(_sname, task_id=_quick_key)
                    if _loaded:
                        _loaded_skill, _skill_dir, _display_name = _loaded
                        _note = (
                            f'[IMPORTANT: The "{_display_name}" skill is auto-loaded. '
                            f"Follow its instructions for this session.]"
                        )
                        _part = _build_skill_message(_loaded_skill, _skill_dir, _note)
                        if _part:
                            _combined_parts.append(_part)
                            _loaded_names.append(_sname)
                    else:
                        logger.warning("[Gateway] Auto-skill '%s' not found", _sname)
                if _combined_parts:
                    # Append the user's original text after all skill payloads
                    _combined_parts.append(event.text)
                    event.text = "\n\n".join(_combined_parts)
                    logger.info(
                        "[Gateway] Auto-loaded skill(s) %s for session %s",
                        _loaded_names, session_key,
                    )
            except Exception as e:
                logger.warning("[Gateway] Failed to auto-load skill(s) %s: %s", _skill_names, e)

        # ── Turn lease (#64934) ────────────────────────────────────────
        # Session resolution is FINAL here (get_or_create → async-delegation
        # pinning → topic tip-walk switch_session are all above). Serialize
        # the [load history → run → flush] region per resolved SESSION_ID:
        # when a second routing key is mapped to this same session_id, its
        # turn waits here for the previous turn's flush instead of loading a
        # stale history base and interleaving transcript writes. Same-key
        # messages never reach this point mid-turn (adapter + runner guards
        # hold them), so the lock is uncontended outside the alias-key route.
        # Fail-closed on timeout: never enter the transcript region without a
        # lease. Outer dispatch returns a bounded rejection/resend notice rather
        # than recreating the exact concurrent-turn corruption this lease exists
        # to prevent. Released in _handle_message's finally via
        # _release_turn_lease — granted per (routing key, run generation) so a
        # stale unwind can't release a newer turn's lease.
        _lease_registry = getattr(self, "_turn_leases", None)
        if _lease_registry is not None:
            try:
                _lease_token = await _lease_registry.acquire(
                    session_entry.session_id,
                    owner_key=_quick_key,
                    generation=run_generation,
                    timeout=_float_env(
                        "HERMES_TURN_LEASE_TIMEOUT", DEFAULT_LEASE_WAIT
                    ),
                )
            except TurnLeaseTimeoutError:
                # The broad session-context cleanup finally starts later in this
                # method. Restore the tokens here before propagating the rejection
                # to outer dispatch, or this early exit leaks task-local identity.
                self._clear_session_env(_session_env_tokens)
                raise
            if _lease_token is not None:
                _lease_state = self._session_state(_quick_key).turn
                _lease_state.lease_token = _lease_token
                _lease_state.lease_generation = run_generation

        # A turn only becomes durable recovery work after it owns (or has
        # explicitly degraded past) the per-session lease.  Marking before the
        # await above would falsely recover an alias-routed message that never
        # began processing if the gateway died while it was still waiting.
        await self._mark_durable_active_turn(event, session_entry.session_key)

        # Load conversation history from transcript
        history = await self.async_session_store.load_transcript(session_entry.session_id)
        
        # -----------------------------------------------------------------
        # Session hygiene: auto-compress pathologically large transcripts
        #
        # Long-lived gateway sessions can accumulate enough history that
        # every new message rehydrates an oversized transcript, causing
        # repeated truncation/context failures.  Detect this early and
        # compress proactively — before the agent even starts.  (#628)
        #
        # Token source priority:
        # 1. Actual API-reported prompt_tokens from the last turn
        #    (stored in session_entry.last_prompt_tokens)
        # 2. Rough char-based estimate (str(msg)//4). Overestimates
        #    by 30-50% on code/JSON-heavy sessions, but that just
        #    means hygiene fires a bit early — safe and harmless.
        # -----------------------------------------------------------------
        if history and len(history) >= 4:
            from agent.model_metadata import (
                estimate_messages_tokens_rough,
                get_model_context_length_async,
            )

            # Read model + compression config from config.yaml.
            # NOTE: hygiene threshold is intentionally HIGHER than the agent's
            # own compressor (0.85 vs 0.50).  Hygiene is a safety net for
            # sessions that grew too large between turns — it fires pre-agent
            # to prevent API failures.  The agent's own compressor handles
            # normal context management during its tool loop with accurate
            # real token counts.  Having hygiene at 0.50 caused premature
            # compression on every turn in long gateway sessions.
            _hyg_model = "anthropic/claude-sonnet-4.6"
            _hyg_threshold_pct = 0.85
            _hyg_compression_enabled = True
            _hyg_hard_msg_limit = 5000
            _hyg_timeout_seconds = 30.0
            _hyg_total_ceiling_seconds = 600.0
            _hyg_failure_cooldown_seconds = 300.0
            _hyg_config_context_length = None
            _hyg_provider = None
            _hyg_base_url = None
            _hyg_api_key = None
            _hyg_configured_model = None
            _hyg_configured_provider = None
            _hyg_configured_base_url = None
            _hyg_data = {}
            try:
                _hyg_data = _load_gateway_config()
                if _hyg_data:
                    # Resolve model name (same logic as run_sync)
                    _model_cfg = _hyg_data.get("model", {})
                    if isinstance(_model_cfg, str):
                        _hyg_model = _model_cfg
                    elif isinstance(_model_cfg, dict):
                        _hyg_model = _model_cfg.get("default") or _model_cfg.get("model") or _hyg_model
                        # Read explicit context_length override from model config
                        # (same as run_agent.py lines 995-1005)
                        _raw_ctx = _model_cfg.get("context_length")
                        if _raw_ctx is not None:
                            try:
                                _hyg_config_context_length = int(_raw_ctx)
                            except (TypeError, ValueError):
                                pass
                        # Read provider for accurate context detection
                        _hyg_provider = _model_cfg.get("provider") or None
                        _hyg_base_url = _model_cfg.get("base_url") or None

                    # Read compression settings — only use enabled flag.
                    # The threshold is intentionally separate from the agent's
                    # compression.threshold (hygiene runs higher).
                    _comp_cfg = _hyg_data.get("compression", {})
                    if isinstance(_comp_cfg, dict):
                        _hyg_compression_enabled = str(
                            _comp_cfg.get("enabled", True)
                        ).lower() in {"true", "1", "yes"}
                        _raw_hard_limit = _comp_cfg.get("hygiene_hard_message_limit")
                        if _raw_hard_limit is not None:
                            try:
                                _parsed = int(_raw_hard_limit)
                                if _parsed > 0:
                                    _hyg_hard_msg_limit = _parsed
                            except (TypeError, ValueError):
                                pass
                        _raw_timeout = _comp_cfg.get("hygiene_timeout_seconds")
                        if _raw_timeout is not None:
                            try:
                                _parsed = float(_raw_timeout)
                                if _parsed > 0:
                                    _hyg_timeout_seconds = _parsed
                            except (TypeError, ValueError):
                                pass
                        _raw_ceiling = _comp_cfg.get("hygiene_total_ceiling_seconds")
                        if _raw_ceiling is not None:
                            try:
                                _parsed = float(_raw_ceiling)
                                if _parsed > 0:
                                    _hyg_total_ceiling_seconds = _parsed
                            except (TypeError, ValueError):
                                pass
                        # The ceiling can never be tighter than one idle
                        # window, or the extension loop would be dead code.
                        _hyg_total_ceiling_seconds = max(
                            _hyg_total_ceiling_seconds, _hyg_timeout_seconds,
                        )
                        _raw_cooldown = _comp_cfg.get("hygiene_failure_cooldown_seconds")
                        if _raw_cooldown is not None:
                            try:
                                _parsed = float(_raw_cooldown)
                                if _parsed >= 0:
                                    _hyg_failure_cooldown_seconds = _parsed
                            except (TypeError, ValueError):
                                pass

                _hyg_configured_model = _hyg_model
                _hyg_configured_provider = _hyg_provider
                _hyg_configured_base_url = _hyg_base_url

                try:
                    _hyg_model, _hyg_runtime = self._resolve_session_agent_runtime(
                        source=source,
                        session_key=session_key,
                        user_config=_hyg_data if isinstance(_hyg_data, dict) else None,
                    )
                    _hyg_provider = _hyg_runtime.get("provider") or _hyg_provider
                    _hyg_base_url = _hyg_runtime.get("base_url") or _hyg_base_url
                    _hyg_api_key = _hyg_runtime.get("api_key") or _hyg_api_key
                except Exception:
                    pass

                if _hyg_config_context_length is not None:
                    try:
                        from hermes_cli.route_identity import should_clear_context_pin_async

                        if await should_clear_context_pin_async(
                            _hyg_configured_model,
                            _hyg_model,
                            _hyg_configured_base_url,
                            _hyg_base_url,
                            _hyg_configured_provider,
                            _hyg_provider,
                        ):
                            _hyg_config_context_length = None
                    except Exception:
                        _hyg_config_context_length = None

                # Check custom_providers per-model context_length
                # (same fallback as run_agent.py lines 1171-1189).
                # Must run after runtime resolution so _hyg_base_url is set.
                if _hyg_config_context_length is None and _hyg_base_url:
                    try:
                        try:
                            from hermes_cli.config import (
                                get_compatible_custom_providers as _gw_gcp,
                                get_custom_provider_context_length as _gw_gccl,
                            )
                            _hyg_custom_providers = _gw_gcp(_hyg_data)
                        except Exception:
                            _hyg_custom_providers = _hyg_data.get("custom_providers")
                            if not isinstance(_hyg_custom_providers, list):
                                _hyg_custom_providers = []
                        _hyg_custom_ctx = _gw_gccl(
                            model=_hyg_model,
                            base_url=_hyg_base_url,
                            custom_providers=_hyg_custom_providers,
                        )
                        if _hyg_custom_ctx:
                            _hyg_config_context_length = int(_hyg_custom_ctx)
                    except (TypeError, ValueError):
                        pass
            except Exception:
                pass

            if _hyg_compression_enabled:
                _hyg_context_length = await get_model_context_length_async(
                    _hyg_model,
                    base_url=_hyg_base_url or "",
                    api_key=_hyg_api_key or "",
                    config_context_length=_hyg_config_context_length,
                    provider=_hyg_provider or "",
                )
                _compress_token_threshold = int(
                    _hyg_context_length * _hyg_threshold_pct
                )
                _warn_token_threshold = int(_hyg_context_length * 0.95)

                _msg_count = len(history)

                # Prefer actual API-reported tokens from the last turn
                # (stored in session entry) over the rough char-based estimate.
                _stored_tokens = session_entry.last_prompt_tokens
                if _stored_tokens > 0:
                    _approx_tokens = _stored_tokens
                    _token_source = "actual"
                else:
                    _approx_tokens = estimate_messages_tokens_rough(history)
                    _token_source = "estimated"
                    # Note: rough estimates overestimate by 30-50% for code/JSON-heavy
                    # sessions, but that just means hygiene fires a bit early — which
                    # is safe and harmless.  The 85% threshold already provides ample
                    # headroom (agent's own compressor runs at 50%).  A previous 1.4x
                    # multiplier tried to compensate by inflating the threshold, but
                    # 85% * 1.4 = 119% of context — which exceeds the model's limit
                    # and prevented hygiene from ever firing for ~200K models (GLM-5).

                # Hard safety valve: force compression if message count is
                # extreme, regardless of token estimates.  This breaks the
                # death spiral where API disconnects prevent token data
                # collection, which prevents compression, which causes more
                # disconnects.  5000 messages is far above any normal session
                # but catches truly runaway growth before it becomes
                # unrecoverable.  Set well clear of legitimate large-context
                # (1M+) sessions doing thousands of short turns — those
                # compress on the token threshold, not this count-based floor.
                # Threshold is configurable via
                # compression.hygiene_hard_message_limit.
                # (#2153)
                _HARD_MSG_LIMIT = _hyg_hard_msg_limit
                _needs_compress = (
                    _approx_tokens >= _compress_token_threshold
                    or _msg_count >= _HARD_MSG_LIMIT
                )

                if _needs_compress:
                    # Use the persistent DB-backed cooldown (same as the
                    # in-conversation compression path in context_compressor.py)
                    # so the cooldown survives gateway restarts. The in-memory
                    # dict was reset on every restart, re-triggering the same
                    # failing compression and wedging session storage (#74136).
                    _session_db = getattr(self, "_session_db", None)
                    if _session_db is not None:
                        _session_db = getattr(_session_db, "_db", _session_db)
                        _getter = getattr(_session_db, "get_compression_failure_cooldown", None)
                        if _getter is not None:
                            try:
                                _cooldown_state = _getter(session_entry.session_id)
                            except Exception:
                                _cooldown_state = None
                            if _cooldown_state and _cooldown_state.get("remaining_seconds", 0) > 0:
                                logger.info(
                                    "Session hygiene: skipping compression for %s; "
                                    "previous failure cooldown active for %.1fs",
                                    session_entry.session_id,
                                    _cooldown_state["remaining_seconds"],
                                )
                                _needs_compress = False

                if _needs_compress:
                    logger.info(
                        "Session hygiene: %s messages, ~%s tokens (%s) — auto-compressing "
                        "(threshold: %s%% of %s = %s tokens)",
                        _msg_count, f"{_approx_tokens:,}", _token_source,
                        int(_hyg_threshold_pct * 100),
                        f"{_hyg_context_length:,}",
                        f"{_compress_token_threshold:,}",
                    )

                    _hyg_meta = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))

                    try:
                        from agent.conversation_compression import CompressionCommitFence
                        from run_agent import AIAgent

                        _hyg_model, _hyg_runtime = self._resolve_session_agent_runtime(
                            source=source,
                            session_key=session_key,
                            user_config=_hyg_data if isinstance(_hyg_data, dict) else None,
                        )
                        if _hyg_runtime.get("api_key"):
                            # Pass the FULL transcript (tool results included).
                            # Filtering to user/assistant-only starved the
                            # compressor: tool results are usually the bulk of
                            # the context, _prune_old_tool_results never saw
                            # them, and short filtered histories tripped the
                            # protect-first/last early-return so nothing was
                            # compressed at all (#3854). The agent loop passes
                            # its full message list to _compress_context — the
                            # gateway now matches.
                            _hyg_msgs = [
                                m for m in history
                                if m.get("role") in {"user", "assistant", "tool"}
                            ]

                            if len(_hyg_msgs) >= 4:
                                try:
                                    _hyg_session_row = await self._session_db.get_session(
                                        session_entry.session_id
                                    )
                                except Exception as exc:
                                    _hyg_session_row = None
                                    logger.warning(
                                        "Session hygiene could not restore the system "
                                        "prompt for session %s: %s. Preserving an empty "
                                        "prompt so the live turn rebuilds it with its "
                                        "configured providers.",
                                        session_entry.session_id,
                                        exc,
                                        exc_info=True,
                                    )
                                _hyg_session_db = getattr(self._session_db, "_db", self._session_db)
                                _hyg_agent = AIAgent(
                                    **_hyg_runtime,
                                    model=_hyg_model,
                                    max_iterations=4,
                                    quiet_mode=True,
                                    skip_memory=True,
                                    enabled_toolsets=["memory"],
                                    session_id=session_entry.session_id,
                                    session_db=_hyg_session_db,
                                )
                                _seed_hygiene_system_prompt(
                                    _hyg_agent,
                                    _hyg_session_row,
                                )
                                # If compression must rebuild instead of retaining
                                # the cached prompt, make the persisted result
                                # deliberately stale for every real gateway surface.
                                _hyg_agent.platform = _GATEWAY_HYGIENE_PLATFORM
                                _hyg_cleanup_deferred = False
                                try:
                                    # Gateway hygiene runs before the user turn
                                    # starts and already owns the session binding.
                                    # Prefer in-place compaction here: it archives
                                    # old rows under the same session id instead of
                                    # minting a continuation child that then has to
                                    # be published back to SessionStore/topic
                                    # bindings.  If no SessionDB is available,
                                    # compress_context leaves this flag false and
                                    # the guard below preserves the transcript.
                                    _hyg_agent.compression_in_place = True
                                    _bind_hyg_state = getattr(
                                        getattr(_hyg_agent, "context_compressor", None),
                                        "bind_session_state",
                                        None,
                                    )
                                    if callable(_bind_hyg_state):
                                        _bind_hyg_state(
                                            _hyg_session_db,
                                            session_entry.session_id,
                                        )
                                    # It must never finalize on close() — close()
                                    # would end the live gateway session row.
                                    _hyg_agent._end_session_on_close = False
                                    _hyg_agent._print_fn = lambda *a, **kw: None

                                    loop = asyncio.get_running_loop()
                                    _hyg_commit_fence = CompressionCommitFence()
                                    _hyg_future = loop.run_in_executor(
                                        None,
                                        lambda: _hyg_agent._compress_context(
                                            _hyg_msgs, "",
                                            approx_tokens=_approx_tokens,
                                            commit_fence=_hyg_commit_fence,
                                        ),
                                    )
                                    try:
                                        # Progress-aware wait: the timeout is an
                                        # INACTIVITY budget, not a total one. The
                                        # compression worker streams its summary
                                        # call and ticks the fence per token
                                        # (CompressionCommitFence.touch_progress),
                                        # so a slow reasoning model that is still
                                        # generating keeps extending the deadline;
                                        # only a genuinely silent worker times out.
                                        # A hard ceiling bounds the total wait so
                                        # a degenerate trickle stream can't hold
                                        # the turn forever.
                                        _hyg_wait_started = time.monotonic()
                                        while True:
                                            # #76354 S3: charge the idle budget
                                            # from the LAST PROGRESS event, not
                                            # from the start of this wait slice —
                                            # otherwise silence can approach 2x
                                            # the configured timeout.
                                            _slice = max(
                                                _hyg_timeout_seconds
                                                - _hyg_commit_fence.seconds_since_progress(),
                                                0.005,
                                            )
                                            try:
                                                _compressed, _ = await asyncio.wait_for(
                                                    asyncio.shield(_hyg_future),
                                                    timeout=_slice,
                                                )
                                                break
                                            except asyncio.TimeoutError:
                                                _hyg_waited = time.monotonic() - _hyg_wait_started
                                                _idle = _hyg_commit_fence.seconds_since_progress()
                                                if (
                                                    _idle < _hyg_timeout_seconds
                                                    and _hyg_waited < _hyg_total_ceiling_seconds
                                                ):
                                                    logger.info(
                                                        "Session hygiene compression for "
                                                        "session %s still streaming after "
                                                        "%.0fs (last progress %.1fs ago) — "
                                                        "extending wait (ceiling %.0fs)",
                                                        session_entry.session_id,
                                                        _hyg_waited, _idle,
                                                        _hyg_total_ceiling_seconds,
                                                    )
                                                    continue
                                                raise
                                    except asyncio.TimeoutError:
                                        _cancelled = None
                                        while _cancelled is None:
                                            # #76354 F1: a hung commit retains the
                                            # fence lock; the lock-free phase
                                            # marker keeps this loop from spinning
                                            # forever while the commit blocks.
                                            if _hyg_commit_fence.commit_in_flight:
                                                _cancelled = False
                                                break
                                            _cancelled = (
                                                _hyg_commit_fence.try_cancel_before_commit()
                                            )
                                            if _cancelled is None:
                                                # Round-2 #5: transient
                                                # lock-setup windows ride
                                                # write patience for seconds;
                                                # 25ms keeps sub-tick latency
                                                # without 1kHz spin.
                                                await asyncio.sleep(0.025)
                                        if not _cancelled:
                                            # The worker crossed the commit boundary just
                                            # before the timeout. The fence poll waited for
                                            # that boundary to finish, so consume the
                                            # completed result instead of treating a
                                            # successful compaction as a timeout.
                                            _compressed, _ = await _hyg_future
                                        else:
                                            # #76354 F4: release the timed-out
                                            # worker's durable lease via the
                                            # holder-qualified hook so the next
                                            # compressor can acquire the lock
                                            # immediately (no ABA against a new
                                            # holder — release is holder-scoped).
                                            _hyg_commit_fence.release_cancelled_compression_lock()
                                            self._defer_agent_cleanup_until_future_done(
                                                _hyg_future,
                                                _hyg_agent,
                                                context="session hygiene timeout",
                                            )
                                            _hyg_cleanup_deferred = True
                                            if _hyg_failure_cooldown_seconds >= 0:
                                                _record_hygiene_cooldown(
                                                    self, session_entry.session_id,
                                                    _hygiene_cooldown_for_failure(
                                                        self, session_key,
                                                        _hyg_failure_cooldown_seconds,
                                                    ),
                                                    "session hygiene compression "
                                                    "timed out with no output from "
                                                    "the summary model",
                                                )
                                            from agent.session_activity import (
                                                ActivityProvenance,
                                            )
                                            _stamp_hygiene_compression_provenance(
                                                _hyg_agent,
                                                "session hygiene compression timed out",
                                                ActivityProvenance.AGENT_COMPRESSION_TIMEOUT,
                                                "hygiene compression timeout "
                                                "activity stamp failed",
                                            )
                                            logger.warning(
                                                "Session hygiene compression for session %s "
                                                "made no progress for %.1fs "
                                                "(total wait %.1fs, ceiling %.1fs); "
                                                "continuing without compression",
                                                session_entry.session_id,
                                                _hyg_commit_fence.seconds_since_progress(),
                                                time.monotonic() - _hyg_wait_started,
                                                _hyg_total_ceiling_seconds,
                                            )
                                            _timeout_msg = (
                                                "⚠️ Context compression timed out "
                                                f"after {_hyg_timeout_seconds:.1f}s "
                                                "with no output from the summary model. "
                                                "No messages were dropped — continuing without "
                                                "compression. Run /compress to retry, /reset for "
                                                "a clean session, or check your "
                                                "auxiliary.compression model configuration."
                                            )
                                            try:
                                                _adapter = self._adapter_for_source(source)
                                                if _adapter and source.chat_id:
                                                    await _adapter.send(
                                                        source.chat_id,
                                                        _timeout_msg,
                                                        metadata=_hyg_meta,
                                                    )
                                            except Exception as _werr:
                                                logger.warning(
                                                    "Failed to deliver compression-timeout "
                                                    "warning to user: %s",
                                                    _werr,
                                                )
                                            raise
                                    except BaseException:
                                        # #76354 F2: non-timeout unwind while the
                                        # detached hygiene worker may still run —
                                        # KeyboardInterrupt, task cancellation, or
                                        # any unexpected error. Revoke commit
                                        # admission (and release the worker's
                                        # durable lease via the holder-qualified
                                        # hook) BEFORE the host unwinds so the
                                        # worker can never commit later.
                                        _hyg_commit_fence.revoke_commit_admission()
                                        if not _hyg_cleanup_deferred:
                                            self._defer_agent_cleanup_until_future_done(
                                                _hyg_future,
                                                _hyg_agent,
                                                context="session hygiene unwind",
                                            )
                                            _hyg_cleanup_deferred = True
                                        raise

                                    # _compress_context ends the old session and creates
                                    # a new session_id.  Write compressed messages into
                                    # the NEW session so the old transcript stays intact
                                    # and searchable via session_search.
                                    _hyg_new_sid = _hyg_agent.session_id
                                    _hyg_rotated = _hyg_new_sid != session_entry.session_id
                                    _hyg_in_place = bool(
                                        getattr(_hyg_agent, "_last_compaction_in_place", False)
                                    )
                                    # Only rewrite the transcript when rotation produced
                                    # a NEW session id.  In-place compaction does NOT
                                    # need a rewrite: archive_and_compact() has already
                                    # soft-archived the previous active rows and inserted
                                    # the compacted messages as the new active set inside
                                    # _compress_context().  Calling rewrite_transcript()
                                    # after in-place compaction would invoke
                                    # replace_messages(active_only=False) which DELETEs
                                    # ALL rows — including the archived turns that
                                    # archive_and_compact() deliberately preserved
                                    # (silent data loss, #61145).
                                    #
                                    # The danger this guards against (mirrors the
                                    # /compress fix #44794/#39704): if _compress_context
                                    # returns a summary but neither rotates nor completes
                                    # archive_and_compact(), the session_id is unchanged
                                    # for a FAILURE reason, and an unconditional
                                    # rewrite_transcript() would DELETE the original
                                    # messages and replace them with only the compressed
                                    # summary (permanent data loss, #21301).
                                    #
                                    # Write-before-repoint (mirrors manual /compress):
                                    # if we repointed session_entry onto the child SID
                                    # and rewrite_transcript then failed (lock/ENOSPC),
                                    # the live entry would already reference a brand-new
                                    # empty session while the turn continues — the
                                    # conversation silently vanishes. Persist the child
                                    # transcript first; only then rebind the live entry.
                                    if _hyg_rotated:
                                        if not await self.async_session_store.rewrite_transcript(
                                            _hyg_new_sid, _compressed
                                        ):
                                            logger.error(
                                                "Session hygiene: failed to persist "
                                                "compressed transcript for rotated "
                                                "session %s → %s; keeping the live "
                                                "entry on the original session so the "
                                                "conversation is not dropped",
                                                session_entry.session_id,
                                                _hyg_new_sid,
                                            )
                                            # Fail closed: treat like no rotation.
                                            _hyg_rotated = False
                                            _hyg_in_place = False
                                        else:
                                            session_entry.session_id = _hyg_new_sid
                                            # The held turn lease follows the
                                            # rotation so an alias key resolving
                                            # the fresh child still serializes
                                            # against this turn (#64934).
                                            self._rebind_turn_lease(
                                                _quick_key, run_generation, _hyg_new_sid
                                            )
                                            await self.async_session_store._save()
                                            await asyncio.to_thread(
                                                self._sync_telegram_topic_binding,
                                                source, session_entry,
                                                reason="hygiene-compression",
                                            )

                                    if _hyg_rotated:
                                        # Reset stored token count — transcript rewritten
                                        session_entry.last_prompt_tokens = 0
                                        history = _compressed
                                        _new_count = len(_compressed)
                                        _new_tokens = estimate_messages_tokens_rough(
                                            _compressed
                                        )
                                    elif _hyg_in_place:
                                        # archive_and_compact() already persisted the
                                        # compacted transcript inside _compress_context.
                                        # Reset counts to match the new active set.
                                        session_entry.last_prompt_tokens = 0
                                        history = _compressed
                                        _new_count = len(_compressed)
                                        _new_tokens = estimate_messages_tokens_rough(
                                            _compressed
                                        )
                                    else:
                                        # No rewrite happened — transcript preserved
                                        # unchanged, so the post-compression counts equal
                                        # the pre-compression ones.
                                        _new_count = _msg_count
                                        _new_tokens = _approx_tokens
                                        logger.warning(
                                            "Gateway hygiene compression for session %s "
                                            "did not rotate or compact in place "
                                            "(no session_db on the hygiene agent) — "
                                            "preserving the original transcript instead "
                                            "of overwriting it with the summary (#21301).",
                                            session_entry.session_id,
                                        )

                                    logger.info(
                                        "Session hygiene: compressed %s → %s msgs, "
                                        "~%s → ~%s tokens",
                                        _msg_count, _new_count,
                                        f"{_approx_tokens:,}", f"{_new_tokens:,}",
                                    )

                                    if _new_tokens >= _warn_token_threshold:
                                        logger.warning(
                                            "Session hygiene: still ~%s tokens after "
                                            "compression",
                                            f"{_new_tokens:,}",
                                        )

                                    # If summary generation failed, the
                                    # compressor aborts entirely and returns
                                    # messages unchanged — nothing is dropped.
                                    # Surface a visible warning to the gateway
                                    # user — agent.log alone is invisible on
                                    # TG/Discord/etc. — so they know the chat
                                    # is "frozen" at the current size and can
                                    # /compress to retry or /reset to start
                                    # fresh.
                                    _comp = getattr(_hyg_agent, "context_compressor", None)
                                    _hyg_aborted = _comp is not None and getattr(
                                        _comp, "_last_compress_aborted", False
                                    )
                                    if not _hyg_aborted:
                                        # Recovery decision lives in the
                                        # extracted, unit-tested predicate — the
                                        # degenerate "did not rotate or compact
                                        # in place" path (#21301) sets both flags
                                        # False and reuses the pre-compression
                                        # counts, so a numbers-only check would
                                        # read a no-op as success and clear the
                                        # streak on every wedged run (#79624).
                                        if hygiene_compaction_recovered(
                                            aborted=_hyg_aborted,
                                            rotated=_hyg_rotated,
                                            in_place=_hyg_in_place,
                                            msg_count=_msg_count,
                                            new_count=_new_count,
                                            approx_tokens=_approx_tokens,
                                            new_tokens=_new_tokens,
                                        ):
                                            _reset_hygiene_failure_streak(
                                                self, session_key
                                            )
                                    if _hyg_aborted:
                                        if _hyg_failure_cooldown_seconds >= 0:
                                            _record_hygiene_cooldown(
                                                self, session_entry.session_id,
                                                _hygiene_cooldown_for_failure(
                                                    self, session_key,
                                                    _hyg_failure_cooldown_seconds,
                                                ),
                                                getattr(
                                                    _comp, "_last_summary_error", None
                                                ),
                                            )
                                        from agent.session_activity import (
                                            ActivityProvenance,
                                        )
                                        _stamp_hygiene_compression_provenance(
                                            _hyg_agent,
                                            "session hygiene compression aborted",
                                            ActivityProvenance.AGENT_COMPRESSION_COOLDOWN,
                                            "hygiene compression abort "
                                            "activity stamp failed",
                                        )
                                        _err = getattr(_comp, "_last_summary_error", None) or "unknown error"
                                        # Force-redact: provider exception text
                                        # may contain credentials; this message
                                        # reaches gateway users directly.
                                        from agent.redact import redact_sensitive_text
                                        _err = redact_sensitive_text(_err, force=True)
                                        _warn_msg = (
                                            "⚠️ Context compression aborted "
                                            f"({_err}). No messages were dropped — "
                                            "conversation is unchanged. Run /compress "
                                            "to retry, /reset for a clean session, or "
                                            "check your auxiliary.compression model "
                                            "configuration."
                                        )
                                        try:
                                            _adapter = self._adapter_for_source(source)
                                            if _adapter and source.chat_id:
                                                await _adapter.send(source.chat_id, _warn_msg, metadata=_hyg_meta)
                                        except Exception as _werr:
                                            logger.warning(
                                                "Failed to deliver compression-failure warning to user: %s",
                                                _werr,
                                            )
                                    # Separately: if the user's CONFIGURED aux
                                    # model failed and we recovered by falling
                                    # back to the main model, tell them — a
                                    # misconfigured auxiliary.compression.model
                                    # is something only they can fix, and
                                    # silent recovery would hide it.
                                    elif _comp is not None and getattr(_comp, "_last_aux_model_failure_model", None):
                                        _aux_model = getattr(_comp, "_last_aux_model_failure_model", "")
                                        _aux_err = getattr(_comp, "_last_aux_model_failure_error", None) or "unknown error"
                                        _aux_msg = (
                                            f"ℹ️ Configured compression model `{_aux_model}` "
                                            f"failed ({_aux_err}). Recovered using your main "
                                            "model — context is intact — but you may want to "
                                            "check `auxiliary.compression.model` in config.yaml."
                                        )
                                        try:
                                            _adapter = self._adapter_for_source(source)
                                            if _adapter and source.chat_id:
                                                await _adapter.send(source.chat_id, _aux_msg, metadata=_hyg_meta)
                                        except Exception as _werr:
                                            logger.warning(
                                                "Failed to deliver aux-model-fallback notice to user: %s",
                                                _werr,
                                            )
                                finally:
                                    # Evict the cached agent so the next turn
                                    # rebuilds its system prompt from current
                                    # SOUL.md, memory, and skills.
                                    self._evict_cached_agent(session_key)
                                    if not _hyg_cleanup_deferred:
                                        await self._cleanup_agent_resources_off_loop(
                                            _hyg_agent, context="session hygiene"
                                        )

                    except Exception as e:
                        logger.warning(
                            "Session hygiene auto-compress failed: %s", e
                        )

        # First-message onboarding -- only on the very first interaction ever.
        # Delivered on the current user message (sidecar), NOT the ephemeral
        # system prompt: present-on-turn-1/absent-on-turn-2 was a guaranteed
        # system-prompt diff and agent rebuild.
        if not history and not await self.async_session_store.has_any_sessions():
            # Default first-contact note: a brief self-introduction.
            _intro_note = (
                "[System note: This is the user's very first message ever. "
                "Briefly introduce yourself and mention that /help shows available commands. "
                "Keep the introduction concise -- one or two sentences max.]"
            )
            # Opt-in structured profile-build path. When enabled (default
            # "ask") and not yet offered on this install, swap the plain intro
            # for a consent-gated directive that offers to build a user
            # profile and persists confirmed facts via memory(target="user").
            # The offer fires at most once (onboarding.seen flag); set
            # onboarding.profile_build: off in config.yaml to disable.
            try:
                from agent.onboarding import (
                    PROFILE_BUILD_FLAG,
                    is_seen,
                    mark_seen,
                    profile_build_directive,
                    profile_build_mode,
                )
                _onb_cfg = _load_gateway_config()
                if (
                    profile_build_mode(_onb_cfg) == "ask"
                    and not is_seen(_onb_cfg, PROFILE_BUILD_FLAG)
                ):
                    turn_sidecar_notes.append(profile_build_directive().strip())
                    mark_seen(_hermes_home / "config.yaml", PROFILE_BUILD_FLAG)
                else:
                    turn_sidecar_notes.append(_intro_note)
            except Exception as _pb_err:
                logger.debug(
                    "Profile-build onboarding directive failed, using plain intro: %s",
                    _pb_err,
                )
                turn_sidecar_notes.append(_intro_note)
        
        # One-time prompt if no home channel is set for this platform
        # Skip for webhooks - they deliver directly to configured targets (github_comment, etc.)
        if not history and source.platform and source.platform != Platform.LOCAL and source.platform != Platform.WEBHOOK:
            platform_name = source.platform.value
            env_key = _home_target_env_var(platform_name)
            # Multiplex: home channel may live only in the profile secret
            # scope / PlatformConfig, not process os.environ.
            home_env = ""
            try:
                from agent.secret_scope import get_secret

                home_env = (get_secret(env_key) or "").strip() if env_key else ""
            except Exception:
                home_env = ""
            if not home_env:
                home_env = (os.getenv(env_key) or "").strip() if env_key else ""
            # Also honor in-memory / yaml home_channel on this platform.
            try:
                if not home_env and self.config.get_home_channel(source.platform):
                    home_env = "set"
            except Exception:
                pass
            # Secondary-profile platforms (e.g. Slack on yolo) may only exist
            # under that profile's loaded config — check after scope install.
            if not home_env:
                try:
                    from gateway.config import load_gateway_config as _lgc
                    prof = (getattr(source, "profile", None) or "").strip()
                    if prof and prof != "default":
                        # Already inside profile scope for secondary handlers;
                        # re-read live config for home_channel.
                        _pcfg = _lgc()
                        if _pcfg.get_home_channel(source.platform):
                            home_env = "set"
                except Exception:
                    pass
            if not home_env:
                # Slack dispatches all Hermes commands through a single
                # parent slash command `/hermes`; bare `/sethome` is not
                # registered and would fail with "app did not respond".
                sethome_cmd = (
                    "/hermes sethome"
                    if source.platform == Platform.SLACK
                    else "/sethome"
                )
                notice = (
                    f"📬 No home channel is set for {platform_name.title()}. "
                    f"A home channel is where Hermes delivers cron job results "
                    f"and cross-platform messages.\n\n"
                    f"Type {sethome_cmd} to make this chat your home channel, "
                    f"or ignore to skip."
                )
                await self._deliver_platform_notice(source, notice)
        
        # -----------------------------------------------------------------
        # Voice channel awareness — deliver current voice channel state so
        # the agent knows who is in the channel and who is speaking, without
        # needing a separate tool call.  Delivered on the current user
        # message and ONLY when it changed since the previous turn: the
        # member/speaking serialization differs essentially every turn, and
        # appending it to the ephemeral system prompt forced a full agent
        # rebuild + prompt-cache re-key per message.  The system prompt
        # carries a static pointer line instead (gateway/session.py).
        # -----------------------------------------------------------------
        _vc_note = self._voice_channel_sidecar_note(event, source, session_key)
        if _vc_note:
            turn_sidecar_notes.append(_vc_note)

        # -----------------------------------------------------------------
        # Auto-analyze images sent by the user
        #
        # If the user attached image(s), we run the vision tool eagerly so
        # the conversation model always receives a text description.  The
        # local file path is also included so the model can re-examine the
        # image later with a more targeted question via vision_analyze.
        #
        # We filter to image paths only (by media_type) so that non-image
        # attachments (documents, audio, etc.) are not sent to the vision
        # tool even when they appear in the same message.
        # -----------------------------------------------------------------
        message_text = await self._prepare_profile_scoped_inbound_message_text(
            event=event,
            source=source,
            history=history,
            session_key=session_key,
        )
        if message_text is None:
            return

        # Capture the platform event time as message metadata and keep the
        # persisted transcript clean (strip any leading timestamp prefix).
        # This runs regardless of the toggle so storage stays clean and the
        # send-time is preserved. Only the in-context RENDER (prepending the
        # human-readable prefix the model sees) is gated behind
        # gateway.message_timestamps.enabled — default OFF.
        try:
            from hermes_time import get_timezone as _get_evt_tz
            from gateway.message_timestamps import (
                coerce_message_timestamp as _coerce_msg_ts,
                render_user_content_with_timestamp as _render_msg_ts,
                strip_leading_message_timestamps as _strip_msg_ts,
            )
            _evt_tz = _get_evt_tz()
            _evt_ts = getattr(event, "timestamp", None)
            if message_text and isinstance(message_text, str):
                _clean_message_text, _embedded_ts = _strip_msg_ts(
                    message_text, tz=_evt_tz)
                persist_user_message = _clean_message_text
                _event_epoch = _coerce_msg_ts(_evt_ts, tz=_evt_tz)
                persist_user_timestamp = (
                    _event_epoch if _event_epoch is not None else _embedded_ts
                )
                if _message_timestamps_enabled(_load_gateway_config()):
                    message_text = _render_msg_ts(
                        _clean_message_text,
                        persist_user_timestamp,
                        tz=_evt_tz,
                    )
                else:
                    # Toggle off: model sees the clean message; the timestamp
                    # is still stored as metadata for later opt-in.
                    message_text = _clean_message_text
        except Exception as _ts_err:
            logger.debug("Message timestamp injection failed (non-fatal): %s", _ts_err)

        # Stage the collected must-deliver notes for this turn's agent run
        # (one-shot; consumed in run_sync).  Staged AFTER the message_text
        # early-out above so an aborted turn cannot leak its notes into the
        # next turn's user message.
        if turn_sidecar_notes and session_key:
            self._set_pending_turn_sidecar_notes(session_key, turn_sidecar_notes)

        # Bind this gateway run generation to the adapter's active-session
        # event so deferred post-delivery callbacks can be released by the
        # same run that registered them.
        self._bind_adapter_run_generation(
            self._adapter_for_source(source),
            session_key,
            run_generation,
        )

        try:
            # Emit agent:start hook
            hook_ctx = {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "chat_id": source.chat_id or "",
                "thread_id": str(getattr(source, "thread_id", None)) if getattr(source, "thread_id", None) else "",
                "chat_type": getattr(source, "chat_type", "") or "",
                "session_id": session_entry.session_id,
                "message": message_text[:500],
            }
            await self.hooks.emit("agent:start", hook_ctx)

            # Run the agent. Capture the session id that this run was launched
            # against so post-run compression publication can be identity-guarded
            # below; a /new or another lifecycle transition may move
            # session_entry.session_id while the old run is still unwinding.
            _run_start_session_id = session_entry.session_id
            _turn_started_monotonic = time.monotonic()
            agent_result = await self._run_agent(
                message=message_text,
                context_prompt=context_prompt,
                history=history,
                source=source,
                session_id=_run_start_session_id,
                session_key=session_key,
                run_generation=run_generation,
                event_message_id=self._reply_anchor_for_event(event),
                channel_prompt=event.channel_prompt,
                moa_config=getattr(event, "_moa_config", None),
                persist_user_message=persist_user_message,
                persist_user_timestamp=persist_user_timestamp,
                persist_user_display_kind=persist_user_display_kind,
                message_type=event.message_type,
            )
            _turn_seconds = time.monotonic() - _turn_started_monotonic

            # Stop persistent typing indicator now that the agent is done.
            # Slack AI status is scoped to a thread/workspace, so preserve the
            # same routing metadata used by the response delivery path.
            try:
                _typing_adapter = self._adapter_for_source(source)
                _stop_with_metadata = getattr(
                    type(_typing_adapter), "_stop_typing_with_metadata", None
                )
                _stop_typing = getattr(type(_typing_adapter), "stop_typing", None)
                if _typing_adapter and callable(_stop_with_metadata):
                    await _typing_adapter._stop_typing_with_metadata(
                        source.chat_id,
                        self._thread_metadata_for_source(
                            source, self._reply_anchor_for_event(event)
                        ),
                    )
                elif _typing_adapter and callable(_stop_typing):
                    await _typing_adapter.stop_typing(source.chat_id)
            except Exception:
                pass

            if not self._is_session_run_current(_quick_key, run_generation):
                logger.info(
                    "Discarding stale agent result for %s — generation %d is no longer current",
                    _quick_key or "?",
                    run_generation,
                )
                _stale_adapter = self._adapter_for_source(source)
                if getattr(type(_stale_adapter), "pop_post_delivery_callback", None) is not None:
                    _stale_adapter.pop_post_delivery_callback(
                        _quick_key,
                        generation=run_generation,
                    )
                elif _stale_adapter and hasattr(_stale_adapter, "_post_delivery_callbacks"):
                    _stale_adapter._post_delivery_callbacks.pop(_quick_key, None)
                return None

            response = agent_result.get("final_response") or ""
            # Hidden-reasoning-only retry exhaustion: the loop's sentinel text
            # ("Codex response remained incomplete after 3 continuation
            # attempts") doubles as final_response, so it would be delivered
            # verbatim into the channel — where peer agents can ingest it as a
            # completed assistant turn (#51628). Blank it here so the normal
            # empty-response handling (and the suppression below) applies.
            if _is_gateway_hidden_reasoning_incomplete_turn(agent_result):
                response = ""
            try:
                from gateway.response_filters import is_intentional_silence_agent_result
                _intentional_silence = is_intentional_silence_agent_result(
                    agent_result, response,
                )
            except Exception:
                _intentional_silence = False

            # Convert the agent's internal "(empty)" sentinel into a
            # user-friendly message.  "(empty)" means the model failed to
            # produce visible content after exhausting all retries (nudge,
            # prefill, empty-retry, fallback).  Sending the raw sentinel
            # looks like a bug; a short explanation is more helpful.
            if response == "(empty)" and not _intentional_silence:
                response = (
                    "⚠️ The model returned no response after processing tool "
                    "results. This can happen with some models — try again or "
                    "rephrase your question."
                )
            agent_messages = agent_result.get("messages", [])
            _response_time = time.time() - _msg_start_time
            _api_calls = agent_result.get("api_calls", 0)
            _resp_len = len(response)
            logger.info(
                "response ready: platform=%s chat=%s time=%.1fs api_calls=%d response=%d chars",
                _platform_name, source.chat_id or "unknown",
                _response_time, _api_calls, _resp_len,
            )

            # NOTE: the cross-process cache-coherence re-baseline
            # (_refresh_agent_cache_message_count) is intentionally deferred
            # until AFTER this turn's transcript persistence block below — it
            # must include the first-turn `session_meta` marker row and the
            # compression session_id swap, both of which happen later.  See
            # the call site after the `update_session(...)` write.

            # Successful turn — clear any stuck-loop counter for this session.
            # This ensures the counter only accumulates across CONSECUTIVE
            # restarts where the session was active (never completed).
            #
            # Also clear the resume_pending flag (set by drain-timeout
            # shutdown) — the turn ran to completion, so recovery
            # succeeded and subsequent messages should no longer receive
            # the restart-interruption system note.
            if session_key and _should_clear_resume_pending_after_turn(agent_result):
                await self._clear_restart_failure_count(session_key)
                try:
                    await self.async_session_store.clear_resume_pending(session_key)
                except Exception as _e:
                    logger.debug(
                        "clear_resume_pending failed for %s: %s",
                        session_key, _e,
                    )

            # Normalize empty responses: surface errors, partial failures, and
            # the case where agent did work but returned no text. Fix for #18765.
            if not _intentional_silence:
                response = _normalize_empty_agent_response(
                    agent_result, response, history_len=len(history),
                )
                response = _sanitize_gateway_final_response(source.platform, response)

            # Ordering contract: the agent thread already updated the contextvar
            # in conversation_compression.py; propagate to SessionEntry + _save().
            # If the agent's session_id changed during compression, update
            # session_entry so transcript writes below go to the right session.
            if agent_result.get("session_id") and agent_result["session_id"] != session_entry.session_id:
                if session_entry.session_id == _run_start_session_id:
                    session_entry.session_id = agent_result["session_id"]
                    # The held turn lease follows the rotation: the transcript
                    # persistence below writes to the NEW id, so the
                    # serialization boundary must move with it or an alias
                    # key resolving the fresh child could interleave (#64934).
                    self._rebind_turn_lease(
                        _quick_key, run_generation, session_entry.session_id
                    )
                    await self.async_session_store._save()
                    await self.async_session_store._record_gateway_session_peer(
                        session_entry.session_id,
                        session_key,
                        source,
                    )
                    await asyncio.to_thread(
                        self._sync_telegram_topic_binding,
                        source, session_entry, reason="agent-result-compression",
                    )
                else:
                    logger.info(
                        "Skipping agent-result session split sync for %s because "
                        "the session binding moved from %s to %s before "
                        "compression finished",
                        session_key or "?",
                        _run_start_session_id,
                        session_entry.session_id,
                    )

            # Prepend reasoning/thinking if display is enabled (per-platform).
            # Mattermost requires explicit per-platform opt-in because this is
            # scratch text, not ordinary final-answer content.
            try:
                _show_reasoning_effective = _resolve_gateway_display_bool(
                    _load_gateway_config(),
                    _platform_config_key(source.platform),
                    "show_reasoning",
                    default=bool(getattr(self, "_show_reasoning", False)),
                    platform=source.platform,
                    require_platform_override_for={Platform.MATTERMOST},
                )
            except Exception:
                _show_reasoning_effective = (
                    False
                    if source.platform == Platform.MATTERMOST
                    else getattr(self, "_show_reasoning", False)
                )
            if _show_reasoning_effective and response and not _intentional_silence:
                last_reasoning = agent_result.get("last_reasoning")
                if last_reasoning:
                    from gateway.stream_consumer import escape_code_fences_for_display
                    # Collapse long reasoning to keep messages readable
                    lines = last_reasoning.strip().splitlines()
                    if len(lines) > 15:
                        display_reasoning = "\n".join(lines[:15])
                        display_reasoning += f"\n_... ({len(lines) - 15} more lines)_"
                    else:
                        display_reasoning = last_reasoning.strip()
                    # Render style is per-platform: Discord defaults to "-# "
                    # subtext (native small grey metadata text); other
                    # platforms keep the fenced code block.
                    try:
                        from gateway.display_config import resolve_display_setting
                        _reasoning_style = resolve_display_setting(
                            _load_gateway_config(),
                            _platform_config_key(source.platform),
                            "reasoning_style",
                            "code",
                        )
                    except Exception:
                        _reasoning_style = "code"
                    if _reasoning_style == "subtext":
                        _quoted = "\n".join(
                            f"-# {ln}" if ln else "-#" for ln in display_reasoning.splitlines()
                        )
                        response = f"-# 💭 Reasoning\n{_quoted}\n\n{response}"
                    elif _reasoning_style == "blockquote":
                        _quoted = "\n".join(
                            f"> {ln}" if ln else ">" for ln in display_reasoning.splitlines()
                        )
                        response = f"> 💭 **Reasoning:**\n{_quoted}\n\n{response}"
                    else:
                        # Escape ``` inside reasoning so inner fences don't
                        # break the outer code block used to render it.
                        display_reasoning = escape_code_fences_for_display(display_reasoning)
                        response = f"💭 **Reasoning:**\n```\n{display_reasoning}\n```\n\n{response}"

            # Runtime-metadata footer — only on the FINAL message of the turn.
            # Off by default (display.runtime_footer.enabled=false).  When
            # streaming already delivered the body, we can't mutate the sent
            # text, so we fire a separate trailing send below.
            _footer_line = ""
            try:
                from gateway.runtime_footer import build_footer_line as _bfl
                _footer_line = _bfl(
                    user_config=_load_gateway_config(),
                    platform_key=_platform_config_key(source.platform),
                    model=agent_result.get("model"),
                    context_tokens=agent_result.get("last_prompt_tokens", 0) or 0,
                    context_length=agent_result.get("context_length") or None,
                    cwd=os.environ.get("TERMINAL_CWD", ""),
                    turn_seconds=_turn_seconds,
                )
            except Exception as _footer_err:
                logger.debug("runtime_footer build failed: %s", _footer_err)
                _footer_line = ""
            if _footer_line and response and not agent_result.get("already_sent") and not _intentional_silence:
                response = f"{response}\n\n{_footer_line}"

            # Emit agent:end hook
            await self.hooks.emit("agent:end", {
                **hook_ctx,
                "response": (response or "")[:500],
                "model": agent_result.get("model", ""),
                "provider": agent_result.get("provider", ""),
            })
            
            # Check for pending process watchers (check_interval on background processes)
            try:
                from tools.process_registry import process_registry
                # Detach the current batch atomically (see crash-recovery drain
                # above): reassign to a fresh list so a watcher appended by a
                # concurrent session during the yield isn't dropped by clear().
                watchers = process_registry.pending_watchers
                process_registry.pending_watchers = []
                for i, watcher in enumerate(watchers):
                    asyncio.create_task(self._run_process_watcher(watcher))
                    if i % 100 == 99:
                        await asyncio.sleep(0)
            except Exception as e:
                logger.error("Process watcher setup error: %s", e)

            # Drain watch pattern notifications that arrived during the agent run.
            # Watch events and completions share the same queue; process
            # completions are already handled by the per-process watcher task
            # above, so we only inject watch-type events here.
            #
            # Async-delegation completions ALSO ride this shared queue but are
            # owned by the dedicated _async_delegation_watcher (started at
            # boot), which covers both the idle and post-turn cases with a
            # single consumer — so we leave them on the queue here.
            try:
                from tools.process_registry import process_registry as _pr
                await self._drain_watch_notifications(_pr.completion_queue)
            except Exception as e:
                logger.debug("Watch queue drain error: %s", e)

            # NOTE: Dangerous command approvals are now handled inline by the
            # blocking gateway approval mechanism in tools/approval.py.  The agent
            # thread blocks until the user responds with /approve or /deny, so by
            # the time we reach here the approval has already been resolved.  The
            # old post-loop pop_pending + approval_hint code was removed in favour
            # of the blocking approach that mirrors CLI's synchronous input().
            
            # Save the full conversation to the transcript, including tool calls.
            # This preserves the complete agent loop (tool_calls, tool results,
            # intermediate reasoning) so sessions can be resumed with full context
            # and transcripts are useful for debugging and training data.
            #
            # IMPORTANT: For context-overflow failures (compression exhausted,
            # generic 400 on large sessions) we must NOT persist the user's
            # message — doing so would grow the session further and cause the
            # same failure on the next attempt, an infinite loop. (#1630, #9893)
            #
            # Transient failures (429, timeout, connection error, provider 5xx)
            # are different: the session is not oversized, and silently dropping
            # the user message causes severe context loss on retry — the agent
            # forgets what was just asked.  Persist the user turn so the
            # conversation is preserved. (#7100)
            agent_failed_early = bool(agent_result.get("failed"))
            hidden_reasoning_incomplete = _is_gateway_hidden_reasoning_incomplete_turn(
                agent_result
            )
            _err_str_for_classify = str(agent_result.get("error", "")).lower()
            # Use specific multi-word phrases (not bare "exceed" or "token")
            # to avoid false positives on transient errors like "rate limit
            # exceeded" or "invalid auth token". Matches run_agent.py's
            # own context-length classifier.
            is_context_overflow_failure = agent_failed_early and (
                bool(agent_result.get("compression_exhausted"))
                or any(p in _err_str_for_classify for p in (
                    "context length", "context size", "context window",
                    "maximum context", "token limit", "too many tokens",
                    "reduce the length", "exceeds the limit",
                    "request entity too large", "prompt is too long",
                    "payload too large", "input is too long",
                ))
                or ("400" in _err_str_for_classify and len(history) > 50)
            )
            if is_context_overflow_failure:
                logger.info(
                    "Skipping transcript persistence for context-overflow "
                    "failure in session %s to prevent session growth loop.",
                    session_entry.session_id,
                )
            elif agent_failed_early:
                logger.info(
                    "Transient agent failure in session %s — persisting user "
                    "message so conversation context is preserved on retry.",
                    session_entry.session_id,
                )
            elif hidden_reasoning_incomplete:
                logger.warning(
                    "Suppressing hidden-reasoning-only incomplete gateway turn "
                    "for session %s: %s",
                    session_entry.session_id,
                    agent_result.get("error", "processing incomplete"),
                )

            # When compression is exhausted, the session is permanently too
            # large to process.  Auto-reset it so the next message starts
            # fresh instead of replaying the same oversized context in an
            # infinite fail loop.  (#9893)
            #
            # A lock-contended defer is the OPPOSITE case: the session is
            # temporarily uncompressible only because a concurrent path holds
            # the compression lock and is actively shrinking it. Never wipe
            # the session for that — retry-next-message semantics apply
            # (#69870 lock-skip consumer; salvaged from #49874).
            if agent_result.get("compression_deferred"):
                logger.info(
                    "Compression deferred for session %s — the compression "
                    "lock is held by a concurrent compressor. Keeping the "
                    "session intact; the next message retries normally.",
                    session_entry.session_id if session_entry else "?",
                )
            elif agent_result.get("compression_exhausted") and session_entry and session_key:
                logger.info(
                    "Auto-resetting session %s after compression exhaustion.",
                    session_entry.session_id,
                )
                new_entry = await self.async_session_store.reset_session(session_key)
                self._evict_cached_agent(session_key)
                # Conversation boundary: one funnel call clears every
                # conversation-scoped per-session dict (#58403 and siblings).
                # See _CONVERSATION_SCOPED_STATE.
                self._clear_conversation_scope(
                    session_key, reason="compression_exhausted_reset"
                )
                if new_entry is not None:
                    # Drop the stale reference to the bloated compressed child and
                    # re-point the Telegram topic binding at the fresh session.
                    # Compression rotated session_entry.session_id to the oversized
                    # compressed child earlier this turn (the agent-result sync
                    # above), and that _sync also rewrote the (chat_id, thread_id)
                    # -> bloated-child binding. reset_session swaps in a clean,
                    # parentless session, but without re-syncing the binding the
                    # next inbound message in this topic gets switch_session'd back
                    # onto the bloated child by the binding-heal walk, reloads the
                    # oversized transcript, and re-triggers compression exhaustion
                    # forever (#35809 — regression of the #9893/#10063 auto-reset).
                    # No-op on non-topic lanes.
                    session_entry = new_entry
                    await asyncio.to_thread(
                        self._sync_telegram_topic_binding,
                        source, session_entry, reason="compression-exhausted-reset",
                    )
                response = (response or "") + (
                    "\n\n🔄 Session auto-reset — the conversation exceeded the "
                    "maximum context size and could not be compressed further. "
                    "Your next message will start a fresh session."
                )

            ts = time.time()  # Unix epoch float — consistent with DB storage
            
            # If this is a fresh session (no history), write the full tool
            # definitions as the first entry so the transcript is self-describing
            # -- the same list of dicts sent as tools=[...] in the API request.
            if is_context_overflow_failure:
                pass  # Skip all transcript writes — don't grow a broken session
            elif not history:
                tool_defs = agent_result.get("tools", [])
                await self.async_session_store.append_to_transcript(
                    session_entry.session_id,
                    {
                        "role": "session_meta",
                        "tools": tool_defs or [],
                        "model": _resolve_gateway_model(),
                        "platform": source.platform.value if source.platform else "",
                        "timestamp": ts,
                    }
                )
            
            # The agent already persisted these messages to SQLite via
            # _flush_messages_to_session_db(), so skip the DB write here
            # to prevent the duplicate-write bug (#860 / #42039). This holds
            # for the codex app-server runtime too: although it early-returns
            # and bypasses conversation_loop's per-step flushes, it flushes its
            # own projected assistant/tool messages before returning and
            # reports agent_persisted=True (see agent/codex_runtime.py). Reading
            # the flag (default = self._session_db is not None) keeps the
            # persistence contract explicit and lets any future non-persisting
            # runtime opt into a gateway-side write by returning False.
            agent_persisted = agent_result.get("agent_persisted", self._session_db is not None)

            # Find only the NEW messages from this turn (skip history we loaded).
            # Use the filtered history length (history_offset) that was actually
            # passed to the agent, not len(history) which includes session_meta
            # entries that were stripped before the agent saw them.
            if is_context_overflow_failure:
                pass  # handled above — skip all transcript writes
            elif agent_failed_early or hidden_reasoning_incomplete:
                # Transient failure (429/timeout/5xx): persist only the user
                # message so the next message can load a transcript that
                # reflects what was said.  Skip the assistant error text since
                # it's a gateway-generated hint, not model output. Hidden-
                # reasoning-only incomplete turns follow the same persistence
                # rule so peer-agent channels don't ingest them as completed
                # assistant turns. (#7100, #51628)
                _user_entry = {
                    "role": "user",
                    "content": (
                        persist_user_message
                        if persist_user_message is not None
                        else message_text
                    ),
                    "timestamp": (
                        persist_user_timestamp
                        if persist_user_timestamp is not None
                        else ts
                    ),
                }
                if persist_user_display_kind:
                    _user_entry["display_kind"] = persist_user_display_kind
                if event.message_id:
                    _user_entry["message_id"] = str(event.message_id)
                # Dedupe: skip if this platform message_id is already in the
                # transcript (prevents duplicate user turns on Telegram retries
                # after transient failures). #47237
                _skip_persist = (
                    event.message_id
                    and await self.async_session_store.has_platform_message_id(
                        session_entry.session_id, str(event.message_id)
                    )
                )
                if _skip_persist:
                    logger.info(
                        "Skipping duplicate user turn "
                        "(message_id=%s) in session %s",
                        event.message_id, session_entry.session_id,
                    )
                else:
                    await self.async_session_store.append_to_transcript(
                        session_entry.session_id,
                        _user_entry,
                        skip_db=agent_persisted,
                    )
            else:
                history_len = agent_result.get("history_offset", len(history))
                new_messages = agent_messages[history_len:] if len(agent_messages) > history_len else []

                # If no new messages found (edge case), fall back to simple user/assistant
                if not new_messages:
                    _user_entry = {
                        "role": "user",
                        "content": (
                            persist_user_message
                            if persist_user_message is not None
                            else message_text
                        ),
                        "timestamp": (
                            persist_user_timestamp
                            if persist_user_timestamp is not None
                            else ts
                        ),
                    }
                    if persist_user_display_kind:
                        _user_entry["display_kind"] = persist_user_display_kind
                    if event.message_id:
                        _user_entry["message_id"] = str(event.message_id)
                    await self.async_session_store.append_to_transcript(
                        session_entry.session_id,
                        _user_entry,
                        skip_db=agent_persisted,
                    )
                    if response:
                        await self.async_session_store.append_to_transcript(
                            session_entry.session_id,
                            {"role": "assistant", "content": response, "timestamp": ts},
                            skip_db=agent_persisted,
                        )
                else:
                    # Attach the inbound platform message_id to the first user
                    # entry written this turn so platform-level quote-resolution
                    # (e.g. Yuanbao QuoteContextMiddleware's transcript fallback)
                    # can find earlier @bot messages by their original message_id.
                    _user_msg_id_attached = False
                    for msg in new_messages:
                        # Skip system messages (they're rebuilt each run)
                        if msg.get("role") == "system":
                            continue
                        # Add timestamp to each message for debugging
                        entry = {**msg, "timestamp": ts}
                        if (
                            not _user_msg_id_attached
                            and msg.get("role") == "user"
                            and event.message_id
                            and "message_id" not in entry
                        ):
                            entry["message_id"] = str(event.message_id)
                            _user_msg_id_attached = True
                        await self.async_session_store.append_to_transcript(
                            session_entry.session_id, entry,
                            skip_db=agent_persisted,
                        )
            
            # Token counts and model are now persisted by the agent directly.
            # Keep only last_prompt_tokens here for context-window tracking and
            # compression decisions.
            await self.async_session_store.update_session(
                session_entry.session_key,
                last_prompt_tokens=agent_result.get("last_prompt_tokens", 0),
                touch_activity=not bool(getattr(event, "internal", False)),
            )

            # Re-baseline the cached agent's message_count snapshot now that
            # ALL of this turn's transcript writes are done — the agent's
            # flushed user/assistant/tool rows AND the first-turn `session_meta`
            # marker appended above.  The cross-process coherence guard (#45966)
            # snapshots the count at agent-BUILD time (before this turn's own
            # writes) and never refreshes it on reuse, so without this the
            # process's own turn grows message_count and the next turn sees a
            # mismatch and rebuilds the agent — destroying prompt caching.
            #
            # This MUST run after the `session_meta` append: that row also
            # increments message_count, so re-baselining before it (the old
            # position) left the snapshot one short and the guard mis-fired on
            # turn 2 of EVERY fresh gateway conversation, rebuilding the cached
            # agent and busting the prompt cache.  Running here also uses the
            # compaction-updated session_id (the agent_result session_id swap
            # above), matching this function's documented contract.  Refreshing
            # here makes the guard fire only on a DIFFERENT process's writes.
            # Fail-safe inside the helper.
            await self._refresh_agent_cache_message_count(
                session_key, session_entry.session_id
            )

            # Intentional silence is a delivery decision, not a transcript
            # mutation.  The agent's [SILENT]/NO_REPLY assistant turn above is
            # still persisted in session history so later turns keep normal
            # user/assistant alternation; only the outbound chat delivery is
            # suppressed.
            if _intentional_silence:
                logger.info(
                    "Suppressing intentional silence marker for session %s",
                    session_entry.session_id,
                )
                response = ""

            # Auto voice reply: send TTS audio before the text response
            _already_sent = bool(agent_result.get("already_sent"))
            # Skip when streaming TTS already delivered audio for this turn (#60671).
            _stts_adapter = self._adapter_for_source(source)
            _streaming_tts_done = (
                _stts_adapter is not None
                and bool(getattr(_stts_adapter, "_streaming_tts_turn_completed", lambda *_a, **_k: False)(session_key, run_generation))
            )
            if (
                not _streaming_tts_done
                and self._should_send_voice_reply(event, response, agent_messages, already_sent=_already_sent)
            ):
                await self._send_voice_reply(event, response)

            # If streaming already delivered the response, extract and
            # deliver any MEDIA: files before returning None.  Streaming
            # sends raw text chunks that include MEDIA: tags — the normal
            # post-processing in _process_message_background is skipped
            # when already_sent is True, so media files would never be
            # delivered without this.
            #
            # Never skip when the agent failed — the error message is new
            # content the user hasn't seen (streaming only sent earlier
            # partial output before the failure).  Without this guard,
            # users see the agent "stop responding without explanation."
            if agent_result.get("already_sent") and not agent_result.get("failed"):
                if response:
                    _media_adapter = self._adapter_for_source(source)
                    if _media_adapter:
                        await self._deliver_media_from_response(
                            response, event, _media_adapter,
                        )
                # Streaming already delivered the body text, but the footer was
                # intentionally held back (see the `not already_sent` gate above).
                # Send it now as a small trailing message so Telegram/Discord/etc.
                # still surface the runtime metadata on the final reply.
                if _footer_line:
                    try:
                        _foot_adapter = self._adapter_for_source(source)
                        if _foot_adapter:
                            await _foot_adapter.send(
                                source.chat_id,
                                _footer_line,
                                metadata=self._thread_metadata_for_source(source, self._reply_anchor_for_event(event)),
                            )
                    except Exception as _e:
                        logger.debug("trailing footer send failed: %s", _e)
                return None

            return response
            
        except Exception as e:
            # Stop typing indicator on error too, retaining Slack thread/workspace
            # routing so a failed turn cannot leave its status visible.
            try:
                _err_adapter = self._adapter_for_source(source)
                _stop_with_metadata = getattr(
                    type(_err_adapter), "_stop_typing_with_metadata", None
                )
                _stop_typing = getattr(type(_err_adapter), "stop_typing", None)
                if _err_adapter and callable(_stop_with_metadata):
                    await _err_adapter._stop_typing_with_metadata(
                        source.chat_id,
                        self._thread_metadata_for_source(
                            source, self._reply_anchor_for_event(event)
                        ),
                    )
                elif _err_adapter and callable(_stop_typing):
                    await _err_adapter.stop_typing(source.chat_id)
            except Exception:
                pass
            logger.exception("Agent error in session %s", session_key)
            # Crash-resilience for failures that happen before AIAgent enters
            # run_conversation() (for example: provider/httpx client init
            # failures). In that path the agent cannot persist the current
            # inbound turn itself, so append the user message here once. If the
            # agent already reached its early turn-start persistence, the latest
            # transcript user row will match and we skip the duplicate.
            try:
                if 'message_text' in locals() and message_text is not None and session_entry is not None:
                    _already_persisted = False
                    try:
                        _recent_transcript = await self.async_session_store.load_transcript(session_entry.session_id)
                    except Exception:
                        _recent_transcript = []
                    for _msg in reversed(_recent_transcript[-10:]):
                        if _msg.get("role") == "user":
                            _expected_user_content = (
                                persist_user_message
                                if persist_user_message is not None
                                else message_text
                            )
                            _already_persisted = (_msg.get("content") == _expected_user_content)
                            break
                    if not _already_persisted:
                        _user_entry = {
                            "role": "user",
                            "content": (
                                persist_user_message
                                if persist_user_message is not None
                                else message_text
                            ),
                            "timestamp": (
                                persist_user_timestamp
                                if persist_user_timestamp is not None
                                else time.time()
                            ),
                        }
                        if 'persist_user_display_kind' in locals() and persist_user_display_kind:
                            _user_entry["display_kind"] = persist_user_display_kind
                        if getattr(event, "message_id", None):
                            _user_entry["message_id"] = str(event.message_id)
                        await self.async_session_store.append_to_transcript(
                            session_entry.session_id,
                            _user_entry,
                        )
            except Exception:
                logger.debug("Failed to persist inbound user message after agent exception", exc_info=True)
            # Log full details server-side only; never expose raw exception
            # types or messages to end users (info-leakage risk).
            status_hint = ""
            status_code = getattr(e, "status_code", None)
            _hist_len = len(history) if 'history' in locals() else 0
            if status_code == 401:
                status_hint = " Check your API key or run `claude /login` to refresh OAuth credentials."
            elif status_code == 402:
                status_hint = " Your API balance or quota is exhausted. Check your provider dashboard."
            elif status_code == 429:
                # Check if this is a plan usage limit (resets on a schedule) vs a transient rate limit
                _err_body = getattr(e, "response", None)
                _err_json = {}
                try:
                    if _err_body is not None:
                        _err_json = _err_body.json().get("error", {})
                        if not isinstance(_err_json, dict):
                            _err_json = {}
                except Exception:
                    pass
                if _err_json.get("type") == "usage_limit_reached":
                    _resets_in = _err_json.get("resets_in_seconds")
                    if _resets_in and _resets_in > 0:
                        import math
                        _hours = math.ceil(_resets_in / 3600)
                        status_hint = f" Your plan's usage limit has been reached. It resets in ~{_hours}h."
                    else:
                        status_hint = " Your plan's usage limit has been reached. Please wait until it resets."
                else:
                    status_hint = " You are being rate-limited. Please wait a moment and try again."
            elif status_code == 529:
                status_hint = " The API is temporarily overloaded. Please try again shortly."
            elif status_code in {400, 500}:
                # 400 with a large session is context overflow.
                # 500 with a large session often means the payload is too large
                # for the API to process — treat it the same way.
                if _hist_len > 50:
                    return (
                        "⚠️ Session too large for the model's context window.\n"
                        "Use /compact to compress the conversation, or "
                        "/reset to start fresh."
                    )
                elif status_code == 400:
                    status_hint = " The request was rejected by the API."
            return (
                f"Sorry, I encountered an unexpected error.{status_hint}\n"
                "Try again or use /reset to start a fresh session."
            )
        finally:
            # Restore session context variables to their pre-handler state
            self._clear_session_env(_session_env_tokens)
