"""Gateway lifecycle / restart / drain methods for GatewayRunner.

Extracted from ``gateway/run.py`` (god-file decomposition campaign, Phase 3
mechanical mixin lifts — ``~/.hermes/plans/god-file-decomposition.md``). This
mixin holds the lifecycle cluster: running-work accounting, scale-to-zero
dormancy, external drain control, platform pause/resume, restart-loop guarding
and failure bookkeeping, detached restart launching, shutdown
notification/finalization, and startup restore (auto-resume of
restart-interrupted sessions).

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
``self.*`` calls resolve unchanged via the MRO. Module-level ``run.py`` helpers
a method needs (``_hermes_home``, ``_load_gateway_config``,
``_resolve_hermes_bin``, ``_parse_session_key``, ``_AGENT_PENDING_SENTINEL``,
``_auto_continue_freshness_window``, ``_startup_restore_drain_timeout_secs``)
are imported lazily inside the method body — a deferred
``from gateway.run import ...`` resolves at call time (``gateway.run`` fully
loaded by then) so this module never imports ``gateway.run`` at import time ->
no import cycle.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from agent.i18n import t
from agent.interrupt_compat import request_hard_interrupt
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
from utils import atomic_json_write

# Match the logger run.py uses (logging.getLogger(__name__) where __name__ ==
# "gateway.run") so extracted log records keep their original logger name.
logger = logging.getLogger("gateway.run")


class GatewayLifecycleMixin:
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

    def _queue_during_drain_enabled(self) -> bool:
        # Both "queue" and "steer" modes imply the user doesn't want messages
        # to be lost during restart — queue them for the newly-spawned gateway
        # process to pick up.  "interrupt" mode drops them (current behaviour).
        return self._restart_requested and self._busy_input_mode in {"queue", "steer"}

    def _enter_external_drain(self) -> None:
        """Begin external drain: stop accepting new turns, flip state.

        Idempotent — re-entering while already draining is a no-op beyond a
        best-effort status re-write. In-flight turns are NOT interrupted (the
        whole point is to let them finish); only NEW turns are refused.
        """
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
        from gateway.run import _AGENT_PENDING_SENTINEL
        for session_key, agent in list(self._running_agents.items()):
            if agent is _AGENT_PENDING_SENTINEL:
                continue
            try:
                request_hard_interrupt(agent, reason)
                logger.debug("Interrupted running agent for session %s during shutdown", session_key)
            except Exception as e:
                logger.debug("Failed interrupting agent during shutdown: %s", e)

    def _update_platform_runtime_status(
        self,
        platform: str,
        *,
        platform_state: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        try:
            from gateway.status import write_runtime_status
            write_runtime_status(
                platform=platform,
                platform_state=platform_state,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception:
            pass

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

    def _scale_to_zero_has_live_background_work(self) -> bool:
        """Live background work that must block a suspend (D3/F7).

        Backgrounded delegate_task / kanban / terminal(background=true) are NOT
        counted by _running_agent_count(), but suspending mid-flight loses them.
        Checks the runner's own tracked tasks + the process registry's running
        processes + any pending process-completion watchers.
        """
        if any(not t.done() for t in self._background_tasks):
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
        """Return ``(max_restarts, window_seconds)`` for the auto-resume
        restart-loop breaker (#30719, defense-3), read from
        ``gateway.restart_loop_guard`` in config.yaml with the module defaults
        as fallback. ``max_restarts <= 0`` disables the breaker.
        """
        from gateway.run import _load_gateway_config
        from gateway import restart_loop_guard as _rlg

        max_restarts = _rlg.DEFAULT_MAX_RESTARTS
        window_seconds = _rlg.DEFAULT_WINDOW_SECONDS
        try:
            user_cfg = _load_gateway_config()
            gw = user_cfg.get("gateway") if isinstance(user_cfg, dict) else None
            rlg = gw.get("restart_loop_guard") if isinstance(gw, dict) else None
            if isinstance(rlg, dict):
                if isinstance(rlg.get("max_restarts"), int):
                    max_restarts = rlg["max_restarts"]
                if isinstance(rlg.get("window_seconds"), int) and rlg["window_seconds"] > 0:
                    window_seconds = rlg["window_seconds"]
        except Exception:  # noqa: BLE001
            pass
        return max_restarts, window_seconds

    def _scale_to_zero_should_arm(self) -> bool:
        """Whether to start the idle watcher (D1/D11/§3.4(1))."""
        from gateway.relay import relay_wake_url
        from gateway.scale_to_zero import (
            messaging_is_relay_only_or_absent,
            scale_to_zero_enabled,
            should_arm,
        )

        try:
            # Only ENABLED platforms count. `config.platforms` is pre-seeded with a
            # disabled placeholder PlatformConfig for every KNOWN platform (telegram,
            # discord, slack, …), so `.keys()` is the full ~20-entry catalog regardless
            # of what this instance actually runs. Passing the bare keys made
            # `messaging_is_relay_only_or_absent` see those placeholders as live
            # direct-socket platforms and return False, so scale-to-zero NEVER armed on
            # a real relay-only instance. Mirror the connect loop, which already gates on
            # `platform_config.enabled` (see the `if not platform_config.enabled: continue`
            # in the adapter-connect loop) — arm off the same notion of "active platform."
            platforms = (
                [p for p, pc in self.config.platforms.items() if getattr(pc, "enabled", False)]
                if self.config
                else []
            )
        except Exception:  # noqa: BLE001
            platforms = []
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
        from gateway.relay import relay_wake_url
        from gateway.scale_to_zero import (
            messaging_is_relay_only_or_absent,
            scale_to_zero_enabled,
        )

        try:
            enabled = scale_to_zero_enabled()
            if not enabled:
                return  # not opted in — normal, stay quiet
            try:
                active = (
                    [
                        getattr(p, "value", p)
                        for p, pc in self.config.platforms.items()
                        if getattr(pc, "enabled", False)
                    ]
                    if self.config
                    else []
                )
            except Exception:  # noqa: BLE001
                active = []
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
        """Watch for idle and drive the relay dormant so the platform can suspend.

        Started ONLY when _scale_to_zero_should_arm() (opted in via the Labs
        HERMES_SCALE_TO_ZERO stamp + relay-only/absent messaging + a wakeUrl).
        On a sustained idle window it runs the DORMANT sequence (D12/F12/F14):
          - mark runtime status `draining` (composes with the existing state
            machine, §3.4(6); does NOT set _running=False),
          - relay adapter.go_dormant() — going_idle->ack + supervisor-preserving
            socket close (NOT disconnect(), NOT the run.py stop path),
          - deliberately NO mark_resume_pending (D13 — suspend preserves RAM).
        The process stays alive; the platform (Fly autostop:"suspend") suspends
        the now-traffic-idle machine and autostart wakes it on the wakeUrl poke,
        at which point the preserved reconnect supervisor re-dials and the
        connector drains the buffered backlog. After driving dormant we set a
        re-arm cooldown so a wake's drained backlog isn't immediately re-quiesced.
        """
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
                    "(relay buffered, socket closed, awaiting platform suspend)",
                    self._scale_to_zero_idle_timeout_seconds(),
                )
                try:
                    self._update_runtime_status("draining")
                except Exception:  # noqa: BLE001 - status is best-effort
                    logger.debug("scale-to-zero: status mark failed", exc_info=True)
                try:
                    result = go_dormant()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:  # noqa: BLE001 - dormancy is best-effort
                    logger.debug("scale-to-zero: go_dormant failed", exc_info=True)
                # 0.F: after a wake the drained inbound updates _last_inbound_at,
                # but give it a window so we don't immediately re-go-dormant on the
                # same idle reading before traffic lands.
                self._scale_to_zero_cooldown_until = time.time() + max(interval, 60.0)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the watcher must never crash the gateway
                logger.debug("scale-to-zero watcher iteration error", exc_info=True)

    def _snapshot_running_agents(self) -> Dict[str, Any]:
        from gateway.run import _AGENT_PENDING_SENTINEL
        return {
            session_key: agent
            for session_key, agent in self._running_agent_items()
            if agent is not _AGENT_PENDING_SENTINEL
        }

    def _increment_restart_failure_counts(self, active_session_keys: set) -> None:
        """Increment restart-failure counters for sessions active at shutdown.

        Persists to a JSON file so counters survive across restarts.
        Sessions NOT in active_session_keys are removed (they completed
        successfully, so the loop is broken).
        """
        from gateway.run import _hermes_home
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
        from gateway.run import _hermes_home
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

    def _clear_restart_failure_count(self, session_key: str) -> None:
        """Clear the restart-failure counter for a session that completed OK.

        Called after a successful agent turn to signal the loop is broken.
        """
        from gateway.run import _hermes_home
        import json

        path = _hermes_home / self._STUCK_LOOP_FILE
        if not path.exists():
            return
        try:
            counts = json.loads(path.read_text(encoding="utf-8"))
            if session_key in counts:
                del counts[session_key]
                if counts:
                    atomic_json_write(path, counts, indent=None)
                else:
                    path.unlink(missing_ok=True)
        except Exception:
            pass

    async def _launch_detached_restart_command(self) -> None:
        from gateway.run import _resolve_hermes_bin
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

    async def _await_active_work_before_restart(self) -> bool:
        """Wait for in-flight work to finish before entering ``stop()``.

        In-band restart used to call ``stop()`` immediately, which folded the
        requesting turn into the drain wait set and force-interrupted it at
        ``restart_drain_timeout`` (#77184). Instead we refuse new turns and
        wait here for active agents/cron/api work to reach zero, then let
        ``stop()`` run against an idle gateway (drain is instant).

        Returns True when work drained to zero, False when the safety cap
        elapsed with work still active (caller proceeds to ``stop()``, which
        may then interrupt remaining runs under ``restart_drain_timeout``).
        """
        active = self._active_work_count()
        if active <= 0:
            return True

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
        while self._active_work_count() > 0:
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
                    "(%.0fs remaining before force drain)",
                    self._active_work_count(),
                    deadline - now,
                )
                try:
                    self._update_runtime_status("draining")
                except Exception:
                    pass
                last_status_at = now
            await asyncio.sleep(0.1)

        logger.info(
            "Restart deferred wait complete — active work drained; "
            "proceeding to stop()"
        )
        return True

    def request_restart(self, *, detached: bool = False, via_service: bool = False) -> bool:
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

    async def _notify_active_sessions_of_shutdown(self) -> None:
        """Send shutdown/restart notifications to active chats and home channels.

        Called at the very start of stop() — adapters are still connected so
        messages can be delivered. Best-effort: individual send failures are
        logged and swallowed so they never block the shutdown sequence.
        """
        from gateway.run import _parse_session_key
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
        from gateway.run import _startup_restore_drain_timeout_secs
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
        from gateway.run import _AGENT_PENDING_SENTINEL, _auto_continue_freshness_window
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

                _max_restarts, _window = self._restart_loop_guard_config()
                if _rlg.check_and_record(_max_restarts, _window):
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
