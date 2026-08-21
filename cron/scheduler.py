"""
Cron job scheduler - executes due jobs.

Provides tick() which checks for due jobs and runs them. The gateway
calls this every 60 seconds from a background thread.

Uses a file-based lock (~/.hermes/cron/.tick.lock) so only one tick
runs at a time if multiple processes overlap.
"""

import asyncio
import atexit
import concurrent.futures
import contextvars
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from contextlib import contextmanager

# fcntl is Unix-only; on Windows use msvcrt for file locking
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add parent directory to path for imports BEFORE repo-level imports.
# Without this, standalone invocations (e.g. after `hermes update` reloads
# the module) fail with ModuleNotFoundError for hermes_time et al.
sys.path.insert(0, str(Path(__file__).parent.parent))

from cron import wake_channel
from hermes_constants import get_hermes_home
from hermes_cli._subprocess_compat import resolve_windows_git_bash, run_text_capture
from hermes_cli.config import load_config, _expand_env_vars
from hermes_cli.fallback_config import get_fallback_chain
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

# OTel — defensive; if obs/ or opentelemetry isn't installed, cron still runs.
try:
    from obs import get_tracer as _obs_get_tracer  # noqa: E402
    _CRON_TRACER = _obs_get_tracer("hermes.cron")
except Exception:  # pragma: no cover
    _CRON_TRACER = None

# SR-535 remainder: Langfuse grouping attributes. Imported separately from
# the tracer so version skew degrades to a no-op stamp, not lost tracing.
try:
    from obs.otel_tracing import stamp_langfuse_attributes as _stamp_langfuse  # noqa: E402
except Exception:  # pragma: no cover
    def _stamp_langfuse(span, **kwargs):  # type: ignore[misc]
        pass


class _NoopCronSpan:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def set_attribute(self, *a, **kw): pass
    def record_exception(self, *a, **kw): pass
    def set_status(self, *a, **kw): pass
    def add_event(self, *a, **kw): pass


def _start_cron_span(name: str):
    if _CRON_TRACER is None:
        return _NoopCronSpan()
    try:
        return _CRON_TRACER.start_as_current_span(name)
    except Exception:
        return _NoopCronSpan()


def _stamp_cron_langfuse(span, job: dict, job_name: str) -> None:
    """SR-535 remainder: Langfuse grouping attributes for a cron fire span.

    session.id = ``<job name>:<YYYY-MM-DD>`` so every fire of one job on
    one day groups as a single Langfuse session; user.id = the job's
    runtime profile (per-job override, else the scheduler's active
    profile); tags = [subsystem, profile] for UI filtering. Best-effort —
    never raises.
    """
    profile = str(job.get("profile") or "").strip()
    if not profile:
        try:
            from hermes_cli.profiles import get_active_profile_name
            profile = get_active_profile_name()
        except Exception:
            profile = "default"
    try:
        date_str = _hermes_now().date().isoformat()
        session_id = f"{job_name}:{date_str}"
    except Exception:
        session_id = job_name
    _stamp_langfuse(
        span,
        session_id=session_id,
        user_id=profile,
        tags=["cron", profile],
    )


def _set_cron_session_title(session_db, session_id, base_title):
    """Robustly title a finished cron session before it is closed.

    Centralizes the title write so the cron finally block can guarantee a
    non-blank, unique title is persisted before end_session()/close() tear
    the connection down (issues #50535, #50536, #50537):

    - #50535: never leaves the session blank. base_title already carries a
      cron-id fallback for nameless jobs; this also guards a failed write.
    - #50537: a duplicate title makes set_session_title raise ValueError (the
      unique-title index). Recover by appending a #N suffix via
      get_next_title_in_lineage() when supported, instead of swallowing the
      error and ending up untitled. If lineage dedup is unavailable, raise.
    - #50536: this runs synchronously in the cron finally block ahead of the
      session close, so no in-flight title write can race the close.

    Returns the title actually persisted, or None if nothing could be set.
    """
    if not session_db or not session_id:
        return None
    title = (base_title or "").strip()
    if not title:
        return None
    try:
        session_db.set_session_title(session_id, title)
        return title
    except ValueError:
        # Title collision against the unique-title index. Fall back to the
        # next title in the lineage (base #2, base #3, ...) when supported.
        next_title_fn = getattr(session_db, "get_next_title_in_lineage", None)
        if next_title_fn is None:
            raise
        deduped = next_title_fn(title)
        if not deduped or deduped == title:
            raise
        session_db.set_session_title(session_id, deduped)
        return deduped


def _summarize_cron_failure_for_delivery(job: dict, error: str | None) -> str:
    """Return a compact one-line failure message for chat delivery.

    Full details stay in the cron output directory and the logs. Chat should
    show the operator what broke without dumping provider JSON, retry noise, or
    stack traces into the delivery channel.
    """
    job_name = job.get("name") or job.get("id") or "cron job"
    text = (error or "unknown error").strip()
    lower = text.lower()

    # Provider/API failures are the common noisy path. Keep these short.
    if "429" in text or "rate limit" in lower or "usage limit" in lower:
        reason = "rate limit"
        if "weekly usage limit" in lower:
            reason = "weekly usage limit"
        elif "quota" in lower:
            reason = "quota limit"
        return (
            f"⚠️ Cron '{job_name}' failed: provider {reason}. "
            "Fallback chain was exhausted or unavailable. "
            "Full details saved in cron output."
        )

    if "readtimeout" in lower or "timed out" in lower or "timeout" in lower:
        return (
            f"⚠️ Cron '{job_name}' failed: provider timeout. "
            "Fallback chain was exhausted or unavailable. "
            "Full details saved in cron output."
        )

    # Match authentication/authorization wording at a word boundary and the
    # 401/403 status codes as whole tokens, so "oauth", "4015" and similar do
    # not trip a misleading auth message.
    if re.search(r"authenticat|authoriz", lower) or re.search(r"\b(401|403)\b", text):
        return (
            f"⚠️ Cron '{job_name}' failed: provider authentication error. "
            "Full details saved in cron output."
        )

    # Strip common exception wrappers and collapse provider payloads. Bound
    # the input first so a multi-KB provider blob cannot slow the
    # substitutions.
    cleaned = re.sub(
        r"^(RuntimeError|Exception|ValueError|HTTPStatusError):\s*",
        "", text[:2000],
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 180:
        cleaned = cleaned[:177].rstrip() + "..."
    return f"⚠️ Cron '{job_name}' failed: {cleaned}"


class CronPromptInjectionBlocked(Exception):
    """Raised by _build_job_prompt when the fully-assembled prompt trips the
    injection scanner. Caught in run_job so the operator sees a clean
    "job blocked" delivery instead of the scheduler crashing.

    Assembled-prompt scanning (including loaded skill content) plugs the
    gap from #3968: create-time scanning only covers the user-supplied
    prompt field; skill content loaded at runtime was never scanned, so a
    malicious skill could carry an injection payload that reached the
    non-interactive (auto-approve) cron agent.
    """


def _resolve_cron_disabled_toolsets(cfg: dict) -> list[str]:
    """Toolsets a cron-spawned agent must never receive.

    Three protected toolsets are always disabled in cron context:
      - ``cronjob`` — would let a cron-spawned agent schedule more cron jobs
      - ``messaging`` — interactive, needs a live gateway session
      - ``clarify`` — interactive, blocks waiting for user input

    User-level ``agent.disabled_toolsets`` from config.yaml is layered on top
    so per-job ``enabled_toolsets`` cannot bypass policy that applies to
    ordinary agent runs (#25752 — LLM-supplied enabled_toolsets was widening
    past config.yaml's denylist).
    """
    disabled = ["cronjob", "messaging", "clarify"]
    agent_cfg = (cfg or {}).get("agent") or {}
    user_disabled = agent_cfg.get("disabled_toolsets") or []
    for name in user_disabled:
        name = str(name).strip()
        if name and name not in disabled:
            disabled.append(name)
    return disabled


def _merge_mcp_into_per_job_toolsets(per_job: list[str], cfg: dict) -> list[str]:
    """Layer enabled MCP servers onto a per-job ``enabled_toolsets`` allowlist.

    A per-job list scopes the *native* toolsets, but on its own it silently
    drops every MCP server: ``discover_mcp_tools()`` registers the tools into
    the global registry, yet ``get_tool_definitions(enabled_toolsets=...)``
    only keeps toolsets named in the list. The agent then rejects every
    ``mcp_*`` call with "Unknown tool". This restores parity with
    ``_get_platform_tools`` MCP semantics:

      * ``no_mcp`` sentinel present  -> no MCP servers (sentinel stripped)
      * one or more MCP server names already listed -> treat as an allowlist,
        add nothing further (the user named exactly the servers they want)
      * otherwise -> union in every globally-enabled MCP server
    """
    result = [t for t in per_job if t != "no_mcp"]
    if "no_mcp" in per_job:
        return result
    # lazy import: avoid heavy hermes_cli import at cron module load (matches
    # _resolve_cron_enabled_toolsets' fallback) and share one MCP-membership
    # computation with the gateway/CLI platform resolver.
    from hermes_cli.tools_config import enabled_mcp_server_names
    enabled_mcp = enabled_mcp_server_names(cfg)
    if set(result) & enabled_mcp:
        return result
    for name in sorted(enabled_mcp):
        if name not in result:
            result.append(name)
    return result


def _resolve_cron_enabled_toolsets(job: dict, cfg: dict) -> list[str] | None:
    """Resolve the toolset list for a cron job.

    Precedence:
    1. Per-job ``enabled_toolsets`` (set via ``cronjob`` tool on create/update).
       Keeps the agent's job-scoped toolset override intact — #6130. Enabled
       MCP servers are layered on per ``_merge_mcp_into_per_job_toolsets`` so a
       native-toolset allowlist does not silently strip MCP tools.
    2. Per-platform ``hermes tools`` config for the ``cron`` platform.
       Mirrors gateway behavior (``_get_platform_tools(cfg, platform_key)``)
       so users can gate cron toolsets globally without recreating every job.
    3. ``None`` on any lookup failure — AIAgent loads the full default set
       (legacy behavior before this change, preserved as the safety net).

    _DEFAULT_OFF_TOOLSETS ({moa, homeassistant, rl}) are removed by
    ``_get_platform_tools`` for unconfigured platforms, so fresh installs
    get cron WITHOUT ``moa`` by default (issue reported by Norbert —
    surprise $4.63 run).
    """
    per_job = job.get("enabled_toolsets")
    if per_job:
        return _merge_mcp_into_per_job_toolsets(list(per_job), cfg or {})
    try:
        from hermes_cli.tools_config import _get_platform_tools  # lazy: avoid heavy import at cron module load
        return sorted(_get_platform_tools(cfg or {}, "cron"))
    except Exception as exc:
        logger.warning(
            "Cron toolset resolution failed, falling back to full default toolset: %s",
            exc,
        )
        return None

# Valid delivery platforms — used to validate user-supplied platform names
# in cron delivery targets, preventing env var enumeration via crafted names.
_KNOWN_DELIVERY_PLATFORMS = frozenset({
    "telegram", "discord", "slack", "whatsapp", "signal",
    "matrix", "mattermost", "homeassistant", "dingtalk", "feishu",
    "wecom", "wecom_callback", "weixin", "sms", "email", "webhook", "bluebubbles",
    "qqbot", "yuanbao",
})

# Platforms that support a configured cron/notification home target, mapped to
# the environment variable used by gateway setup/runtime config.
_HOME_TARGET_ENV_VARS = {
    "matrix": "MATRIX_HOME_ROOM",
    "telegram": "TELEGRAM_HOME_CHANNEL",
    "discord": "DISCORD_HOME_CHANNEL",
    "slack": "SLACK_HOME_CHANNEL",
    "signal": "SIGNAL_HOME_CHANNEL",
    "mattermost": "MATTERMOST_HOME_CHANNEL",
    "sms": "SMS_HOME_CHANNEL",
    "email": "EMAIL_HOME_ADDRESS",
    "dingtalk": "DINGTALK_HOME_CHANNEL",
    "feishu": "FEISHU_HOME_CHANNEL",
    "wecom": "WECOM_HOME_CHANNEL",
    "weixin": "WEIXIN_HOME_CHANNEL",
    "bluebubbles": "BLUEBUBBLES_HOME_CHANNEL",
    "qqbot": "QQBOT_HOME_CHANNEL",
    "whatsapp": "WHATSAPP_HOME_CHANNEL",
    "whatsapp_cloud": "WHATSAPP_CLOUD_HOME_CHANNEL",
}

# Legacy env var names kept for back-compat.  Each entry is the current
# primary env var → the previous name.  _get_home_target_chat_id falls
# back to the legacy name if the primary is unset, so users who set the
# old name before the rename keep working until they migrate.
_LEGACY_HOME_TARGET_ENV_VARS = {
    "QQBOT_HOME_CHANNEL": "QQ_HOME_CHANNEL",
}

from cron.jobs import (
    advance_next_run,
    claim_dispatch,
    emit_cron_triggered_safe,
    get_due_and_skipped_jobs,
    # Unused here, but tests/cron/test_scheduler.py does
    # patch("cron.scheduler.get_due_jobs"); patching a name this module
    # never bound raises AttributeError.
    get_due_jobs,  # noqa: F401
    heartbeat_run_claim,
    load_jobs,
    mark_job_run,
    amend_late_outcome_after_abandon,
    save_job_output,
)
from cron.executions import (
    amend_execution_after_abandon,
    create_execution,
    finish_execution,
    mark_execution_running,
)

# Sentinel: when a cron agent has nothing new to report, it can start its
# response with this marker to suppress delivery.  Output is still saved
# locally for audit.
SILENT_MARKER = "[SILENT]"
CRON_EVENT_SUMMARY_MAX_CHARS = 4000

# Event bus integration (lazy-loaded to avoid circular imports)
_event_emitter = None

def _get_event_emitter():
    """Lazy-load the CronEventEmitter.

    Prefers the gateway's shared EventBus.  When this process is NOT the
    gateway — ``get_bus()`` is None because ``gateway_integration.startup()``
    never ran here (e.g. ``hermes serve``/the TUI web server, the dashboard,
    or a CLI cron run) — fall back to a process-local EventBus on the
    canonical events DB.  SQLite WAL + busy_timeout makes cross-process
    writes safe, and FailureClusterDetector's state file was already
    designed for the cross-process case.

    Before 2026-07-16 the no-gateway-bus case cached the ``False`` sentinel
    forever, so any non-gateway process that won the jobs.json claim races
    ran jobs normally while silently dropping every cron_started/
    cron_completed/cron_failed emit — the multi-hour "emission dark
    windows" that made watchdog_sweep raise false silence alarms.
    """
    global _event_emitter
    if _event_emitter is None:
        try:
            from events.gateway_integration import get_bus
            from events.bus import EventBus
            from events.producers.cron_emitter import CronEventEmitter
            bus = get_bus()
            if bus is None:
                bus = EventBus()
                logger.info(
                    "Cron event emitter: gateway bus not initialized in this "
                    "process; using direct EventBus at %s", bus.db_path,
                )
            _event_emitter = CronEventEmitter(bus)
        except Exception as e:
            logger.warning(
                "Cron event emitter unavailable — cron lifecycle events "
                "will NOT be recorded by this process: %s", e,
            )
            _event_emitter = False  # sentinel: don't retry
    return _event_emitter if _event_emitter else None

# Canonical silence tokens recognized in cron output.  Cron's contract is
# intentionally looser than the gateway's exact-whole-response rule: the cron
# system prompt *instructs* the agent to emit "[SILENT]", and real agents often
# bracket it with a short note or trailing newline.  We therefore suppress when
# a marker is the entire response OR appears as its own first/last line — but
# NOT when a token merely appears mid-sentence in a genuine report (e.g.
# "I considered staying [SILENT] but here is the summary…" must deliver).
_CRON_SILENCE_TOKENS = frozenset({"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"})


def _is_cron_silence_response(text: str) -> bool:
    """Return True when a cron final response should suppress delivery.

    Recognizes the bracketed ``[SILENT]`` sentinel (whole-response, first line,
    or last line) plus the bracketless ``SILENT`` / ``NO_REPLY`` / ``NO REPLY``
    variants the model emits when it drops the brackets (#51438, #46917).
    Whitespace-trimmed and case-insensitive.  A token buried mid-sentence is
    treated as real content and delivered.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False

    def _is_token(line: str) -> bool:
        return " ".join(line.strip().upper().split()) in _CRON_SILENCE_TOKENS

    # Whole response is exactly a token.
    if _is_token(stripped):
        return True
    # Marker on its own first or last line (trailing/leading note on a
    # separate line — e.g. "2 deals filtered\n\n[SILENT]").
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if lines and (_is_token(lines[0]) or _is_token(lines[-1])):
        return True
    # Bracketed sentinel used as a same-line prefix — the documented cron
    # pattern "[SILENT] No changes detected".  Restricted to the bracketed
    # form so a bare word like "Silent retry succeeded" is NOT swallowed.
    upper = stripped.upper()
    if upper.startswith("[SILENT]"):
        return True
    return False

# ---------------------------------------------------------------------------
# Persistent thread pool for parallel cron jobs.
# The tick function submits jobs here and returns immediately so the ticker
# thread is never blocked by long-running jobs (e.g. the fixer running 15+ min).
# ---------------------------------------------------------------------------
_parallel_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
_parallel_pool_max_workers: Optional[int] = None
_running_job_ids: set = set()
_running_lock = threading.Lock()

# Job IDs the gateway shutdown path force-killed the tool subprocess of
# while still in ``_running_job_ids`` (see ``mark_running_jobs_interrupted``
# below). ``run_one_job``'s own completion path checks this set before
# writing its own ``last_status`` so a cron agent thread that keeps running
# in-process after its tool was killed out from under it — and produces a
# plausible-looking final response from truncated output — can never
# overwrite the interrupted status with a false "ok" (#60432).
_interrupted_job_ids: set = set()


def get_running_job_ids() -> "frozenset[str]":
    """Thread-safe snapshot of cron job IDs currently executing.

    A job ID is a member from the moment ``_submit_with_guard`` dispatches
    it onto the parallel/sequential pool until ``_process_job`` returns —
    i.e. for the job's *entire* run, tool calls included, not just the
    ticker's dispatch instant.

    The gateway shutdown path (``gateway/run.py::GatewayRunner.
    _drain_active_agents``) reads this to treat in-flight cron work as
    active the same way it already treats in-flight chat sessions via
    ``_running_agents`` — cron jobs run through their own thread pool here,
    entirely outside that dict, so without this the drain is structurally
    blind to them (#60432).
    """
    with _running_lock:
        return frozenset(_running_job_ids)


def mark_running_jobs_interrupted(reason: str) -> list:
    """Best-effort: mark every currently in-flight cron job interrupted.

    Called by the gateway shutdown path immediately after it force-kills
    tool subprocesses (``process_registry.kill_all()``). A job whose tool
    subprocess was just killed out from under it must never be allowed to
    report success — even though its agent thread is still alive in this
    same process and may go on to produce a plausible-looking final
    response from the now-truncated tool output.

    Records the job IDs in ``_interrupted_job_ids`` BEFORE writing
    ``last_status`` so ``run_one_job``'s own eventual completion for the
    same job (racing in its own thread) sees the flag and skips its normal
    write instead of clobbering this one — see the check near the end of
    ``run_one_job``. This does not attempt to correlate the killed
    subprocess PID to a specific job ID (the process registry tracks PIDs,
    not cron job IDs); any job still dispatched at the moment of a forced
    kill is treated as interrupted, matching the coarser precedent already
    set by ``GatewayRunner._interrupt_running_agents``, which interrupts
    every entry in ``_running_agents`` on a drain timeout without
    per-agent correlation either.

    Returns the list of job IDs marked, for the caller to log.
    """
    with _running_lock:
        job_ids = list(_running_job_ids)
        _interrupted_job_ids.update(job_ids)
    marked = []
    for job_id in job_ids:
        try:
            mark_job_run(job_id, False, reason)
            marked.append(job_id)
        except Exception as e:
            logger.warning("Failed to mark job %s interrupted: %s", job_id, e)
    return marked


def _is_interrupted(job_id: str) -> bool:
    """Non-destructive peek at whether the shutdown path has marked
    ``job_id`` interrupted (see ``mark_running_jobs_interrupted``).

    Called by ``run_one_job`` BEFORE it decides what to deliver — a job
    whose tool subprocess was killed mid-flight may still produce a
    plausible-looking ``final_response`` from the truncated output, and
    that must not go out to the user as if it were a normal result.
    Unlike ``_consume_interrupted_flag`` below, this does not clear the
    flag: the later, authoritative check (right before ``last_status`` is
    written) still needs to see it."""
    with _running_lock:
        return job_id in _interrupted_job_ids


def _consume_interrupted_flag(job_id: str) -> bool:
    """Return True and clear the flag if the shutdown path already marked
    ``job_id`` interrupted (see ``mark_running_jobs_interrupted``).

    Called by ``run_one_job`` right before it would otherwise write its own
    ``last_status``. Consuming (discarding) rather than just checking keeps
    the flag from leaking across a later, unrelated run of the same job ID
    (recurring jobs reuse their ID every fire)."""
    with _running_lock:
        if job_id in _interrupted_job_ids:
            _interrupted_job_ids.discard(job_id)
            return True
        return False


# Sequential (env-mutating) cron jobs — workdir jobs that touch
# process-global runtime state — must run one at a time, but must NOT block the
# ticker thread.  A persistent single-thread executor preserves ordering across
# ticks while keeping dispatch fire-and-forget, the same as the parallel pool.
_sequential_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None


class _ReadWriteLock:
    """Writer-preferring readers-writer lock.

    Guards the process-global ``os.environ["TERMINAL_CWD"]`` override that a
    workdir cron job applies for the whole of its agent run.  Workdir jobs are
    writers: they mutate the shared env and need exclusive access.  Workdir-less
    jobs are readers: they only observe ``TERMINAL_CWD`` (indirectly, via the
    terminal / file / code-exec tools), so any number of them may run
    concurrently with each other, but none may run alongside a writer — that is
    exactly what stops a workdir-less job from picking up another job's workdir
    override and running its commands in the wrong directory.

    Writer preference bounds the wait for a workdir job (dispatched on the
    single-thread sequential pool) so a stream of workdir-less readers cannot
    starve it.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer_active = False
        self._writers_waiting = 0

    def acquire_read(self) -> None:
        with self._cond:
            while self._writer_active or self._writers_waiting > 0:
                self._cond.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def acquire_write(self) -> None:
        with self._cond:
            self._writers_waiting += 1
            try:
                while self._writer_active or self._readers > 0:
                    self._cond.wait()
            finally:
                self._writers_waiting -= 1
            self._writer_active = True

    def release_write(self) -> None:
        with self._cond:
            self._writer_active = False
            self._cond.notify_all()


# Serializes a job's process-global overrides against every other concurrently
# running cron job.  See _ReadWriteLock and run_job for the usage contract.
_terminal_cwd_lock = _ReadWriteLock()


def _job_mutates_process_globals(job: dict) -> bool:
    """True when this job overrides process-global state for its whole run.

    SINGLE SOURCE OF TRUTH.  Two kinds of job do this and they must be treated
    identically by the tick() sequential/parallel partition AND by the
    readers-writer lock:

    * ``workdir`` jobs override ``os.environ["TERMINAL_CWD"]``.
    * ``profile`` jobs go through :func:`_job_profile_context`, which sets the
      module-global ``_hermes_home``, installs a Hermes-home override, and
      snapshots/restores ``os.environ``.  While one is active
      ``_get_hermes_home()`` returns that profile's home for EVERY thread.

    The two classifications were written separately and drifted: the partition
    tested ``workdir or profile`` while the lock tested ``workdir`` alone.  A
    profile job therefore went to the sequential pool (which only stops
    sequential jobs overlapping EACH OTHER) while taking the lock as a READER,
    so the parallel pool kept running beside it and those jobs resolved their
    ``script:`` slot under the wrong profile.

    Not hypothetical: 8 "Script not found" failures across 6 jobs on
    2026-08-19/20, every one under ``profiles/financier`` and every one
    concurrent with a financier-profile job.  They fail SILENTLY -- the job's
    ``last_status`` stays ``ok`` because the LLM addendum runs fine on empty
    input -- so nothing downstream could see it.
    """
    return bool(
        (job.get("workdir") or "").strip() or (job.get("profile") or "").strip()
    )


def _get_parallel_pool(max_workers: Optional[int]) -> concurrent.futures.ThreadPoolExecutor:
    """Return (or create) the persistent parallel pool."""
    global _parallel_pool, _parallel_pool_max_workers
    if _parallel_pool is None or _parallel_pool_max_workers != max_workers:
        if _parallel_pool is not None:
            _parallel_pool.shutdown(wait=False, cancel_futures=False)
        _parallel_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="cron-parallel",
        )
        _parallel_pool_max_workers = max_workers
    return _parallel_pool


def _get_sequential_pool() -> concurrent.futures.ThreadPoolExecutor:
    """Return (or create) the persistent single-thread sequential pool.

    A single worker guarantees env-mutating jobs never overlap, even
    across ticks: a job queued by a newer tick waits for the previous tick's
    sequential jobs to finish rather than corrupting their os.environ
    state.
    """
    global _sequential_pool
    if _sequential_pool is None:
        _sequential_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="cron-seq",
        )
    return _sequential_pool


def _shutdown_parallel_pool() -> None:
    """Shut down the persistent pools on process exit."""
    global _parallel_pool, _parallel_pool_max_workers, _sequential_pool
    if _parallel_pool is not None:
        _parallel_pool.shutdown(wait=True, cancel_futures=False)
        _parallel_pool = None
        _parallel_pool_max_workers = None
    if _sequential_pool is not None:
        _sequential_pool.shutdown(wait=True, cancel_futures=False)
        _sequential_pool = None


atexit.register(_shutdown_parallel_pool)


def _interpreter_shutting_down(exc: Optional[BaseException] = None) -> bool:
    """True when the Python interpreter is finalizing.

    A cron tick can fire while the gateway is tearing down — SIGTERM from
    ``hermes update`` / ``hermes gateway stop`` / systemd restart, or an
    OOM-kill. Once finalization starts, ``concurrent.futures`` refuses new
    work with ``RuntimeError: cannot schedule new futures after interpreter
    shutdown`` and asyncio's default executor is gone, so *any* attempt to
    schedule delivery (live-adapter, ``asyncio.run``, or a fresh pool) is
    doomed and only pollutes ``errors.log`` with a traceback. Callers use
    this to skip gracefully with a warning instead of crashing (#58720,
    #55924).

    ``exc`` lets a caller also treat an already-raised scheduling error as a
    shutdown signal: the ``concurrent.futures`` module-global flag can be set
    a hair before ``sys.is_finalizing()`` flips, so matching the error text is
    a safe fallback for that race.
    """
    if sys.is_finalizing():
        return True
    if exc is not None:
        # Match the SHORT prefix deliberately: CPython emits two shutdown
        # variants — "cannot schedule new futures after interpreter shutdown"
        # (asyncio.run_coroutine_threadsafe / a torn-down default executor) and
        # "cannot schedule new futures after shutdown" (a plain
        # ThreadPoolExecutor). Both are documented in #58720. The common prefix
        # catches both; the sibling agent/tool_executor._is_interpreter_shutdown_submit_error
        # matches only the fuller "...after interpreter shutdown" form.
        return "cannot schedule new futures" in str(exc).lower()
    return False


# Backward-compatible module override used by tests and emergency monkeypatches.
_hermes_home: Path | None = None


def _get_hermes_home() -> Path:
    """Resolve Hermes home dynamically while preserving test monkeypatch hooks.

    Cron is per-profile by design (#4707): the in-process ticker runs inside a
    profile-scoped gateway, so resolving the active HERMES_HOME at call time
    means a profile's jobs are stored AND executed under that profile's home
    (its .env, config.yaml, scripts, skills). Do not freeze this at import or
    anchor it at the shared default root — either re-breaks profile isolation.
    """
    return _hermes_home or get_hermes_home()


def _get_lock_paths() -> tuple[Path, Path]:
    """Resolve cron lock paths at call time so profile/env changes are honored."""
    hermes_home = _get_hermes_home()
    lock_dir = hermes_home / "cron"
    return lock_dir, lock_dir / ".tick.lock"


# === Same-job concurrency guard (Guard #3, added 2026-04-30) ================
#
# Closes the 2026-04-30 sentinel-vip-morning triple-fire (canonical case
# event_id 4edcb4b1-aa07-4dbb-b799-8af167d4f92e). The cron tick path is
# protected by `advance_next_run` so duplicates from the recurring schedule
# cannot land within a single tick window — but `trigger_job(job_id)` in
# cron/jobs.py:571 sets next_run_at = NOW and bypasses that guard. When a
# user-initiated trigger races a tick-scheduled fire (or a fire that is
# wedged), two `_process_job` calls can land for the same job_id. For
# Sentinel that means two Chrome browser-harness processes colliding on
# the per-process lock; for any cron it means duplicate LLM credits, mailbox
# writes, and AGENT_ITERATION events.
#
# Registry shape (also intended to support Guard #1, cron_aborted on
# shutdown, which needs to enumerate currently-running jobs):
#   _in_flight[job_id] = _InFlightRecord(
#       start_monotonic=...,           # time.monotonic() at registration
#       job_name="...",                 # for event payloads
#       cron_started_event_id=...       # populated after on_job_started succeeds
#   )
# All mutations go through `_in_flight_lock`. Any future branch that adds
# fields here must update the dataclass below AND the helpers used by
# _process_job (`_register_in_flight`, `_release_in_flight`).
@dataclass
class _InFlightRecord:
    start_monotonic: float
    job_name: str
    cron_started_event_id: Optional[str] = None


_in_flight: Dict[str, _InFlightRecord] = {}
_in_flight_lock = threading.Lock()

# Default guard threshold (seconds) used when HERMES_CRON_HARD_TIMEOUT is
# unset.  This is the duplicate-guard's view of "how long before we treat
# the prior fire as wedged"; it is intentionally separate from the
# scheduler's wall-clock timeout (which defaults to unlimited) so that
# the dedup guard works correctly even in environments that have not
# opted into a wall-clock timeout.
_DEFAULT_DUP_GUARD_TIMEOUT_S = 1800.0


def _dup_guard_timeout_seconds() -> float:
    """Effective elapsed-time threshold the dedup guard uses to decide
    whether the prior fire is healthy (concurrent_fire_blocked) or
    wedged-but-tracked (prior_fire_exceeded_timeout)."""
    raw = os.getenv("HERMES_CRON_HARD_TIMEOUT", "").strip()
    try:
        val = float(raw) if raw else 0.0
    except (TypeError, ValueError):
        val = 0.0
    return val if val > 0 else _DEFAULT_DUP_GUARD_TIMEOUT_S


# === Min-interval-since-last-fire guard (Guard #4, 2026-04-30 follow-up) ====
#
# Closes the SEQUENTIAL-burst gap left by Guard #3.  Guard #3 only catches
# CONCURRENT re-fires (where one is still in-flight when the next arrives).
# The 2026-04-30 sentinel-vip-morning fires at 14:02 / 14:34 / 14:49 UTC
# were spaced 28 min and 13 min apart -- each prior fire had already
# released its in-flight slot before the next arrived, so Guard #3 never
# engaged.  This guard rejects a tick-time fire when the job's last_run_at
# is within `min_seconds_between_fires` of NOW, regardless of whether the
# prior fire was tick-scheduled or trigger-scheduled.
#
# Default off (per-job opt-in via the field in jobs.json, with a global
# env override).  Recommended setting for sentinel-vip-* jobs is 1800
# (30 min) -- see sentinel-vip-burst-rc-2026-04-30.md §6.
def _job_min_seconds_between_fires(job: dict) -> int:
    """Resolve the effective min-interval-between-fires for ``job``.

    Priority: per-job ``min_seconds_between_fires`` field >
    ``HERMES_CRON_MIN_SECONDS_BETWEEN_FIRES`` env var > 0 (off).

    Returns 0 when the guard is disabled for this job; the guard then
    short-circuits without touching ``last_run_at``.
    """
    per_job = job.get("min_seconds_between_fires")
    if per_job is not None:
        try:
            return max(0, int(per_job))
        except (TypeError, ValueError):
            pass
    raw = os.getenv("HERMES_CRON_MIN_SECONDS_BETWEEN_FIRES", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            pass
    return 0


def _try_register_in_flight(job_id: str, job_name: str) -> Optional[_InFlightRecord]:
    """Attempt to register a job as in-flight.

    Returns ``None`` if the slot was acquired (caller may proceed).
    Returns the existing ``_InFlightRecord`` if a duplicate fire is
    already running (caller must reject and emit cron_skipped_duplicate).
    """
    with _in_flight_lock:
        prior = _in_flight.get(job_id)
        if prior is not None:
            return prior
        _in_flight[job_id] = _InFlightRecord(
            start_monotonic=time.monotonic(),
            job_name=job_name,
            cron_started_event_id=None,
        )
        return None


def _attach_started_event_id(job_id: str, event_id: Optional[str]) -> None:
    """Record the cron_started event_id on an existing in-flight slot.

    Called after on_job_started returns so future duplicate-skip events
    can reference the live cron_started for cross-event correlation.
    """
    if not event_id:
        return
    with _in_flight_lock:
        rec = _in_flight.get(job_id)
        if rec is not None:
            rec.cron_started_event_id = event_id


def _release_in_flight(job_id: str) -> None:
    """Release the in-flight slot for ``job_id`` (idempotent)."""
    with _in_flight_lock:
        _in_flight.pop(job_id, None)


def _release_in_flight_started_before(job_id: str, started_before: float) -> None:
    """Release the slot only if it belongs to a run that started before
    ``started_before`` (monotonic).

    Used by the soft-deadline handler when it abandons a runaway worker:
    by Guard #3 the registry can only hold the runaway's record at that
    moment, but if the runaway finished in the join/is_alive race window
    a SUCCESSOR fire may already have registered — its fresh record must
    not be popped by the deadline handler.
    """
    with _in_flight_lock:
        rec = _in_flight.get(job_id)
        if rec is not None and rec.start_monotonic <= started_before:
            _in_flight.pop(job_id, None)


def _emit_cron_skipped_duplicate(
    emitter, job_id: str, job_name: str, prior: "_InFlightRecord"
) -> None:
    """Emit exactly one CRON_SKIPPED_DUPLICATE for a same-job fire blocked
    because ``prior`` is still in flight.

    Shared by BOTH duplicate-block sites so they surface identical
    observability:

      * Guard #3 inside ``_process_job`` — a duplicate that reached the worker
        (e.g. the external-provider ``fire_due`` path, or an ``_in_flight``
        record seeded out-of-band), and
      * the submit-time running-set guard in ``_submit_with_guard`` — upstream's
        0.16.0 non-blocking-dispatch dedup (``_running_job_ids``), which lands
        FIRST in the tick path and otherwise swallows the duplicate silently.
        That is the trigger_job()-vs-tick cross-tick race Guard #3 was built to
        make visible; the running-set guard shadowed its emit seam when the
        0.16.0 catch-up merge rerouted dispatch (no conflict, dead emit).

    ``reason`` mirrors the dedup guard's wedge threshold:
    ``concurrent_fire_blocked`` while the prior fire is within the timeout,
    ``prior_fire_exceeded_timeout`` once it has outlived it. Never raises.
    """
    elapsed = max(0.0, time.monotonic() - prior.start_monotonic)
    timeout_s = _dup_guard_timeout_seconds()
    reason = (
        "concurrent_fire_blocked"
        if elapsed < timeout_s
        else "prior_fire_exceeded_timeout"
    )
    logger.warning(
        "Cron duplicate fire blocked: job_id=%s job_name=%s "
        "elapsed=%.1fs reason=%s prior_event=%s",
        job_id, job_name, elapsed, reason, prior.cron_started_event_id,
    )
    if emitter:
        try:
            emitter.on_job_skipped_duplicate(
                job_id=job_id,
                job_name=job_name,
                prior_cron_started_event_id=prior.cron_started_event_id,
                prior_elapsed_seconds=round(elapsed, 1),
                reason=reason,
            )
        except Exception as ee:
            logger.warning(
                "Event emit failed for cron_skipped_duplicate: %s", ee
            )


# === Per-job soft deadline (2026-06-10 audit M1 T1.4) =======================
#
# LLM cron jobs had NO wall-clock bound: a hung run_job blocked its worker
# forever — and in the sequential bucket that blocked every later job in
# the tick (observed incident class: one slow LLM job stalls all crons).
# Python threads cannot be killed, so "timeout" means: stop WAITING, mark
# the run failed, surface an event, and (where safe) free the schedule.
_DEFAULT_JOB_TIMEOUT_S = 1800.0  # matches _DEFAULT_DUP_GUARD_TIMEOUT_S


def _job_timeout_seconds(job: dict) -> float:
    """Soft wall-clock deadline for one cron job run.

    Priority: per-job ``timeout_seconds`` field >
    ``HERMES_CRON_JOB_TIMEOUT_SECONDS`` env var > config
    ``cron.job_timeout_seconds`` > 1800s default. A value <= 0 disables
    the deadline for that job. The default deliberately equals the dedup
    guard's wedge threshold so both mechanisms agree on when a fire is
    considered stuck.
    """
    per_job = job.get("timeout_seconds")
    if per_job is not None:
        try:
            return float(per_job)
        except (TypeError, ValueError):
            pass
    raw = os.getenv("HERMES_CRON_JOB_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    try:
        _cfg = load_config() or {}
        val = (_cfg.get("cron", {}) if isinstance(_cfg, dict) else {}).get(
            "job_timeout_seconds"
        )
        if val is not None:
            return float(val)
    except Exception:
        pass
    return _DEFAULT_JOB_TIMEOUT_S


def _run_callable_with_deadline(job, process_fn, abandon_on_timeout, ctx):
    """Run one due job under a soft wall-clock deadline.

    The job executes in a daemon worker thread (under ``ctx``, the
    caller's copied contextvars). At the deadline:

    - ``abandon_on_timeout=True`` (parallel-safe jobs): the run is marked
      failed, a failure event is emitted, the in-flight slot is released
      (start-time-checked) so the next scheduled fire is not blocked, and
      the worker is abandoned. ``process_fn`` receives an ``_abandoned``
      threading.Event and must suppress its late side effects once set.
    - ``abandon_on_timeout=False`` (workdir/profile jobs): those mutate
      process-global state (os.environ snapshot/restore, Hermes-home
      override), so racing a successor against the runaway's eventual
      restore could corrupt the successor's environment. The deadline
      only ALERTS (failure-shaped event for operator visibility) and then
      keeps waiting, preserving the sequential invariant.
    """
    timeout_s = _job_timeout_seconds(job)
    if timeout_s <= 0:
        return ctx.run(process_fn, job)

    job_id = job["id"]
    job_name = job.get("name", job_id)
    abandoned = threading.Event()
    box: dict = {
        "deadline_decided": threading.Event(),
        "deadline_finalized": threading.Event(),
        "deadline_monotonic": (
            time.monotonic() + timeout_s if abandon_on_timeout else None
        ),
    }

    def _worker():
        try:
            import inspect

            worker_kwargs = {"_abandoned": abandoned}
            if "_deadline_box" in inspect.signature(process_fn).parameters:
                worker_kwargs["_deadline_box"] = box
            box["result"] = ctx.run(process_fn, job, **worker_kwargs)
        except Exception:
            logger.exception("Deadline worker for cron job %s crashed", job_id)
            box["result"] = False

    t = threading.Thread(
        target=_worker, daemon=True, name=f"cron-job-{str(job_id)[:12]}"
    )
    t.start()
    t.join(timeout_s)
    if not t.is_alive():
        box["deadline_decided"].set()
        return bool(box.get("result", False))

    deadline_now = time.monotonic()
    # Signal abandonment BEFORE logging/event emission. Those calls can block;
    # setting it afterward let a worker finish, commit success, and then be
    # overwritten by the provisional deadline failure.
    if abandon_on_timeout:
        abandoned.set()
    box["deadline_decided"].set()
    msg = "soft deadline exceeded: still running after %ds%s" % (
        int(timeout_s),
        "; worker abandoned (daemon thread)" if abandon_on_timeout
        else "; sequential job — tick keeps waiting",
    )
    logger.error("Cron job '%s' (%s): %s", job_name, job_id, msg)
    emitter = _get_event_emitter()
    if emitter:
        try:
            emitter.on_job_completed(
                job_id=job_id,
                job_name=job_name,
                success=False,
                duration=round(timeout_s, 1),
                output_summary=None,
                error=msg,
                # The next real completion refreshes the true count.
                consecutive_errors=0,
            )
        except Exception as ee:
            logger.warning("Event emit failed for deadline timeout: %s", ee)

    if not abandon_on_timeout:
        t.join()
        return bool(box.get("result", False))

    firecrawl_state = box.get("firecrawl_state")
    if firecrawl_state and firecrawl_state.first_failure:
        _finalize_agent_iteration_event(
            emitter,
            job,
            "",
            success=False,
            firecrawl_state=firecrawl_state,
        )
    box["deadline_error"] = msg
    try:
        try:
            box["deadline_last_run_at"] = mark_job_run(job_id, False, msg)
        except Exception:
            logger.exception("Failed to mark timed-out cron job %s", job_id)
        # This execution verdict is provisional: the abandoned worker remains
        # alive and can compare-and-swap it to the real outcome when it finishes.
        _deadline_execution_id = job.get("execution_id")
        if _deadline_execution_id:
            try:
                finish_execution(_deadline_execution_id, success=False, error=msg)
            except Exception:
                logger.debug(
                    "finish_execution failed for deadline-abandoned %s", job_id
                )
    finally:
        # The worker can cross its finish line while the deadline owner is
        # still persisting the provisional verdict. Order that handoff
        # explicitly instead of racing on dict keys or wall time.
        box["deadline_finalized"].set()
    _release_in_flight_started_before(job_id, deadline_now)
    return False


def _deadline_has_elapsed(
    abandoned: Optional[threading.Event], deadline_box: Optional[dict]
) -> bool:
    """Recognize abandonment without a thread-scheduling race.

    After ``join(timeout)`` returns, the worker can run before the watchdog
    thread sets ``abandoned``. Crossing the monotonic boundary makes it wait
    for the watchdog's authoritative live/dead decision rather than infer
    abandonment from thread scheduling.
    """
    if abandoned is None:
        return False
    if abandoned.is_set():
        return True
    if deadline_box is None:
        return False
    deadline = deadline_box.get("deadline_monotonic")
    if deadline is None or time.monotonic() < deadline:
        return False
    # We crossed the boundary while the watchdog thread may still be waking
    # from join(timeout). Wait for its authoritative decision: if this worker
    # is still alive, it will declare abandonment; if the join observed a
    # completed worker, normal completion owns the verdict.
    decided = deadline_box.get("deadline_decided")
    if decided is None or not decided.wait(timeout=30):
        logger.error("Deadline watchdog did not publish its boundary decision")
        return False
    return abandoned.is_set()


def _amend_late_deadline_outcome(
    job: dict,
    *,
    success: bool,
    error: Optional[str],
    deadline_box: Optional[dict],
) -> bool:
    """Persist a deadline-abandoned worker's real terminal outcome.

    Delivery, completion events, and slot mutation remain suppressed. The
    durable job and execution records are the exception: leaving them at
    the provisional deadline failure is the false-red defect this handoff
    repairs. Both writes are narrow compare-and-swaps, so a successor fire
    always wins.
    """
    if deadline_box is None:
        return False
    finalized = deadline_box.get("deadline_finalized")
    if finalized is None:
        logger.error(
            "Job '%s': deadline finalization signal missing; late outcome not amended",
            job.get("id"),
        )
        return False
    # The watchdog owns the provisional write and must finish it before either
    # CAS can identify that exact attempt. Do not turn a slow event subscriber
    # or jobs/ledger write into a permanent false red by giving up here: this
    # already-abandoned daemon worker has no later retry path. The watchdog sets
    # this event from a finally block, so every ordinary success/failure path
    # releases the handoff.
    finalized.wait()
    abandon_error = deadline_box.get("deadline_error")
    deadline_last_run_at = deadline_box.get("deadline_last_run_at")
    if not abandon_error:
        logger.error(
            "Job '%s': deadline error identity missing; late outcome not amended",
            job.get("id"),
        )
        return False

    job_amended = False
    if deadline_last_run_at:
        try:
            job_amended = amend_late_outcome_after_abandon(
                job["id"],
                abandon_error=abandon_error,
                expected_last_run_at=deadline_last_run_at,
                success=success,
                error=error,
            )
        except Exception:
            logger.exception("Late job-status amendment failed for %s", job.get("id"))
    else:
        logger.error(
            "Job '%s': deadline job timestamp missing; job status not amended",
            job.get("id"),
        )

    ledger_amended = False
    execution_id = job.get("execution_id")
    if execution_id:
        try:
            ledger_amended = amend_execution_after_abandon(
                execution_id,
                abandon_error=abandon_error,
                success=success,
                error=error,
            ) is not None
        except Exception:
            logger.exception(
                "Late execution-ledger amendment failed for %s", job.get("id")
            )
    return job_amended or ledger_amended


def _summarize_for_event_bus(final_response: str) -> str:
    """Preserve normal cron reports while capping truly huge event payloads."""
    summary = final_response or ""
    if len(summary) <= CRON_EVENT_SUMMARY_MAX_CHARS:
        return summary

    budget = max(1, CRON_EVENT_SUMMARY_MAX_CHARS - 3)
    clipped = summary[:budget]
    split_at = max(clipped.rfind("\n"), clipped.rfind(" "))
    if split_at >= budget // 2:
        clipped = clipped[:split_at]
    return clipped.rstrip() + "..."


# === Tailor structured iteration event (2026-04-29) =========================
#
# Plan: docs/superpowers/plans/2026-04-29-tailor-structured-iteration-event.md
# Design: docs/superpowers/specs/2026-04-29-tailor-structured-iteration-event-design.md
#
# Tailor's [SILENT] response is by-design (it suppresses Telegram delivery)
# but carries zero diagnostic info. The Critic cluster triage on 2026-04-29
# could not distinguish "47 eligible packets, all already terminal"
# (legitimate) from "0 eligible because Applier is stuck" (real bug).
#
# Solution: Tailor's SOUL.md now requires it to emit a JSON-marker block on
# every cron run. The cron-wrapper extracts it and emits a structured
# `tailor_iteration` event. `[SILENT]` is preserved unchanged — the new
# event runs ALONGSIDE it, not in place of it.
#
# Failure modes are explicit AGENT_ERROR events (rather than silent drops)
# so the failure is observable on the bus.
TAILOR_ITERATION_MARKER_RE = re.compile(
    r"<TAILOR_ITERATION_JSON>(.*?)</TAILOR_ITERATION_JSON>",
    re.DOTALL,
)
TAILOR_ITERATION_REQUIRED_FIELDS = (
    "eligible_count",
    "tailored_count",
    "skipped_terminal_count",
    "skipped_other_count",
    "reason",
)
TAILOR_ITERATION_REASON_ENUM = frozenset({
    "all_already_terminal",
    "no_eligible_packets",
    "tailored_some",
    "mixed",
    "error",
    "other",
})
TAILOR_ITERATION_JOB_NAME = "jobflow-tailor"

# Reason strings emitted on AGENT_ERROR for parser failures.
TAILOR_ITERATION_REASON_MISSING = "tailor_iteration_missing"
TAILOR_ITERATION_REASON_PARSE_FAILED = "tailor_iteration_parse_failed"
TAILOR_ITERATION_REASON_SCHEMA_MISMATCH = "tailor_iteration_schema_mismatch"


def _extract_tailor_iteration(final_response: str):
    """Extract the structured iteration block from Tailor's cron output.

    Returns ``(parsed_payload, error_reason, raw_block)``:
      - ``parsed_payload`` is a dict on the happy path (marker found, JSON
        parses, schema conforms); otherwise ``None``.
      - ``error_reason`` is one of TAILOR_ITERATION_REASON_* constants on
        failure; otherwise ``None``.
      - ``raw_block`` is the matched block contents (str) when the marker
        was present, even on parse/schema failure (None when missing).

    Exactly one of ``parsed_payload`` / ``error_reason`` is non-None.

    Schema validation:
      - All four count fields must be ints with value >= 0.
      - ``reason`` must be a string. Unknown values are accepted (passed
        through to the bus); the Critic flags drift later.
    """
    text = final_response or ""
    match = TAILOR_ITERATION_MARKER_RE.search(text)
    if not match:
        return None, TAILOR_ITERATION_REASON_MISSING, None

    raw_block = match.group(1).strip()
    try:
        parsed = json.loads(raw_block)
    except (json.JSONDecodeError, ValueError):
        return None, TAILOR_ITERATION_REASON_PARSE_FAILED, raw_block

    if not isinstance(parsed, dict):
        return None, TAILOR_ITERATION_REASON_SCHEMA_MISMATCH, raw_block

    # Required fields presence + types.
    for field_name in TAILOR_ITERATION_REQUIRED_FIELDS:
        if field_name not in parsed:
            return None, TAILOR_ITERATION_REASON_SCHEMA_MISMATCH, raw_block

    for count_field in (
        "eligible_count",
        "tailored_count",
        "skipped_terminal_count",
        "skipped_other_count",
    ):
        value = parsed.get(count_field)
        # bool is a subclass of int — exclude explicitly so ``True`` doesn't
        # masquerade as a count.
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None, TAILOR_ITERATION_REASON_SCHEMA_MISMATCH, raw_block

    if not isinstance(parsed.get("reason"), str) or not parsed["reason"]:
        return None, TAILOR_ITERATION_REASON_SCHEMA_MISMATCH, raw_block

    return parsed, None, raw_block


def _emit_tailor_iteration_event(emitter, job: dict, final_response: str) -> None:
    """Hook called from _process_job after a jobflow-tailor run completes.

    Gated by job_name == TAILOR_ITERATION_JOB_NAME — other crons short-circuit.

    Emits exactly one of:
      - EventType.TAILOR_ITERATION (happy path)
      - EventType.AGENT_ERROR with payload.reason in TAILOR_ITERATION_REASON_*
        (any of the 3 failure modes)

    Wrapped in a broad try/except so a parser bug never crashes the
    cron tick — emit failures degrade gracefully to a debug log.
    """
    if not emitter:
        return
    if job.get("name") != TAILOR_ITERATION_JOB_NAME:
        return
    try:
        from events.schema import EventType

        bus = getattr(emitter, "bus", None)
        if bus is None:
            return

        job_id = job.get("id") or ""
        job_name = job.get("name", TAILOR_ITERATION_JOB_NAME)

        parsed, error_reason, raw_block = _extract_tailor_iteration(final_response)

        if error_reason is None and parsed is not None:
            payload = dict(parsed)  # copy so we can add metadata fields
            payload["job_id"] = job_id
            payload["job_name"] = job_name
            bus.emit(
                event_type=EventType.TAILOR_ITERATION,
                source="tailor",
                payload=payload,
                correlation_id=job_id or None,
                job_id=job_id or None,
            )
            return

        # Failure path — emit AGENT_ERROR with explicit reason so the
        # Critic + operators can discriminate prompt regression from a
        # silent run.
        err_payload = {
            "reason": error_reason,
            "job_id": job_id,
            "job_name": job_name,
        }
        if raw_block is not None:
            # Keep a bounded copy of the offending block so audit log doesn't
            # blow up if the agent emitted a multi-MB payload between markers.
            err_payload["detail"] = raw_block[:2000]
        bus.emit(
            event_type=EventType.AGENT_ERROR,
            source="tailor",
            payload=err_payload,
            correlation_id=job_id or None,
            job_id=job_id or None,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("tailor_iteration emit failed: %s", e)


# ============================================================================
# AGENT_ITERATION — generic per-agent run summary (2026-04-30)
#
# Extends the TAILOR_ITERATION pattern to every cron-driven agent. Each
# agent's SOUL.md contracts it to emit an <AGENT_ITERATION_JSON>{...}</AGENT_
# ITERATION_JSON> block at the end of its cron output. The cron-wrapper
# extracts and emits a structured EventType.AGENT_ITERATION event.
#
# Why generic vs. one EventType per agent: a single envelope keeps the
# Critic's analyzer surface small (one event-type to query) and lets agents
# evolve their counter schemas without bus.py changes. Per-agent topic
# routing (AGENT_TOPIC_MAP in telegram_notifier.py) puts the message in the
# right Telegram surface based on payload.agent.
#
# Schema (validated by _extract_agent_iteration):
#   agent (str, required)        — canonical lowercase agent name
#   summary (str, required)      — one-line human-readable text (≤200 chars)
#   counters (dict, optional)    — agent-specific metrics
#   anomalies (list, optional)   — list of dicts/strings flagged for Critic
#   reason (str, optional)       — categorical reason (e.g. "no_work")
#
# Failure modes are explicit AGENT_ERROR events so a regression in any
# agent's prompt is observable on the bus rather than silently dropped.
#
# Agent-name override (2026-04-30): the LLM-supplied ``agent`` field is
# overridden with ``canonical_agent_source(job_name)`` whenever the job
# name maps to a known canonical identity.  This makes per-agent topic
# routing (AGENT_TOPIC_MAP in telegram_notifier.py) deterministic
# instead of dependent on the LLM picking the right name for its role
# each run.  The original LLM choice is preserved as
# ``agent_llm_supplied`` for audit/debugging.  Jobs without a canonical
# mapping fall back to the LLM-supplied name.  See
# docs/superpowers/specs/2026-04-30-agent-iteration-canonical-name.md.
#
# Marker-missing fallback (2026-04-30): when a known canonical agent's
# job omits the ``<AGENT_ITERATION_JSON>`` marker entirely, a placeholder
# event is synthesized so 100% of canonical-agent runs leave an audit
# trail (devflow-bridge was observed dropping the marker on ~46% of
# runs).  Synthesized payloads carry ``synthesized: True`` so downstream
# consumers (Critic, dashboards) can filter or weight them differently
# from real LLM-emitted iterations.  Jobs without a canonical mapping
# remain silent — we do not synthesize for ad-hoc / one-off crons that
# never opted into the schema.  See
# docs/superpowers/specs/2026-04-30-agent-iteration-marker-fallback.md.
# ============================================================================
AGENT_ITERATION_MARKER_RE = re.compile(
    r"<AGENT_ITERATION_JSON>(.*?)</AGENT_ITERATION_JSON>",
    re.DOTALL,
)
AGENT_ITERATION_REQUIRED_FIELDS = ("agent", "summary")
AGENT_ITERATION_REASON_MISSING = "agent_iteration_missing"
AGENT_ITERATION_REASON_PARSE_FAILED = "agent_iteration_parse_failed"
AGENT_ITERATION_REASON_SCHEMA_MISMATCH = "agent_iteration_schema_mismatch"
AGENT_ITERATION_SUMMARY_MAX_CHARS = 200
AGENT_ITERATION_BRIEF_MAX_CHARS = 1500
TRACKER_ITERATION_JOB_NAME = "jobflow-tracker-cycle"
TRACKER_PHASE_SECONDS_KEYS = frozenset({
    "inbox",
    "pipeline_update",
    "analytics",
    "postgres_bridge",
    "api_reconcile",
})


def _valid_phase_seconds(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != TRACKER_PHASE_SECONDS_KEYS:
        return False
    return all(
        not isinstance(seconds, bool)
        and isinstance(seconds, (int, float))
        and math.isfinite(seconds)
        and seconds >= 0
        for seconds in value.values()
    )


def _strip_iteration_markers(text: str) -> str:
    """Remove internal iteration-tracking marker blocks from agent output
    before it is delivered to a user-facing channel.

    Cron-driven agents emit ``<AGENT_ITERATION_JSON>{...}</AGENT_ITERATION_JSON>``
    (and Tailor the legacy ``<TAILOR_ITERATION_JSON>{...}</TAILOR_ITERATION_JSON>``)
    as a machine-only contract consumed by the event-bus extractors
    (``_extract_agent_iteration`` / ``_extract_tailor_iteration``).  Those
    extractors read the marker off the raw ``final_response`` in
    ``_process_job`` and never see the delivered copy, so stripping the block
    here leaves structured-event emission untouched.

    The block must never reach the delivered body: Telegram's HTML parse mode
    rejects the unsupported ``<agent_iteration_json>`` tag and falls back to
    plain text on every send (high-frequency, cosmetic log spam), and the raw
    JSON is noise to the user.

    Marker-free text is returned unchanged (no whitespace normalization), so
    this is a no-op for the vast majority of deliveries.
    """
    if not text:
        return text
    cleaned = AGENT_ITERATION_MARKER_RE.sub("", text)
    cleaned = TAILOR_ITERATION_MARKER_RE.sub("", cleaned)
    if cleaned == text:
        return text
    # A removed block typically leaves a trailing (or leading) blank-line gap.
    # Collapse 3+ consecutive newlines to a paragraph break and trim the ends.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _extract_agent_iteration(final_response: str):
    """Extract the generic agent-iteration block from any cron output.

    Returns ``(parsed_payload, error_reason, raw_block)`` matching the
    TAILOR_ITERATION extractor's contract.

    Schema validation:
      - ``agent`` and ``summary`` must be non-empty strings.
      - ``summary`` must be ≤AGENT_ITERATION_SUMMARY_MAX_CHARS (truncated
        with marker rather than rejected — keeps real-world output flowing
        even if an agent prompt-drifts).
      - ``counters`` (if present) must be a dict mapping str → number.
      - ``anomalies`` (if present) must be a list.
      - ``reason`` (if present) must be a string.
      - ``brief`` is optional; when present it must be a string or schema-mismatch,
        is trimmed, dropped if blank, and capped at 1500 chars with an ellipsis.

    The marker is OPTIONAL — _missing_ is signaled but ALSO silent (no
    AGENT_ERROR) for non-contracted jobs. Only PRESENT-but-malformed cases
    surface as AGENT_ERROR so we don't spam every legacy job's output.
    """
    text = final_response or ""
    match = AGENT_ITERATION_MARKER_RE.search(text)
    if not match:
        return None, AGENT_ITERATION_REASON_MISSING, None

    raw_block = match.group(1).strip()
    try:
        parsed = json.loads(raw_block)
    except (json.JSONDecodeError, ValueError):
        return None, AGENT_ITERATION_REASON_PARSE_FAILED, raw_block

    if not isinstance(parsed, dict):
        return None, AGENT_ITERATION_REASON_SCHEMA_MISMATCH, raw_block

    for field_name in AGENT_ITERATION_REQUIRED_FIELDS:
        value = parsed.get(field_name)
        if not isinstance(value, str) or not value.strip():
            return None, AGENT_ITERATION_REASON_SCHEMA_MISMATCH, raw_block

    counters = parsed.get("counters")
    if counters is not None:
        if not isinstance(counters, dict):
            return None, AGENT_ITERATION_REASON_SCHEMA_MISMATCH, raw_block
        for key, val in counters.items():
            if not isinstance(key, str):
                return None, AGENT_ITERATION_REASON_SCHEMA_MISMATCH, raw_block
            if key == "phase_seconds":
                if not _valid_phase_seconds(val):
                    return None, AGENT_ITERATION_REASON_SCHEMA_MISMATCH, raw_block
            elif isinstance(val, bool) or not isinstance(val, (int, float)):
                return None, AGENT_ITERATION_REASON_SCHEMA_MISMATCH, raw_block

    anomalies = parsed.get("anomalies")
    if anomalies is not None and not isinstance(anomalies, list):
        return None, AGENT_ITERATION_REASON_SCHEMA_MISMATCH, raw_block

    reason = parsed.get("reason")
    if reason is not None and not isinstance(reason, str):
        return None, AGENT_ITERATION_REASON_SCHEMA_MISMATCH, raw_block

    if "brief" in parsed:
        brief = parsed["brief"]
        if not isinstance(brief, str):
            return None, AGENT_ITERATION_REASON_SCHEMA_MISMATCH, raw_block
        stripped = brief.strip()
        if not stripped:
            parsed.pop("brief")
        else:
            if len(stripped) > AGENT_ITERATION_BRIEF_MAX_CHARS:
                stripped = stripped[: AGENT_ITERATION_BRIEF_MAX_CHARS - 1] + "…"
            parsed["brief"] = stripped

    # Normalize: lowercase agent name, truncate summary if needed
    parsed["agent"] = parsed["agent"].strip().lower()
    if len(parsed["summary"]) > AGENT_ITERATION_SUMMARY_MAX_CHARS:
        parsed["summary"] = (
            parsed["summary"][: AGENT_ITERATION_SUMMARY_MAX_CHARS - 1] + "…"
        )

    return parsed, None, raw_block


def _install_scout_firecrawl_run(job: dict):
    """Install one Firecrawl run state for a canonical Scout agent activation."""
    if job.get("no_agent"):
        return None, None
    from events.producers.agent_source_mapping import canonical_agent_source

    if canonical_agent_source(job.get("name")) != "scout":
        return None, None
    from agent.firecrawl_run_state import install_firecrawl_run

    return install_firecrawl_run()


def _reset_scout_firecrawl_run(token) -> None:
    if token is None:
        return
    from agent.firecrawl_run_state import reset_firecrawl_run

    reset_firecrawl_run(token)


def _credits_iteration_fields() -> dict:
    return {
        "action_required": True,
        "action_kind": "credits",
        "provider_error": "provider_credits_exhausted",
        "provider_scope": "account",
    }


def _claim_credits_iteration(firecrawl_state) -> bool:
    return bool(
        firecrawl_state
        and firecrawl_state.first_failure
        and firecrawl_state.claim_credits_action()
    )


def _emit_agent_iteration_event(
    emitter,
    job: dict,
    final_response: str,
    *,
    firecrawl_state=None,
    credits_only: bool = False,
) -> None:
    """Hook called from _process_job for ALL successful cron runs.

    Unlike _emit_tailor_iteration_event which is gated by job_name, this
    helper inspects every job's output for the AGENT_ITERATION marker.
    Jobs without the marker emit nothing (no AGENT_ERROR — this is opt-in).

    Emits one of:
      - EventType.AGENT_ITERATION (happy path)
      - EventType.AGENT_ERROR with payload.reason == AGENT_ITERATION_REASON_*
        (only if the marker was PRESENT but malformed; missing marker is
        silent so legacy jobs don't spam errors)
    """
    if not emitter:
        return
    # Skip jobflow-tailor — it has its own dedicated TAILOR_ITERATION
    # event with stricter schema. Avoid double-emit.
    if job.get("name") == TAILOR_ITERATION_JOB_NAME:
        return
    try:
        from events.schema import EventType

        bus = getattr(emitter, "bus", None)
        if bus is None:
            return

        job_id = job.get("id") or ""
        job_name = job.get("name", "")

        parsed, error_reason, raw_block = _extract_agent_iteration(final_response)

        # Lazy import — kept inside the function to avoid bootstrapping
        # events.producers at scheduler import time (matches the
        # EventType import pattern above).
        from events.producers.agent_source_mapping import (
            canonical_agent_source,
            is_canonical_agent,
        )

        if error_reason == AGENT_ITERATION_REASON_MISSING:
            # Marker absent.  Synthesize a placeholder AGENT_ITERATION for
            # known canonical agents so 100% of canonical-agent runs leave
            # an audit trail, even when the LLM forgets the marker (post-
            # 2026-04-30 telemetry showed devflow-bridge omitting it on
            # ~46% of runs).  Non-canonical / ad-hoc crons stay silent —
            # they never opted into the schema and we don't want to spam
            # AGENT_ITERATION events for arbitrary one-off jobs.
            canonical_name = canonical_agent_source(job_name)
            if not is_canonical_agent(canonical_name):
                return
            credits_owed = bool(
                firecrawl_state and firecrawl_state.first_failure
            )
            if credits_only and not credits_owed:
                return
            synth_payload = {
                "agent": canonical_name,
                "summary": (
                    f"{canonical_name} failed with Firecrawl credits exhausted"
                    if credits_only and credits_owed
                    else (
                        f"{canonical_name} completed (synthesized — "
                        f"agent did not emit AGENT_ITERATION_JSON)"
                    )
                ),
                "synthesized": True,
                "job_id": job_id,
                "job_name": job_name,
            }
            if credits_owed:
                synth_payload.update(_credits_iteration_fields())
                if not _claim_credits_iteration(firecrawl_state):
                    return
            bus.emit(
                event_type=EventType.AGENT_ITERATION,
                source=canonical_name,
                payload=synth_payload,
                correlation_id=job_id or None,
                job_id=job_id or None,
            )
            return

        if (
            error_reason is None
            and parsed is not None
            and job_name == TRACKER_ITERATION_JOB_NAME
            and not _valid_phase_seconds(
                parsed.get("counters", {}).get("phase_seconds")
            )
        ):
            error_reason = AGENT_ITERATION_REASON_SCHEMA_MISMATCH
            parsed = None

        if error_reason is None and parsed is not None:
            if credits_only and not (
                firecrawl_state and firecrawl_state.first_failure
            ):
                return
            payload = dict(parsed)
            payload["job_id"] = job_id
            payload["job_name"] = job_name

            # Deterministic agent override: route by job_name, not LLM whim.
            # The LLM is asked to "pick agent-name from your role" but its
            # interpretation drifts run-to-run (devflow-bridge has been
            # observed emitting agent=devflow|hermes_to_devflow|bridge|
            # watchdog|hermes_to_devflow_watchdog).  When the job name has
            # a known canonical agent, force it; preserve the LLM choice
            # as agent_llm_supplied for audit.
            canonical_name = canonical_agent_source(job_name)
            if is_canonical_agent(canonical_name):
                payload["agent_llm_supplied"] = parsed.get("agent")
                payload["agent"] = canonical_name

            if firecrawl_state and firecrawl_state.first_failure:
                payload.update(_credits_iteration_fields())
                if not _claim_credits_iteration(firecrawl_state):
                    return
            bus.emit(
                event_type=EventType.AGENT_ITERATION,
                source=payload.get("agent") or job_name or "unknown",
                payload=payload,
                correlation_id=job_id or None,
                job_id=job_id or None,
            )
            return

        # Marker-present-but-malformed → AGENT_ERROR with diagnostic detail.
        # A prior credits attempt makes the entire finalizer retry silent,
        # including this diagnostic event.
        if (
            firecrawl_state
            and firecrawl_state.first_failure
            and firecrawl_state.credits_action_claimed
        ):
            return
        err_payload = {
            "reason": error_reason,
            "job_id": job_id,
            "job_name": job_name,
        }
        if raw_block is not None:
            err_payload["detail"] = raw_block[:2000]
        try:
            bus.emit(
                event_type=EventType.AGENT_ERROR,
                source=job_name or "cron",
                payload=err_payload,
                correlation_id=job_id or None,
                job_id=job_id or None,
            )
        except Exception as e:  # Preserve the independently gated credits attempt.
            logger.debug("agent_iteration diagnostic emit failed: %s", e)
        if firecrawl_state and firecrawl_state.first_failure:
            canonical_name = canonical_agent_source(job_name)
            if is_canonical_agent(canonical_name):
                synth_payload = {
                    "agent": canonical_name,
                    "summary": (
                        f"{canonical_name} failed with Firecrawl credits exhausted"
                    ),
                    "synthesized": True,
                    "job_id": job_id,
                    "job_name": job_name,
                    **_credits_iteration_fields(),
                }
                if _claim_credits_iteration(firecrawl_state):
                    bus.emit(
                        event_type=EventType.AGENT_ITERATION,
                        source=canonical_name,
                        payload=synth_payload,
                        correlation_id=job_id or None,
                        job_id=job_id or None,
                    )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("agent_iteration emit failed: %s", e)


def _finalize_agent_iteration_event(
    emitter,
    job: dict,
    final_response: str,
    *,
    success: bool,
    firecrawl_state,
) -> None:
    """Emit ordinary iteration evidence or one credits-gated Scout action."""
    if success:
        _emit_agent_iteration_event(
            emitter,
            job,
            final_response,
            firecrawl_state=firecrawl_state,
        )
    elif firecrawl_state and firecrawl_state.first_failure:
        _emit_agent_iteration_event(
            emitter,
            job,
            final_response,
            firecrawl_state=firecrawl_state,
            credits_only=True,
        )
# ============================================================================


@contextmanager
def _job_profile_context(job_id: str, profile: Optional[str]):
    """Temporarily run a job under a specific Hermes profile.

    Cron jobs are stored and scheduled by the profile running the scheduler, but
    an individual job can opt into a different runtime profile. While active,
    the scheduler's test/override hook and a context-local Hermes home override
    both point at the resolved profile directory so _get_hermes_home(),
    .env/config loading, script resolution, AIAgent construction, and downstream
    get_hermes_home() callers agree on the same home.

    Some existing provider/config paths still load profile .env values through
    os.environ, so profile jobs also snapshot and restore the process
    environment on exit. tick() runs profile jobs sequentially to keep that
    temporary mutation isolated from other scheduled jobs.
    """
    raw_profile = str(profile or "").strip()
    if not raw_profile:
        yield None
        return

    env_snapshot = os.environ.copy()

    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    normalized_profile = normalize_profile_name(raw_profile)
    try:
        profile_home = Path(resolve_profile_env(normalized_profile)).resolve()
    except (FileNotFoundError, ValueError) as exc:
        logger.warning(
            "Job '%s': configured profile %r no longer valid (%s) — "
            "falling back to scheduler default",
            job_id, raw_profile, exc,
        )
        yield None
        return

    override_token = None
    try:
        # ONLY the context-local override. Assigning the module-global
        # `_hermes_home` here as well is what leaked this profile to every
        # other thread: `_get_hermes_home()` reads that global FIRST
        # (`_hermes_home or get_hermes_home()`), so the global silently
        # defeated the ContextVar's isolation and a concurrently-firing job
        # resolved HERMES_HOME/scripts/<name> under THIS profile. Measured
        # 2026-08-19/20: 8 "Script not found" failures across 6 jobs, all under
        # profiles/financier, all concurrent with a financier-profile job, each
        # collecting nothing while still reporting last_status=ok.
        #
        # The global remains a TEST/monkeypatch hook (see _get_hermes_home) --
        # it is simply never written from the job path any more.
        override_token = set_hermes_home_override(profile_home)
        logger.info(
            "Job '%s': using Hermes profile '%s' (%s)",
            job_id,
            normalized_profile,
            profile_home,
        )
        yield normalized_profile
    finally:
        if override_token is not None:
            reset_hermes_home_override(override_token)
        # Delta-based restore: remove added keys, restore changed keys.
        # Avoids a brief window where other threads see an empty env.
        added = set(os.environ.keys()) - set(env_snapshot.keys())
        for k in added:
            os.environ.pop(k, None)
        for k, v in env_snapshot.items():
            if os.environ.get(k) != v:
                os.environ[k] = v


def _resolve_origin(job: dict) -> Optional[dict]:
    """Extract origin info from a job, preserving any extra routing metadata.

    Treats non-dict origins (free-form provenance strings, ints, lists from
    migration scripts or hand-edited jobs.json) as missing instead of
    crashing with ``AttributeError`` on ``origin.get(...)``. Without this
    guard, a job tagged with e.g. ``"combined-digest-replaces-x-and-y"``
    crashed every fire attempt with
    ``'str' object has no attribute 'get'`` — ``mark_job_run`` recorded the
    failure, but the next tick re-loaded the same poisoned origin and
    crashed identically until the field was patched manually (#18722).
    """
    origin = job.get("origin")
    # Defensive: jobs.json has historically been seeded with non-dict values
    # (e.g. a bare provenance string), which would crash every cron tick when
    # we called `.get()` on it. Treat any non-dict shape as "no origin".
    if not isinstance(origin, dict) or not origin:
        return None
    platform = origin.get("platform")
    chat_id = origin.get("chat_id")
    if platform and chat_id:
        return origin
    return None


def _cron_mirror_delivery_enabled(job: dict, cfg: Optional[dict] = None) -> bool:
    """Whether a cron delivery should also be mirrored into the target chat's
    gateway session transcript.

    Default OFF — preserves the historical isolation guarantee (cron deliveries
    live only in the cron job's own session, never the target chat's history)
    byte-for-byte for everyone who does not opt in.

    Precedence (first decisive value wins):
      1. Per-job ``attach_to_session`` (bool) — set via the ``cronjob`` tool,
         lets one briefing job opt in without flipping global behaviour.
      2. Global ``cron.mirror_delivery`` (bool) in config.yaml.
      3. False.

    When enabled, the cron's final output is appended to the target session as
    an assistant turn via the existing ``gateway.mirror.mirror_to_session`` —
    the same primitive ``send_message`` uses — so the next user reply in that
    chat sees the brief in context (no "what is Task #2?" amnesia). This is
    alternation- and cache-safe: the append lands at a turn boundary between
    user turns, never mid-loop, and never mutates the cached system prompt.
    """
    per_job = job.get("attach_to_session")
    if isinstance(per_job, bool):
        return per_job
    try:
        if cfg is None:
            cfg = load_config() or {}
        return bool((cfg.get("cron", {}) or {}).get("mirror_delivery", False))
    except Exception:
        return False


def _target_matches_origin(origin: dict, platform_name: str, chat_id: str,
                           thread_id: Optional[str]) -> bool:
    """True when a delivery target is the job's own origin conversation.

    Mirroring is scoped to the origin session by design (see
    ``_maybe_mirror_cron_delivery``). A job created from a live gateway chat
    stamps that chat as ``origin`` (``cronjob_tools._origin_from_env``), and
    that session is guaranteed to exist — it is the very conversation the user
    was in when they scheduled the job. Fan-out targets (``deliver=all``,
    explicit ``platform:chat_id`` to some *other* chat, or a home-channel
    fallback for an origin-less API/script job) are deliberately NOT mirrored:
    they are broadcasts, not a continuation of a conversation, and may point at
    a chat the user never opened an agent session in.

    This makes the historical "cold-start" worry a non-case: when the mirror
    semantically applies (target == origin) the session always exists; when no
    session exists, the target was never the origin conversation, so we simply
    do not mirror.
    """
    if not origin:
        return False
    if str(origin.get("platform", "")).lower() != str(platform_name).lower():
        return False
    if str(origin.get("chat_id", "")) != str(chat_id):
        return False
    # thread_id must match when the origin pins one (topic-scoped chats); a
    # target that lost the thread_id is not the same conversation lane.
    origin_thread = origin.get("thread_id")
    if origin_thread is not None and str(origin_thread) != str(thread_id or ""):
        return False
    return True


def _maybe_mirror_cron_delivery(
    job: dict,
    platform_name: str,
    chat_id: str,
    mirror_text: str,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    *,
    enabled: bool = False,
) -> None:
    """Best-effort mirror of a cron delivery into the origin chat's session.

    No-op unless ``enabled`` (resolved once by the caller, and already scoped to
    the origin target — see ``_target_matches_origin``). Reuses the shipped
    ``mirror_to_session`` so cron rides exactly the same path that interactive
    ``send_message`` mirroring already uses, including passing ``user_id`` so a
    per-user-isolated group chat resolves to the exact member who scheduled the
    job (parity with ``send_message``). All failures are swallowed — a delivery
    that succeeded must never be reported as failed because the transcript
    mirror hit a problem.

    Because the caller only enables this for the target that equals the job's
    origin conversation, the session is expected to exist (the job was born in
    that session). A missing session therefore indicates an origin-less /
    fan-out delivery that should not have been mirrored anyway, and is treated
    as a silent no-op — never a synthetic session is created.
    """
    if not enabled:
        return
    text = (mirror_text or "").strip()
    if not text:
        return
    try:
        from gateway.mirror import mirror_to_session

        # Mirror as a USER turn with a labelled prefix, NOT an assistant turn.
        # The brief is not the agent speaking; an assistant-role mirror lands as
        # assistant→assistant after the agent's last turn and breaks strict
        # alternation (issue #2221, the exact failure #2313 removed). A
        # user-role turn collapses safely via repair_message_sequence's
        # consecutive-user merge on every provider, and the prefix preserves the
        # "this came from cron" context that the dropped SQLite mirror metadata
        # would otherwise lose on replay.
        ok = mirror_to_session(
            platform_name,
            str(chat_id),
            f"[Cron delivery: {job.get('name') or job.get('id', 'cron')}]\n{text}",
            source_label="cron",
            thread_id=thread_id,
            user_id=user_id,
            role="user",
        )
        if ok:
            logger.info(
                "Job '%s': mirrored delivery into %s:%s session transcript",
                job.get("id", "?"), platform_name, chat_id,
            )
        else:
            logger.debug(
                "Job '%s': delivery mirror skipped for %s:%s "
                "(no matching gateway session — cold start)",
                job.get("id", "?"), platform_name, chat_id,
            )
    except Exception as e:
        logger.debug(
            "Job '%s': delivery mirror failed for %s:%s: %s",
            job.get("id", "?"), platform_name, chat_id, e,
        )


def _open_continuable_cron_thread(
    job: dict,
    adapter,
    chat_id: str,
    loop,
) -> Optional[str]:
    """Open a dedicated thread for a continuable cron job (thread-preferred).

    Returns the new ``thread_id`` on success, or ``None`` when the platform has
    no thread primitive (WhatsApp/Signal/SMS) or creation failed — the ``None``
    return is the caller's signal to fall back to the origin-DM mirror, the same
    open-thread-or-fallback shape as ``GatewayRunner._process_handoff``. Reuses
    the shipped ``adapter.create_handoff_thread``; no new adapter surface.
    """
    create_thread = getattr(adapter, "create_handoff_thread", None)
    if not callable(create_thread) or loop is None:
        return None
    task_name = job.get("name") or job.get("id", "cron")
    thread_name = f"Hermes — {task_name}"
    try:
        from agent.async_utils import safe_schedule_threadsafe

        coro = create_thread(str(chat_id), thread_name)
        future = safe_schedule_threadsafe(coro, loop)  # type: ignore[arg-type]
        if future is None:
            return None
        new_thread_id = future.result(timeout=30)
        return str(new_thread_id) if new_thread_id else None
    except Exception as e:
        logger.debug(
            "Job '%s': create_handoff_thread failed on %s — falling back to "
            "DM-session mirror: %s",
            job.get("id", "?"), getattr(adapter, "name", "?"), e,
        )
        return None


def _seed_cron_thread_session(
    job: dict,
    adapter,
    platform_name: str,
    chat_id: str,
    thread_id: str,
    mirror_text: str,
    chat_name: Optional[str] = None,
) -> None:
    """Seed the freshly-opened cron thread's session with the brief.

    Without this the brief is *visible* in the new thread but absent from any
    transcript, so the user's first reply in-thread would hit a session with no
    record of it ("what is Task #2?"). We create the thread-keyed session (the
    same key the user's reply will resolve to — ``build_session_key`` keys
    threads as participant-shared, so no ``user_id`` is needed) and append the
    brief as an assistant turn via the shipped ``mirror_to_session``.

    Mirrors ``GatewayRunner._process_handoff``'s seed step, but standalone:
    cron reaches the live ``SessionStore`` through the adapter's
    ``_session_store`` handle rather than the gateway object. Best-effort — a
    delivery that already succeeded is never failed by a seeding problem.
    """
    text = (mirror_text or "").strip()
    if not text:
        return
    try:
        from gateway.config import Platform
        from gateway.session import SessionSource

        session_store = getattr(adapter, "_session_store", None)
        if session_store is not None:
            try:
                platform_enum = Platform(platform_name.lower())
            except (ValueError, KeyError):
                platform_enum = None
            if platform_enum is not None:
                dest_source = SessionSource(
                    platform=platform_enum,
                    chat_id=str(chat_id),
                    chat_name=chat_name,
                    chat_type="thread",
                    user_id="system:cron",
                    user_name="Cron",
                    thread_id=str(thread_id),
                )
                # Ensure the thread-keyed session row exists so the mirror has
                # a target and the user's later reply joins the same session.
                session_store.get_or_create_session(dest_source)

        from gateway.mirror import mirror_to_session

        # User-role + labelled prefix (see _maybe_mirror_cron_delivery): the
        # seeded brief must not read as an assistant turn, or the user's first
        # in-thread reply produces assistant→user→... off a phantom assistant
        # message. Pass the seed user_id so the mirror resolves the exact
        # thread-keyed session row we just created.
        mirror_to_session(
            platform_name,
            str(chat_id),
            f"[Cron delivery: {job.get('name') or job.get('id', 'cron')}]\n{text}",
            source_label="cron",
            thread_id=str(thread_id),
            user_id="system:cron",
            role="user",
        )
        logger.info(
            "Job '%s': opened continuable thread %s on %s:%s and seeded the brief",
            job.get("id", "?"), thread_id, platform_name, chat_id,
        )
    except Exception as e:
        logger.debug(
            "Job '%s': seeding cron thread session failed for %s:%s:%s: %s",
            job.get("id", "?"), platform_name, chat_id, thread_id, e,
        )


def _seed_cron_channel_session(
    job: dict,
    adapter,
    platform_name: str,
    chat_id: str,
    mirror_text: str,
    *,
    is_dm: bool,
    user_id: Optional[str],
    chat_name: Optional[str] = None,
) -> bool:
    """Seed the FLAT (thread_id=None) session for an ``in_channel`` cron delivery.

    The ``in_channel`` surface (D1/D2) delivers the brief flat into the channel
    with no thread, so the continuation surface is the whole-channel /
    whole-DM session keyed ``thread_id=None`` — the same bucket
    ``reply_in_thread: false`` routes an inbound plain reply to.

    Unlike the thread path, the shipped delivery-mirror alone is NOT sufficient
    here: ``mirror_to_session`` only APPENDS to a session that already EXISTS
    (``_find_session_id`` → no-op when none matches), and a flat channel
    ``(…, None)`` row is only created when a human posts a top-level message the
    bot processes — a ``chat_postMessage`` cron delivery never goes through the
    inbound handler, so the row is usually absent and the mirror silently drops
    the brief (verified live: the brief never landed, the reply had no context).
    So we CREATE the flat session row first, exactly like
    ``_seed_cron_thread_session`` does for threads, then mirror into it.

    The session KEY must match what the user's later inbound reply resolves to
    (``build_session_key``):
    - **Channel** (``chat_type="group"``): key is
      ``…:group:<chat_id>:<user_id>`` — user-isolated — so the seed MUST carry
      the **origin's real ``user_id``** (the member who scheduled the job), NOT
      a synthetic ``system:cron`` id, or the reply keys to a different session.
    - **1:1 DM** (``chat_type="dm"``): the key is ``…:dm:<chat_id>`` and does
      NOT embed ``user_id``, so any ``user_id`` resolves to the same session.
    ``chat_type`` mirrors the inbound handler's own choice
    (``"dm" if is_dm else "group"``, ``adapter.py``), so the seeded key is
    byte-identical to the reply's key.

    Returns True if a seed row was created and the brief mirrored, else False
    (caller falls back to the plain mirror). Best-effort — a delivery that
    already succeeded is never failed by a seeding problem.
    """
    text = (mirror_text or "").strip()
    if not text:
        return False
    try:
        from gateway.config import Platform
        from gateway.session import SessionSource

        chat_type = "dm" if is_dm else "group"
        session_store = getattr(adapter, "_session_store", None)
        if session_store is not None:
            try:
                platform_enum = Platform(platform_name.lower())
            except (ValueError, KeyError):
                platform_enum = None
            if platform_enum is not None:
                dest_source = SessionSource(
                    platform=platform_enum,
                    chat_id=str(chat_id),
                    chat_name=chat_name,
                    chat_type=chat_type,
                    user_id=str(user_id) if user_id else None,
                    thread_id=None,  # flat — the whole-channel/DM session
                )
                # Create the flat session row so the mirror has a target and the
                # user's later plain reply joins the SAME session.
                session_store.get_or_create_session(dest_source)

        from gateway.mirror import mirror_to_session

        ok = mirror_to_session(
            platform_name,
            str(chat_id),
            f"[Cron delivery: {job.get('name') or job.get('id', 'cron')}]\n{text}",
            source_label="cron",
            thread_id=None,
            user_id=str(user_id) if user_id else None,
            role="user",
        )
        if ok:
            logger.info(
                "Job '%s': seeded flat in_channel session on %s:%s (chat_type=%s)",
                job.get("id", "?"), platform_name, chat_id, chat_type,
            )
        return bool(ok)
    except Exception as e:
        logger.debug(
            "Job '%s': seeding in_channel session failed for %s:%s: %s",
            job.get("id", "?"), platform_name, chat_id, e,
        )
        return False


def _cron_job_origin_log_suffix(job: dict) -> str:
    """Return safe provenance details for security warnings about a cron job.

    The scheduler normally has no live HTTP request object when it detects a
    bad stored ``context_from`` reference. Including the job's saved origin
    makes future probe logs actionable without exposing secrets: platform/chat
    metadata for gateway-created jobs, and optional source-IP fields for API
    surfaces that persist them in origin metadata.
    """
    origin = job.get("origin")
    if not isinstance(origin, dict):
        return ""

    fields = []
    for key in ("platform", "chat_id", "thread_id", "source_ip", "remote", "forwarded_for"):
        value = origin.get(key)
        if value is None:
            continue
        text = str(value).replace("\r", " ").replace("\n", " ").strip()
        if text:
            fields.append(f"origin_{key}={text[:200]!r}")
    return " " + " ".join(fields) if fields else ""


def _plugin_cron_env_var(platform_name: str) -> str:
    """Return the cron home-channel env var registered by a plugin platform.

    Falls through the platform registry so plugins that set
    ``cron_deliver_env_var`` on their ``PlatformEntry`` get cron delivery
    support without editing this module.
    """
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins()  # idempotent
        from gateway.platform_registry import platform_registry
        entry = platform_registry.get(platform_name.lower())
        if entry and entry.cron_deliver_env_var:
            return entry.cron_deliver_env_var
    except Exception:
        pass
    return ""


def _is_known_delivery_platform(platform_name: str) -> bool:
    """Whether ``platform_name`` is a valid cron delivery target.

    Hardcoded built-ins in ``_KNOWN_DELIVERY_PLATFORMS`` are checked first;
    plugin platforms registered via ``PlatformEntry`` are accepted if they
    provide a ``cron_deliver_env_var``.
    """
    name = platform_name.lower()
    if name in _KNOWN_DELIVERY_PLATFORMS:
        return True
    return bool(_plugin_cron_env_var(name))


def _resolve_home_env_var(platform_name: str) -> str:
    """Return the env var name for a platform's cron home channel.

    Built-in platforms are in ``_HOME_TARGET_ENV_VARS``; plugin platforms are
    resolved from the platform registry.
    """
    name = platform_name.lower()
    env_var = _HOME_TARGET_ENV_VARS.get(name)
    if env_var:
        return env_var
    return _plugin_cron_env_var(name)


def _get_home_target_chat_id(platform_name: str) -> str:
    """Return the configured home target chat/room ID for a delivery platform."""
    env_var = _resolve_home_env_var(platform_name)
    if not env_var:
        return ""
    value = os.getenv(env_var, "")
    if not value:
        legacy = _LEGACY_HOME_TARGET_ENV_VARS.get(env_var)
        if legacy:
            value = os.getenv(legacy, "")
    return value


def _get_home_target_thread_id(platform_name: str) -> Optional[str]:
    """Return the optional thread/topic ID for a platform home target.

    Telegram-only override: ``TELEGRAM_CRON_THREAD_ID`` takes precedence over
    ``TELEGRAM_HOME_CHANNEL_THREAD_ID`` for cron delivery. When topic mode is
    enabled, deliveries that land in the root DM (thread_id unset) end up in
    the system-only lobby where the user cannot reply — the gateway returns
    the lobby reminder and drops ``reply_to_message_id`` (#24409). Pointing
    cron at a dedicated topic via this env var lets replies work as expected
    without changing the lobby invariant.
    """
    env_var = _resolve_home_env_var(platform_name)
    if not env_var:
        return None
    if platform_name.lower() == "telegram":
        cron_thread = os.getenv("TELEGRAM_CRON_THREAD_ID", "").strip()
        if cron_thread:
            return cron_thread
    value = os.getenv(f"{env_var}_THREAD_ID", "").strip()
    if not value:
        legacy = _LEGACY_HOME_TARGET_ENV_VARS.get(env_var)
        if legacy:
            value = os.getenv(f"{legacy}_THREAD_ID", "").strip()
    return value or None


def _iter_home_target_platforms(name_filter=None):
    """Iterate built-in + plugin platform names that expose a home channel.

    Used by the ``deliver=origin`` fallback when the job has no origin.

    *name_filter* is an optional ``name -> bool`` predicate applied BEFORE a
    deferred plugin platform is resolved. Resolving runs that plugin's loader,
    which imports the adapter module and the whole vendor SDK behind it, so a
    caller that already knows it will discard the name must be able to say so
    up front rather than paying for an import it throws away. See
    ``cron_delivery_targets``, which gates on the set of connected platforms.
    """
    for name in _HOME_TARGET_ENV_VARS:
        if name_filter is not None and not name_filter(name):
            continue
        yield name
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins()  # idempotent
        from gateway.platform_registry import platform_registry
        for name in platform_registry.known_names():
            # Filter by NAME before resolving. ``plugin_entries()`` would run
            # every deferred loader -- importing all ~18 bundled adapters and
            # their vendor SDKs -- only for this loop to discard the 11 whose
            # names are already built-ins above. Resolving just the survivors
            # yields the identical set: ``get()`` runs one loader, and a
            # built-in name is skipped without importing anything.
            if name in _HOME_TARGET_ENV_VARS:
                continue
            if name_filter is not None and not name_filter(name):
                continue
            entry = platform_registry.get(name)
            if entry and entry.source == "plugin" and entry.cron_deliver_env_var:
                yield name
    except Exception:
        pass


def cron_delivery_targets() -> list[dict]:
    """Return the platforms a cron job can auto-deliver to.

    Single source of truth for any UI (dashboard dropdown, etc.) that lets a
    user pick a cron delivery target. A platform is included when it is a valid
    cron delivery platform AND its gateway is configured (enabled + credentials
    present). Each entry reports whether the platform's home target (the
    room/channel cron posts to) is set — a platform can be configured for
    interactive use but still lack the home target an unattended cron job needs.

    Returns a list of dicts: ``{"id", "name", "home_target_set", "home_env_var"}``
    ordered by the gateway's canonical platform order. Callers should always
    prepend the implicit ``local`` option themselves — it needs no config.
    """
    targets: list[dict] = []
    try:
        from gateway.config import load_gateway_config

        gateway_config = load_gateway_config()
        connected = {p.value for p in gateway_config.get_connected_platforms()}
    except Exception:
        logger.debug("cron_delivery_targets: gateway config unavailable", exc_info=True)
        connected = set()

    # Gate on ``connected`` INSIDE the iterator, not after it yields. The
    # membership test is name-only and already known here, but applying it
    # afterwards meant every unconnected plugin platform was resolved -- and
    # its vendor SDK imported -- purely to be discarded on the next line.
    # Measured with only ``matrix`` connected: 26.5s and 72 extra module roots
    # (microsoft_teams -> jwt -> cryptography -> bcrypt) for a one-item result.
    # That is the same resolve-then-discard shape as the built-in pre-filter
    # inside the iterator; see tests/cron/test_home_target_import_cost.py.
    for name in _iter_home_target_platforms(name_filter=connected.__contains__):
        if not _is_known_delivery_platform(name):
            continue
        env_var = _resolve_home_env_var(name)
        targets.append(
            {
                "id": name,
                "name": name.replace("_", " ").title(),
                "home_target_set": bool(_get_home_target_chat_id(name)),
                "home_env_var": env_var or None,
            }
        )
    return targets


def _resolve_single_delivery_target(job: dict, deliver_value: str) -> Optional[dict]:
    """Resolve one concrete auto-delivery target for a cron job."""

    origin = _resolve_origin(job)

    if deliver_value == "local":
        return None

    if deliver_value == "origin":
        if origin:
            return {
                "platform": origin["platform"],
                "chat_id": str(origin["chat_id"]),
                "thread_id": origin.get("thread_id"),
            }
        # Origin missing (e.g. job created via API/script) — try each
        # platform's home channel as a fallback instead of silently dropping.
        for platform_name in _iter_home_target_platforms():
            chat_id = _get_home_target_chat_id(platform_name)
            if chat_id:
                logger.info(
                    "Job '%s' has deliver=origin but no origin; falling back to %s home channel",
                    job.get("name", job.get("id", "?")),
                    platform_name,
                )
                return {
                    "platform": platform_name,
                    "chat_id": chat_id,
                    "thread_id": _get_home_target_thread_id(platform_name),
                }
        return None

    if ":" in deliver_value:
        platform_name, rest = deliver_value.split(":", 1)
        platform_key = platform_name.lower()

        from tools.send_message_tool import _parse_target_ref

        parsed_chat_id, parsed_thread_id, is_explicit = _parse_target_ref(platform_key, rest)
        if is_explicit:
            chat_id, thread_id = parsed_chat_id, parsed_thread_id
        else:
            chat_id, thread_id = rest, None

        # Resolve human-friendly labels like "Alice (dm)" to real IDs.
        try:
            from gateway.channel_directory import resolve_channel_name
            resolved = resolve_channel_name(platform_key, chat_id)
            if resolved:
                parsed_chat_id, parsed_thread_id, resolved_is_explicit = _parse_target_ref(platform_key, resolved)
                if resolved_is_explicit:
                    chat_id = parsed_chat_id
                    if parsed_thread_id is not None:
                        thread_id = parsed_thread_id
                else:
                    chat_id = resolved
        except Exception:
            pass

        return {
            "platform": platform_name,
            "chat_id": chat_id,
            "thread_id": thread_id,
        }

    platform_name = deliver_value
    if origin and origin.get("platform") == platform_name:
        return {
            "platform": platform_name,
            "chat_id": str(origin["chat_id"]),
            "thread_id": origin.get("thread_id"),
        }

    if not _is_known_delivery_platform(platform_name):
        return None
    chat_id = _get_home_target_chat_id(platform_name)
    if not chat_id:
        return None

    return {
        "platform": platform_name,
        "chat_id": chat_id,
        "thread_id": _get_home_target_thread_id(platform_name),
    }


def _normalize_deliver_value(deliver) -> str:
    """Normalize a stored/submitted ``deliver`` value to its canonical string form.

    The contract is that ``deliver`` is a string (``"local"``, ``"origin"``,
    ``"telegram"``, ``"telegram:-1001:17"``, or comma-separated combinations).
    Historically some callers — MCP clients passing an array, direct edits of
    ``jobs.json``, or stale code paths — have stored a list/tuple like
    ``["telegram"]``.  ``str(["telegram"])`` would serialize to the literal
    string ``"['telegram']"``, which is not a known platform and fails
    resolution silently.  Flatten lists/tuples into a comma-separated string
    so both forms work.  Returns ``"local"`` for anything falsy.
    """
    if deliver is None or deliver == "":
        return "local"
    if isinstance(deliver, (list, tuple)):
        parts = [str(p).strip() for p in deliver if str(p).strip()]
        return ",".join(parts) if parts else "local"
    return str(deliver)


# Routing intent tokens — resolved at fire time, not create time, so a
# job created before Telegram was wired up will pick up Telegram once it
# comes online.  ``all`` expands into the set of connected platforms
# (those with a configured home chat_id) in _expand_routing_tokens.
_ROUTING_TOKENS = frozenset({"all"})


def _expand_routing_tokens(part: str) -> List[str]:
    """Expand a routing-intent token to concrete platform names.

    ``all`` expands to every platform in ``_iter_home_target_platforms()``
    that has a configured home chat_id right now.  Unknown / non-token
    values pass through unchanged as a single-element list, so the caller
    can treat every token uniformly.
    """
    token = part.lower()
    if token not in _ROUTING_TOKENS:
        return [part]
    expanded: List[str] = []
    for platform_name in _iter_home_target_platforms():
        if _get_home_target_chat_id(platform_name):
            expanded.append(platform_name)
    return expanded


def _resolve_delivery_targets(job: dict) -> List[dict]:
    """Resolve all concrete auto-delivery targets for a cron job.

    Accepts the legacy comma-separated ``deliver`` string plus the
    ``all`` routing-intent token, which expands to every platform with
    a configured home channel.  Tokens may be combined with explicit
    targets: ``origin,all`` and ``all,telegram:-100:17`` both work.
    Duplicate (platform, chat_id, thread_id) tuples are collapsed by the
    existing dedup pass.
    """
    deliver = _normalize_deliver_value(job.get("deliver", "local"))
    if deliver == "local":
        return []

    raw_parts = [p.strip() for p in deliver.split(",") if p.strip()]

    # Expand routing intents.
    parts: List[str] = []
    for raw in raw_parts:
        parts.extend(_expand_routing_tokens(raw))

    seen = set()
    targets = []
    for part in parts:
        target = _resolve_single_delivery_target(job, part)
        if target:
            key = (target["platform"].lower(), str(target["chat_id"]), target.get("thread_id"))
            if key not in seen:
                seen.add(key)
                targets.append(target)
    return targets


def _resolve_delivery_target(job: dict) -> Optional[dict]:
    """Resolve the concrete auto-delivery target for a cron job, if any."""
    targets = _resolve_delivery_targets(job)
    return targets[0] if targets else None


# Media extension sets — audio routing is centralized in gateway.platforms.base
# via should_send_media_as_audio() so Telegram-specific rules stay in one place.
_VIDEO_EXTS = frozenset({'.mp4', '.mov', '.avi', '.mkv', '.webm', '.3gp'})
_IMAGE_EXTS = frozenset({'.jpg', '.jpeg', '.png', '.webp', '.gif'})


def _send_media_via_adapter(
    adapter,
    chat_id: str,
    media_files: list,
    metadata: dict | None,
    loop,
    job: dict,
    platform=None,
) -> None:
    """Send extracted MEDIA files as native platform attachments via a live adapter.

    Routes each file to the appropriate adapter method (send_voice, send_image_file,
    send_video, send_document) based on file extension — mirroring the routing logic
    in ``BasePlatformAdapter._process_message_background``.
    """
    from pathlib import Path

    from gateway.platforms.base import BasePlatformAdapter, should_send_media_as_audio

    media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)

    for media_path, _is_voice in media_files:
        try:
            ext = Path(media_path).suffix.lower()
            route_platform = platform if platform is not None else getattr(adapter, "platform", None)
            if should_send_media_as_audio(route_platform, ext, is_voice=_is_voice):
                coro = adapter.send_voice(chat_id=chat_id, audio_path=media_path, metadata=metadata)
            elif ext in _VIDEO_EXTS:
                coro = adapter.send_video(chat_id=chat_id, video_path=media_path, metadata=metadata)
            elif ext in _IMAGE_EXTS:
                coro = adapter.send_image_file(chat_id=chat_id, image_path=media_path, metadata=metadata)
            else:
                coro = adapter.send_document(chat_id=chat_id, file_path=media_path, metadata=metadata)

            from agent.async_utils import safe_schedule_threadsafe
            future = safe_schedule_threadsafe(coro, loop)
            if future is None:
                logger.warning(
                    "Job '%s': cannot send media %s, gateway loop unavailable",
                    job.get("id", "?"), media_path,
                )
                return
            try:
                result = future.result(timeout=30)
            except TimeoutError:
                future.cancel()
                raise
            if result and not getattr(result, "success", True):
                logger.warning(
                    "Job '%s': media send failed for %s: %s",
                    job.get("id", "?"), media_path, getattr(result, "error", "unknown"),
                )
        except Exception as e:
            logger.warning("Job '%s': failed to send media %s: %s", job.get("id", "?"), media_path, e)


def _confirm_adapter_delivery(send_result) -> bool:
    """Return True only if ``send_result`` unambiguously confirms delivery.

    A live adapter that returns ``None`` (e.g. a swallowed exception, a busy
    platform, or a code path that returns early without producing a
    ``SendResult``) must NOT be treated as success — doing so causes the
    scheduler to log ``"delivered to <chat> via live adapter"`` while the
    gateway never actually sees the message (#47056).

    Likewise, an object missing a ``success`` attribute (e.g. a bare ``dict``
    or a partial mock) is a contract violation: it does not actually tell us
    whether the send succeeded.  Require an explicit, truthy ``success``
    attribute to count as confirmed.
    """
    if send_result is None:
        return False
    if not hasattr(send_result, "success"):
        return False
    return bool(getattr(send_result, "success"))


def _is_channel_dm_topic(
    runtime_adapter: Any,
    chat_id: Any,
    loop: Any,
    job_id: str,
) -> bool:
    """Decide whether an (already-ambiguous) Telegram topic target is a genuine
    Bot API *channel* Direct-Messages topic (route via
    ``direct_messages_topic_id``) rather than a forum-style topic in a private
    chat (route via ``message_thread_id``).

    Callers gate this on the ambiguous shape first
    (``telegram:<positive_chat_id>:<numeric_thread_id>``) — that shape is
    identical for both cases, so shape alone cannot decide (this was the #52060
    regression).  The real signal is the chat *type*: a genuine channel DM topic
    lives on a ``channel`` chat.  Probe the live adapter's ``get_chat_info`` once
    and only return True when the chat is a channel.

    Fails SAFE to ``message_thread_id`` (returns False) for adapters without a
    probe, or any probe error/timeout — that is the pre-#22773 behaviour and the
    correct default for the common forum-topic case.
    """
    # Resolve on the CLASS, not the instance (general pitfall #11): a MagicMock
    # instance auto-creates a truthy ``get_chat_info`` attribute, so an
    # instance-level probe would misclassify test doubles. Real adapters expose
    # the coroutine on the class regardless.
    get_chat_info = getattr(type(runtime_adapter), "get_chat_info", None)
    if not callable(get_chat_info):
        return False
    try:
        from agent.async_utils import safe_schedule_threadsafe

        future = safe_schedule_threadsafe(
            get_chat_info(runtime_adapter, str(chat_id)), loop,  # type: ignore[arg-type]
        )
        if future is None:
            return False
        # Lighter than a send (metadata-only Bot API call), so a shorter bound
        # than the 30s/60s send waits elsewhere in this file is intentional.
        info = future.result(timeout=10)
    except Exception:
        logger.debug(
            "Job '%s': get_chat_info probe failed for chat=%s — "
            "defaulting to message_thread_id routing",
            job_id, chat_id, exc_info=True,
        )
        return False
    is_channel = isinstance(info, dict) and str(info.get("type") or "").lower() == "channel"
    if is_channel:
        logger.info(
            "Job '%s': chat=%s is a channel — routing via direct_messages_topic_id",
            job_id, chat_id,
        )
    return is_channel


def _deliver_result(
    job: dict,
    content: str,
    adapters=None,
    loop=None,
    skip_cron_framing: bool = False,
    *,
    buttons=None,
) -> Optional[str]:
    """
    Deliver job output to the configured target(s) (origin chat, specific platform, etc.).

    When ``adapters`` and ``loop`` are provided (gateway is running), tries to
    use the live adapter first — this supports E2EE rooms (e.g. Matrix) where
    the standalone HTTP path cannot encrypt.  Falls back to standalone send if
    the adapter path fails or is unavailable.

    When ``skip_cron_framing`` is True, the "Cronjob Response" header/footer
    wrapper is omitted. Use this for event-bus subscribers where the cron
    framing is misleading.

    ``buttons``, when provided, is the serializable spec from
    ``events.override_buttons.buttons_for``. It is forwarded only to the
    standalone ``_send_to_platform`` fallback below (the verified production
    path for event-bus deliveries, since ``adapters``/``loop`` are None for
    those callers) and is otherwise inert. Defaults to ``None``, in which
    case this function's behavior is unchanged from before this parameter
    existed.

    Returns None on success, or an error string on failure.
    """
    # Strip internal iteration-tracking markers before they reach any
    # user-facing channel. The event-bus extractors already consumed them
    # from the raw final_response upstream; left in the delivered body they
    # break Telegram HTML parsing and surface as raw JSON noise.
    content = _strip_iteration_markers(content)
    targets = _resolve_delivery_targets(job)
    if not targets:
        deliver_value = _normalize_deliver_value(job.get("deliver", "local"))
        if deliver_value == "local":
            return None  # local-only jobs don't deliver — not a failure
        # deliver=origin with no resolvable origin and no configured home
        # channels: treat as local rather than reporting an error.  CLI-created
        # jobs never capture a {platform, chat_id} origin, so failing here would
        # make every CLI `deliver=origin` (or auto-detect) job emit a spurious
        # "no delivery target resolved" error on every run (#43014).  The output
        # is still persisted in last_output for `cron list`/resume.
        if deliver_value == "origin":
            logger.info(
                "Job '%s': deliver=origin but no origin or home channels — "
                "skipping delivery (output saved in last_output)",
                job.get("name", job.get("id", "?")),
            )
            return None
        msg = f"no delivery target resolved for deliver={deliver_value}"
        logger.warning("Job '%s': %s", job["id"], msg)
        return msg

    from tools.send_message_tool import _send_to_platform
    from gateway.config import load_gateway_config, Platform

    # Optionally wrap the content with a header/footer so the user knows this
    # is a cron delivery.  Wrapping is on by default; set cron.wrap_response: false
    # in config.yaml for clean output. Event-bus subscribers pass
    # skip_cron_framing=True to bypass the wrapper entirely. Per-job
    # wrap_response:false in jobs.json disables framing for that job only.
    wrap_response = True
    user_cfg = None
    try:
        user_cfg = load_config()
        wrap_response = user_cfg.get("cron", {}).get("wrap_response", True)
    except Exception:
        pass

    if "wrap_response" in job:
        wrap_response = bool(job["wrap_response"])

    if skip_cron_framing:
        wrap_response = False

    if wrap_response:
        task_name = job.get("name", job["id"])
        job_id = job.get("id", "")
        delivery_content = (
            f"Cronjob Response: {task_name}\n"
            f"(job_id: {job_id})\n"
            f"-------------\n\n"
            f"{content}\n\n"
            f"To stop or manage this job, send me a new message (e.g. \"stop reminder {task_name}\")."
        )
    else:
        delivery_content = content

    # Extract MEDIA: tags so attachments are forwarded as files, not raw text
    from gateway.platforms.base import BasePlatformAdapter
    media_files, cleaned_delivery_content = BasePlatformAdapter.extract_media(delivery_content)
    media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)

    # Resolve the delivery-mirror gate ONCE (default off). When on, each
    # successful delivery is also appended to the target chat's gateway session
    # transcript so a user reply in that chat sees the cron output in context.
    # Mirror the CLEAN, unwrapped output (not the cron header/footer).
    try:
        mirror_enabled = _cron_mirror_delivery_enabled(job, user_cfg)
    except Exception:
        mirror_enabled = False
    mirror_text = ""
    if mirror_enabled:
        _, mirror_text = BasePlatformAdapter.extract_media(content)
        mirror_text = (mirror_text or "").strip()

    try:
        config = load_gateway_config()
    except Exception as e:
        msg = f"failed to load gateway config: {e}"
        logger.error("Job '%s': %s", job["id"], msg)
        return msg

    delivery_errors = []

    for target in targets:
        platform_name = target["platform"]
        chat_id = target["chat_id"]
        thread_id = target.get("thread_id")

        # Diagnostic: log thread_id for topic-aware delivery debugging
        origin = _resolve_origin(job) or {}
        origin_thread = origin.get("thread_id")
        if origin_thread and not thread_id:
            logger.warning(
                "Job '%s': origin has thread_id=%s but delivery target lost it "
                "(deliver=%s, target=%s)",
                job["id"], origin_thread, job.get("deliver", "local"), target,
            )
        elif thread_id:
            logger.debug(
                "Job '%s': delivering to %s:%s thread_id=%s",
                job["id"], platform_name, chat_id, thread_id,
            )

        # Mirror is scoped to the ORIGIN conversation only. A fan-out / broadcast
        # / home-channel-fallback target is never mirrored (it is not the
        # conversation the job was created in, and may have no session at all).
        mirror_this_target = mirror_enabled and _target_matches_origin(
            origin, platform_name, chat_id, thread_id
        )
        # Pass the origin's user_id so a per-user-isolated group chat resolves to
        # the exact member who scheduled the job — parity with send_message.
        origin_user_id = origin.get("user_id") if mirror_this_target else None

        # Built-in names resolve to their enum member; plugin platform names
        # create dynamic members via Platform._missing_().
        try:
            platform = Platform(platform_name.lower())
        except (ValueError, KeyError):
            msg = f"unknown platform '{platform_name}'"
            logger.warning("Job '%s': %s", job["id"], msg)
            delivery_errors.append(msg)
            continue

        pconfig = config.platforms.get(platform)
        if not pconfig or not pconfig.enabled:
            msg = f"platform '{platform_name}' not configured/enabled"
            logger.warning("Job '%s': %s", job["id"], msg)
            delivery_errors.append(msg)
            continue

        # Prefer the live adapter when the gateway is running — this supports E2EE
        # rooms (e.g. Matrix) where the standalone HTTP path cannot encrypt.
        runtime_adapter = (adapters or {}).get(platform)
        delivered = False
        target_errors = []

        # Continuable cron surface (D1/D2/D6): resolve the delivery surface for
        # this platform generically from its config ``extra``. Default "thread"
        # (today's behaviour, byte-identical). "in_channel" delivers the brief
        # FLAT into the channel (no dedicated thread) so a plain channel reply
        # continues the job in-context via the shared-channel session
        # ``(platform, chat_id, None)`` — the same bucket ``reply_in_thread:
        # false`` routes inbound channel messages to. The key is read
        # generically here (any platform); the ``in_channel`` branch is gated on
        # the adapter capability flag ``supports_inchannel_continuable`` so an
        # unsupported platform fails SAFE to "thread" (Slack is the first
        # consumer; "first consumer ≠ definition").
        surface_mode = "thread"
        try:
            surface_raw = (pconfig.extra or {}).get("cron_continuable_surface")
            if surface_raw is not None and str(surface_raw).strip().lower() == "in_channel":
                surface_mode = "in_channel"
        except Exception:
            surface_mode = "thread"
        in_channel_surface = surface_mode == "in_channel"
        if in_channel_surface and runtime_adapter is not None and not getattr(
            runtime_adapter, "supports_inchannel_continuable", False
        ):
            # Fail safe (D6): platform has no in_channel continuation primitive.
            logger.debug(
                "Job '%s': cron_continuable_surface=in_channel not supported on "
                "%s, using thread",
                job.get("id", "?"), platform_name,
            )
            in_channel_surface = False

        # For an in_channel delivery the flat continuation session is created
        # explicitly below (the shipped mirror only APPENDS to an existing
        # session, and the flat channel row is otherwise absent for a
        # chat_postMessage delivery). ``is_dm`` selects the session chat_type so
        # the seeded key matches the inbound reply's key: a 1:1 DM keys as
        # ``dm`` (Slack DM channel ids start with "D"; or the origin says so),
        # everything else as ``group`` (shared channel). ``inchannel_seeded``
        # suppresses the generic mirror below so the brief is not double-written.
        origin_chat_type = str(origin.get("chat_type") or "").lower()
        is_dm_target = origin_chat_type == "dm" or (
            not origin_chat_type and str(chat_id).startswith("D")
        )
        inchannel_seeded = False

        # Continuable cron (thread-preferred): when mirroring is enabled for the
        # origin target and the gateway is live, try to open a DEDICATED thread
        # for this job and deliver the brief into it. On thread-capable
        # platforms (Telegram/Discord/Slack) the brief + the user's replies live
        # in their own scrollback; the thread-keyed session is seeded so a reply
        # continues with full context. On DM-only platforms (WhatsApp/Signal)
        # create_handoff_thread returns None and we fall back to mirroring into
        # the origin DM session (handled after delivery). Cf. _process_handoff.
        #
        # in_channel surface (D2): SKIP thread creation entirely — leave
        # thread_id=None so the delivery posts flat, then
        # ``_seed_cron_channel_session`` (below) CREATES the shared-channel
        # session and mirrors the brief into it. The shipped mirror alone is
        # NOT enough here: ``mirror_to_session`` only APPENDS to an existing
        # session and a flat ``(platform, chat_id, None)`` row is otherwise
        # absent for a ``chat_postMessage`` delivery, so the seed must create
        # the row first (F5).
        thread_seeded = False
        opened_thread_id: Optional[str] = None
        if (
            mirror_this_target
            and not in_channel_surface
            and runtime_adapter is not None
            and loop is not None
            and not thread_id  # never override an explicit origin thread/topic
        ):
            new_thread_id = _open_continuable_cron_thread(
                job, runtime_adapter, chat_id, loop,
            )
            if new_thread_id:
                # Route THIS delivery into the new thread now (the send needs the
                # thread_id), but defer seeding the thread session until the
                # delivery actually succeeds — otherwise an open-succeeds /
                # deliver-fails case leaves a seeded brief the user never saw,
                # and (worse) suppresses the DM-fallback mirror via thread_seeded.
                thread_id = new_thread_id
                opened_thread_id = new_thread_id

        if runtime_adapter is not None and loop is not None and getattr(loop, "is_running", lambda: False)():
            # Telegram topic routing (#22773, regression fixed #52060): a
            # ``telegram:<positive_chat_id>:<numeric_thread_id>`` cron target is
            # ambiguous — a forum-style topic in a private chat and a genuine
            # Bot API channel Direct-Messages topic share the same shape and
            # need OPPOSITE routing. Disambiguate at delivery time via
            # ``_is_channel_dm_topic`` (see its docstring for the full
            # rationale); ``thread_id`` goes in ``route_metadata`` so the
            # anchorless cron send bypasses the DeliveryRouter's private-chat
            # reply-anchor requirement. Compute the routed metadata ONCE so both
            # the text send (via DeliveryRouter) and the media send agree.
            from gateway.delivery import (
                DeliveryRouter,
                DeliveryTarget,
                _looks_like_int,
                looks_like_telegram_private_chat_id,
            )

            is_ambiguous_telegram_topic = (
                platform == Platform.TELEGRAM
                and thread_id is not None
                and looks_like_telegram_private_chat_id(str(chat_id))
                and _looks_like_int(str(thread_id))
            )
            route_via_dm_topic = is_ambiguous_telegram_topic and _is_channel_dm_topic(
                runtime_adapter, chat_id, loop, job["id"],
            )
            if route_via_dm_topic:
                # Genuine Bot API channel Direct-Messages topic (#22773 mode 2):
                # routed via direct_messages_topic_id, no bare thread_id.
                route_thread_id = None
                route_metadata = {
                    "direct_messages_topic_id": str(thread_id),
                    "job_id": job["id"],
                }
                # Media metadata mirrors the text routing so attachments land in
                # the same DM topic instead of the General lane (#22773).
                media_metadata = {"direct_messages_topic_id": str(thread_id)}
            else:
                # Forum-style topic (private chat / supergroup) or non-topic
                # target: route via message_thread_id (#52060).  Put thread_id in
                # *route_metadata* (not just the DeliveryTarget) deliberately —
                # the DeliveryRouter's private-chat topic detection
                # (gateway/delivery.py) demands a reply anchor when thread_id is
                # absent from metadata; cron deliveries have no inbound reply
                # anchor, so the metadata key bypasses that check and lets the
                # adapter route via a plain message_thread_id.
                route_thread_id = str(thread_id) if thread_id is not None else None
                route_metadata = {"job_id": job["id"]}
                if route_thread_id:
                    route_metadata["thread_id"] = route_thread_id
                media_metadata = {"thread_id": thread_id} if thread_id else None

            try:
                # Send cleaned text (MEDIA tags stripped) — not the raw content.
                # Route through the gateway's DeliveryRouter so the live send
                # gets the same platform-specific routing as live messages —
                # in particular Telegram's three-mode topic routing.  The
                # standalone cron path lacked this, so DM-topic cron deliveries
                # landed in the General topic or were rejected by Bot API 10.0
                # (#22773).
                text_to_send = cleaned_delivery_content.strip()
                adapter_ok = True
                timed_out = False
                if text_to_send:
                    from agent.async_utils import safe_schedule_threadsafe

                    router = DeliveryRouter(config, adapters)
                    route_target = DeliveryTarget(
                        platform=platform,
                        chat_id=str(chat_id),
                        thread_id=route_thread_id,
                        is_explicit=True,
                    )
                    # Pass thread routing via the target (not a bare metadata
                    # "thread_id"): the router only applies its Telegram DM-topic
                    # detection when "thread_id"/"message_thread_id" are absent
                    # from metadata, deriving the routing from target.thread_id
                    # or the explicit direct_messages_topic_id above.
                    future = safe_schedule_threadsafe(
                        router._deliver_to_platform(
                            route_target,
                            text_to_send,
                            route_metadata,
                        ),
                        loop,
                    )
                    if future is None:
                        adapter_ok = False
                        target_errors.append("live adapter event loop scheduling failed")
                    else:
                        send_result = None
                        timeout_handled = False
                        try:
                            send_result = future.result(timeout=60)
                        except TimeoutError:
                            # #38922: a slow confirmation does NOT necessarily
                            # mean the send failed — but we must distinguish two
                            # cases via future.cancel()'s return value:
                            #
                            #   cancel() == False -> the coroutine was already
                            #     running on the gateway loop when the timeout
                            #     fired; the request is in flight on the wire and
                            #     cannot be un-sent.  Re-sending via standalone
                            #     would be a guaranteed DUPLICATE, so treat it as
                            #     delivered (assume-delivered).
                            #
                            #   cancel() == True -> the scheduled callback never
                            #     started executing (loop wedged/backlogged for
                            #     the full 60s), so nothing was sent.  We MUST
                            #     fall through to the standalone path or the
                            #     message is silently dropped (worse than a
                            #     duplicate).
                            cancelled = future.cancel()
                            if cancelled:
                                msg = (
                                    f"live adapter send to {platform_name}:{chat_id} "
                                    "timed out before the coroutine was dispatched"
                                )
                                logger.warning(
                                    "Job '%s': %s, falling back to standalone",
                                    job["id"], msg,
                                )
                                target_errors.append(msg)
                                adapter_ok = False  # fall through to standalone path
                                timeout_handled = True
                            else:
                                timed_out = True
                                timeout_handled = True
                                logger.warning(
                                    "Job '%s': live adapter send to %s:%s timed out "
                                    "after 60s; already dispatched (in flight), "
                                    "assuming delivered (skipping standalone fallback "
                                    "to avoid duplicate)",
                                    job["id"], platform_name, chat_id,
                                )
                        except Exception as ex:
                            # A real send error (not a slow confirmation) — fall
                            # through to the standalone path so the message is
                            # still delivered.
                            target_errors.append(f"live adapter send failed: {ex}")
                            raise

                        if timeout_handled:
                            # The timeout branch above already decided the
                            # outcome (assume-delivered if in flight, or
                            # adapter_ok=False to fall through if never
                            # dispatched).  send_result is None, so skip the
                            # confirmation/thread-fallback inspection below.
                            pass
                        else:
                            # _deliver_to_platform returns either a SendResult
                            # (.success attr) or, when the silence-narration
                            # filter drops the message, a plain dict
                            # {"success": True, "delivered": False, ...}.
                            # Normalize both shapes so a getattr default doesn't
                            # misread a dict, and so a None / success-less object
                            # is NOT counted as delivered (#47056).
                            if isinstance(send_result, dict):
                                send_success = bool(send_result.get("success", False))
                                send_raw_response = send_result.get("raw_response")
                            else:
                                send_success = _confirm_adapter_delivery(send_result)
                                send_raw_response = getattr(send_result, "raw_response", None)

                            if not send_success:
                                if isinstance(send_result, dict):
                                    err = send_result.get("error", "unknown")
                                    shape = "dict"
                                elif send_result is not None:
                                    err = getattr(send_result, "error", None)
                                    shape = type(send_result).__name__
                                else:
                                    err = "no response from adapter"
                                    shape = "None"
                                msg = (
                                    f"live adapter send to {platform_name}:{chat_id} "
                                    f"returned unconfirmed result ({shape}, error={err})"
                                )
                                logger.warning(
                                    "Job '%s': %s, falling back to standalone",
                                    job["id"], msg,
                                )
                                target_errors.append(msg)
                                adapter_ok = False  # fall through to standalone path
                            elif (
                                send_raw_response
                                and thread_id
                                and send_raw_response.get("thread_fallback")
                            ):
                                requested_thread_id = send_raw_response.get("requested_thread_id") or thread_id
                                msg = (
                                    f"configured thread_id {requested_thread_id} for "
                                    f"{platform_name}:{chat_id} was not found; delivered without thread_id"
                                )
                                logger.warning("Job '%s': %s", job["id"], msg)
                                delivery_errors.append(msg)

                # Send extracted media files as native attachments via the live
                # adapter, using the same DM-topic-aware routing as the text send
                # (#22773 — media previously used a bare thread_id and landed in
                # the General lane for private DM topics).  Skip on an in-flight
                # confirmation timeout: the gateway loop is contended, so each
                # media send would also block its 30s budget, and the text
                # payload is already assumed delivered (#38922).  Record the
                # skipped attachments so the drop is visible rather than silently
                # lost.
                if adapter_ok and not timed_out and media_files:
                    _send_media_via_adapter(
                        runtime_adapter,
                        chat_id,
                        media_files,
                        media_metadata,
                        loop,
                        job,
                        platform=platform,
                    )
                elif timed_out and media_files:
                    msg = (
                        f"{len(media_files)} media attachment(s) not delivered to "
                        f"{platform_name}:{chat_id} (live adapter confirmation timed out)"
                    )
                    logger.warning("Job '%s': %s", job["id"], msg)
                    delivery_errors.append(msg)

                if adapter_ok:
                    logger.info("Job '%s': delivered to %s:%s via live adapter", job["id"], platform_name, chat_id)
                    delivered = True
                    # Seed the thread session only now that delivery into it
                    # succeeded (deferred from thread-open above).
                    if opened_thread_id and not thread_seeded:
                        _seed_cron_thread_session(
                            job, runtime_adapter, platform_name, chat_id,
                            opened_thread_id, mirror_text,
                            chat_name=origin.get("chat_name"),
                        )
                        thread_seeded = True
                    # in_channel surface: CREATE + seed the flat channel/DM
                    # session (the shipped mirror only appends to an existing
                    # session — the flat row is otherwise absent for a
                    # chat_postMessage delivery, so the brief would be lost).
                    if in_channel_surface and mirror_this_target and not thread_seeded:
                        inchannel_seeded = _seed_cron_channel_session(
                            job, runtime_adapter, platform_name, chat_id,
                            mirror_text, is_dm=is_dm_target,
                            user_id=origin_user_id,
                            chat_name=origin.get("chat_name"),
                        )
                    _maybe_mirror_cron_delivery(
                        job, platform_name, chat_id, mirror_text,
                        thread_id=thread_id, user_id=origin_user_id,
                        enabled=mirror_this_target and not thread_seeded and not inchannel_seeded,
                    )
            except Exception as e:
                err_msg = f"live adapter delivery to {platform_name}:{chat_id} failed: {e}"
                if not any(err_msg in err for err in target_errors):
                    target_errors.append(err_msg)
                logger.warning(
                    "Job '%s': %s, falling back to standalone",
                    job["id"], err_msg,
                )

        if not delivered:
            # If the interpreter is finalizing (gateway SIGTERM / restart /
            # OOM), scheduling any new delivery is futile — asyncio.run and a
            # fresh ThreadPoolExecutor both raise "cannot schedule new futures
            # after interpreter shutdown". Skip gracefully with a warning
            # rather than emitting an ERROR traceback on every restart-race
            # (#58720, #55924).
            if _interpreter_shutting_down():
                msg = f"delivery to {platform_name}:{chat_id} skipped — interpreter is shutting down"
                logger.warning("Job '%s': %s", job["id"], msg)
                target_errors.append(msg)
                delivery_errors.extend(target_errors)
                continue
            # Standalone path: run the async send in a fresh event loop (safe from any thread)
            coro = _send_to_platform(platform, pconfig, chat_id, cleaned_delivery_content, thread_id=thread_id, media_files=media_files, buttons=buttons)
            try:
                result = asyncio.run(coro)
            except RuntimeError as run_err:
                # asyncio.run() checks for a running loop before awaiting the coroutine;
                # when it raises, the original coro was never started — close it to
                # prevent "coroutine was never awaited" RuntimeWarning, then retry in a
                # fresh thread that has no running loop.
                coro.close()
                # If the RuntimeError is the interpreter-finalization signal,
                # the fresh-thread fallback would fail identically — skip
                # gracefully instead of logging a shutdown-race traceback.
                if _interpreter_shutting_down(run_err):
                    msg = f"delivery to {platform_name}:{chat_id} skipped — interpreter is shutting down"
                    logger.warning("Job '%s': %s", job["id"], msg)
                    target_errors.append(msg)
                    delivery_errors.extend(target_errors)
                    continue
                # The thread-pool fallback can itself raise (SMTP ConnectionError,
                # future.result timeout, etc.). An exception raised inside this
                # `except RuntimeError` block is NOT caught by the sibling
                # `except Exception` below — it would escape _deliver_result()
                # and crash the whole delivery loop, silently skipping every
                # remaining target (#47163). Wrap the fallback in its own
                # try/except so a per-target failure is logged and the loop
                # continues to the next target.
                try:
                    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    try:
                        future = pool.submit(asyncio.run, _send_to_platform(platform, pconfig, chat_id, cleaned_delivery_content, thread_id=thread_id, media_files=media_files, buttons=buttons))
                        result = future.result(timeout=30)
                    finally:
                        pool.shutdown(wait=False)
                except Exception as e:
                    # A shutdown-race here is expected during teardown; downgrade
                    # to a warning so it doesn't read as a genuine failure.
                    if _interpreter_shutting_down(e):
                        msg = f"delivery to {platform_name}:{chat_id} skipped — interpreter is shutting down"
                        logger.warning("Job '%s': %s", job["id"], msg)
                        target_errors.append(msg)
                        delivery_errors.extend(target_errors)
                        continue
                    msg = f"delivery to {platform_name}:{chat_id} failed: {e}"
                    logger.error("Job '%s': %s", job["id"], msg, exc_info=True)
                    target_errors.extend([msg])
                    delivery_errors.extend(target_errors)
                    continue
            except Exception as e:
                msg = f"delivery to {platform_name}:{chat_id} failed: {e}"
                logger.error("Job '%s': %s", job["id"], msg, exc_info=True)
                target_errors.extend([msg])
                delivery_errors.extend(target_errors)
                continue

            if result and result.get("error"):
                msg = f"delivery error: {result['error']}"
                logger.error("Job '%s': %s", job["id"], msg)
                target_errors.extend([msg])
                delivery_errors.extend(target_errors)
                continue

            logger.info("Job '%s': delivered to %s:%s", job["id"], platform_name, chat_id)
            _maybe_mirror_cron_delivery(
                job, platform_name, chat_id, mirror_text,
                thread_id=thread_id, user_id=origin_user_id,
                enabled=mirror_this_target and not thread_seeded,
            )

    if delivery_errors:
        return "; ".join(delivery_errors)
    return None


_DEFAULT_SCRIPT_TIMEOUT = 3600  # seconds (1 hour)
# Backward-compatible module override used by tests and emergency monkeypatches.
_SCRIPT_TIMEOUT = _DEFAULT_SCRIPT_TIMEOUT
_RUN_CLAIM_HEARTBEAT_SECONDS = 60.0


def _get_script_timeout() -> int:
    """Resolve cron pre-run script timeout from module/env/config with a safe default."""
    if _SCRIPT_TIMEOUT != _DEFAULT_SCRIPT_TIMEOUT:
        try:
            timeout = int(float(_SCRIPT_TIMEOUT))
            if timeout > 0:
                return timeout
        except Exception:
            logger.warning("Invalid patched _SCRIPT_TIMEOUT=%r; using env/config/default", _SCRIPT_TIMEOUT)

    env_value = os.getenv("HERMES_CRON_SCRIPT_TIMEOUT", "").strip()
    if env_value:
        try:
            timeout = int(float(env_value))
            if timeout > 0:
                return timeout
        except Exception:
            logger.warning("Invalid HERMES_CRON_SCRIPT_TIMEOUT=%r; using config/default", env_value)

    try:
        cfg = load_config() or {}
        cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
        configured = cron_cfg.get("script_timeout_seconds")
        if configured is not None:
            timeout = int(float(configured))
            if timeout > 0:
                return timeout
    except Exception as exc:
        logger.debug("Failed to load cron script timeout from config: %s", exc)

    return _DEFAULT_SCRIPT_TIMEOUT


def _resolve_bash() -> str | None:
    """Locate a bash capable of running HERMES_HOME/scripts/*.sh.

    On Windows, PATH order often puts the WSL launcher
    (System32\\bash.exe) or its WindowsApps stub ahead of Git Bash.
    WSL bash runs the script inside the Linux VM, where a ``C:\\...``
    script path does not resolve — bash exits 127 with a mangled-path
    "No such file or directory".  Delegates to the shared
    ``resolve_windows_git_bash`` helper, which prefers Git Bash
    explicitly and refuses the WSL launcher — the same resolution the
    terminal tool uses, including the HERMES_GIT_BASH_PATH override and
    Hermes' portable Git install.
    """
    if sys.platform != "win32":
        return shutil.which("bash") or (
            "/bin/bash" if os.path.isfile("/bin/bash") else None
        )
    return resolve_windows_git_bash()


def _read_windows_pyvenv_cfg(venv_dir: Path) -> dict[str, str]:
    cfg_path = venv_dir / "pyvenv.cfg"
    try:
        lines = cfg_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    parsed: dict[str, str] = {}
    for raw in lines:
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        parsed[key.strip().lower()] = value.strip()
    return parsed


def _windows_cron_python_invocation(python_exe: str) -> tuple[str, dict[str, str]]:
    """Return an output-capable hidden Python invocation for Windows scripts.

    Cron scripts capture stdout/stderr, so using ``pythonw.exe`` directly can
    lose script output.  uv-created venv ``python.exe`` launchers are also a
    problem: even with CREATE_NO_WINDOW, the launcher can re-exec the base
    console interpreter and flash a visible window.  For uv venvs, bypass the
    launcher and run the base ``python.exe`` directly with the venv paths
    overlaid in the environment.
    """
    if sys.platform != "win32":
        return python_exe, {}

    interpreter = Path(python_exe)
    venv_dir = interpreter.parent.parent
    env_overlay: dict[str, str] = {}

    if interpreter.name.lower() == "pythonw.exe":
        sibling = interpreter.with_name("python.exe")
        if sibling.exists():
            interpreter = sibling

    cfg = _read_windows_pyvenv_cfg(venv_dir)
    home = cfg.get("home", "")
    site_packages = venv_dir / "Lib" / "site-packages"
    if "uv" in cfg and home:
        base_python = Path(home) / "python.exe"
        if base_python.exists() and site_packages.exists():
            interpreter = base_python
            env_overlay["VIRTUAL_ENV"] = str(venv_dir)
            pythonpath_entries = [
                str(Path(__file__).resolve().parents[1]),
                str(site_packages),
            ]
            existing_pythonpath = os.environ.get("PYTHONPATH", "")
            if existing_pythonpath:
                pythonpath_entries.append(existing_pythonpath)
            env_overlay["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)

    return str(interpreter), env_overlay


def _run_job_script(script_path: str, timeout_s=None) -> tuple[bool, str]:
    """Execute a cron job's data-collection script and capture its output.

    ``timeout_s`` (per-job ``script_timeout_seconds`` field) overrides the
    global env/config script timeout when it is a positive number — long
    deterministic jobs like the nightly test gate need more than the
    120s default without raising the ceiling for every script.

    Scripts must reside within HERMES_HOME/scripts/.  Both relative and
    absolute paths are resolved and validated against this directory to
    prevent arbitrary script execution via path traversal or absolute
    path injection.

    Supported interpreters (chosen by file extension):

    * ``.sh`` / ``.bash`` — run with ``/bin/bash``
    * anything else — run with the current Python interpreter
      (``sys.executable``), preserving the original behaviour for
      Python-based pre-check and data-collection scripts.

    Shell support lets ``no_agent=True`` jobs ship classic bash watchdogs
    (the `memory-watchdog.sh` pattern) without wrapping them in Python.

    Subprocess environment is passed through ``_sanitize_subprocess_env`` so
    provider credentials and other Hermes-managed secrets are not inherited
    (SECURITY.md §2.3), matching terminal and MCP child processes.

    Args:
        script_path: Path to the script.  Relative paths are resolved
            against HERMES_HOME/scripts/.  Absolute and ~-prefixed paths
            are also validated to ensure they stay within the scripts dir.

    Returns:
        (success, output) — on failure *output* contains the error message so the
        LLM can report the problem to the user.
    """
    scripts_dir = _get_hermes_home() / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir_resolved = scripts_dir.resolve()

    raw = Path(script_path).expanduser()
    if raw.is_absolute():
        path = raw.resolve()
    else:
        path = (scripts_dir / raw).resolve()

    # Guard against path traversal, absolute path injection, and symlink
    # escape — scripts MUST reside within HERMES_HOME/scripts/.
    try:
        path.relative_to(scripts_dir_resolved)
    except ValueError:
        return False, (
            f"Blocked: script path resolves outside the scripts directory "
            f"({scripts_dir_resolved}): {script_path!r}"
        )

    if not path.exists():
        return False, f"Script not found: {path}"
    if not path.is_file():
        return False, f"Script path is not a file: {path}"

    script_timeout = None
    if timeout_s is not None:
        try:
            script_timeout = int(float(timeout_s))
        except (TypeError, ValueError):
            script_timeout = None
        if script_timeout is not None and script_timeout <= 0:
            script_timeout = None
    if script_timeout is None:
        script_timeout = _get_script_timeout()

    # Re-read .env fresh for every script run, mirroring the agent-job path
    # (see the identical block in _process_job): script subprocesses inherit
    # the GATEWAY's os.environ below, and without this reload a rotated
    # secret in profiles/<active>/.env is invisible until a gateway restart.
    # Bit for real on 2026-07-11: HERMES_GITHUB_TOKEN was rotated in .env but
    # devflow-pr-build-poll kept 401'ing with the launch-inherited dead token.
    # Same env-staleness class as the 2026-07-10 TELEGRAM_HOME_CHANNEL miss.
    try:
        from hermes_cli.env_loader import (
            load_hermes_dotenv,
            reset_secret_source_cache,
        )
        reset_secret_source_cache()
        load_hermes_dotenv(hermes_home=_get_hermes_home())
    except Exception as e:
        # A broken .env reload must not block a script that may not even
        # need env at all; the stale-env behavior is the pre-2026-07-11
        # status quo, not a new failure.
        logger.warning("Script-slot .env reload failed (continuing with process env): %s", e)

    # Pick an interpreter by extension.  Bash for .sh/.bash, Python for
    # everything else.  We deliberately do NOT honour the file's own
    # shebang: the scripts dir is trusted, but keeping the interpreter
    # choice explicit here keeps the allowed surface small and auditable.
    suffix = path.suffix.lower()
    if suffix in {".sh", ".bash"}:
        # Resolve bash dynamically so Windows (Git Bash) and Linux/macOS
        # all work.  On native Windows without Git for Windows installed
        # _resolve_bash returns None — fall back to a clear error rather
        # than a FileNotFoundError with a confusing "[WinError 2]"
        # traceback.
        _bash = _resolve_bash()
        if _bash is None:
            return False, (
                f"Cannot run .sh/.bash script {path.name!r}: bash not found on PATH. "
                "On Windows, install Git for Windows (which ships Git Bash) "
                "or rewrite the script as Python (.py)."
        )
        argv = [_bash, str(path)]
        env_overlay: dict[str, str] = {}
    else:
        python_exe, env_overlay = _windows_cron_python_invocation(sys.executable)
        argv = [python_exe, str(path)]

    try:
        from tools.environments.local import _sanitize_subprocess_env

        env = _sanitize_subprocess_env(os.environ.copy())
        env.update(env_overlay)
        # run_text_capture, not capture_output=True: a script slot runs an
        # arbitrary user script, and scripts routinely shell out again (a .sh
        # that calls curl, a .py that drives git). On Windows the grandchild
        # inherits the capture pipe handles and keeps the write end open, so
        # the pipe never reaches EOF — subprocess.run kills only the direct
        # child (bash/python) at ``script_timeout`` and then blocks forever
        # re-draining. That wedges the scheduler thread, not just this job.
        # Temp-file capture has no pipes to drain, so the budget holds.
        # It also applies CREATE_NO_WINDOW itself (the console-flash flag
        # windows_hide_flags supplied here) and decodes as utf-8/replace,
        # which is what the encoding/errors kwargs asked for.
        result = run_text_capture(
            argv,
            timeout=script_timeout,
            cwd=str(path.parent),
            env=env,
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        # Redact secrets from both stdout and stderr before any return path.
        try:
            from agent.redact import redact_sensitive_text
            stdout = redact_sensitive_text(stdout)
            stderr = redact_sensitive_text(stderr)
        except Exception as e:
            logger.warning("Failed to redact sensitive text from output: %s", e)
            stdout = "[REDACTED - redaction failed]"
            stderr = "[REDACTED - redaction failed]"

        if result.returncode != 0:
            parts = [f"Script exited with code {result.returncode}"]
            if stderr:
                parts.append(f"stderr:\n{stderr}")
            if stdout:
                parts.append(f"stdout:\n{stdout}")
            return False, "\n".join(parts)

        return True, stdout

    except subprocess.TimeoutExpired:
        return False, f"Script timed out after {script_timeout}s: {path}"
    except Exception as exc:
        return False, f"Script execution failed: {exc}"


def _run_job_script_with_claim_heartbeat(
    job: dict, script_path: str
) -> tuple[bool, str]:
    """Run a cron script while keeping its owned one-shot claim fresh.

    Script execution is synchronous and may legitimately outlive the stale
    claim TTL.  Without a concurrent heartbeat, another scheduler process can
    mistake the live run for a dead owner and dispatch the same one-shot again.
    Recurring jobs and unclaimed/manual runs have no durable one-shot claim and
    therefore use the ordinary script path without starting a thread.

    The claim owner is captured from the dispatched job and never re-read from
    storage.  ``heartbeat_run_claim`` compares that stable owner before every
    refresh, so a stale runner cannot extend a replacement owner's claim.
    """
    schedule = job.get("schedule")
    claim = job.get("run_claim")
    owner = str(claim.get("by") or "") if isinstance(claim, dict) else ""
    # Fork: honour the per-job script_timeout_seconds override on every
    # branch of this wrapper (see _run_job_script's timeout_s parameter).
    _timeout_s = job.get("script_timeout_seconds")
    if not (
        isinstance(schedule, dict)
        and schedule.get("kind") == "once"
        and owner
    ):
        return _run_job_script(script_path, timeout_s=_timeout_s)

    job_id = str(job.get("id") or "")
    stop = threading.Event()
    heartbeat_context = contextvars.copy_context()

    def _heartbeat_loop() -> None:
        while not stop.wait(_RUN_CLAIM_HEARTBEAT_SECONDS):
            try:
                heartbeat_run_claim(job_id, expected_owner=owner)
            except Exception:
                logger.debug(
                    "Job '%s': script run_claim heartbeat failed",
                    job_id,
                    exc_info=True,
                )

    heartbeat_thread = threading.Thread(
        target=heartbeat_context.run,
        args=(_heartbeat_loop,),
        name="cron-script-claim-heartbeat",
        daemon=True,
    )
    try:
        heartbeat_thread.start()
    except Exception:
        logger.debug(
            "Job '%s': could not start script run_claim heartbeat",
            job_id,
            exc_info=True,
        )
        return _run_job_script(script_path, timeout_s=_timeout_s)

    try:
        return _run_job_script(script_path, timeout_s=_timeout_s)
    finally:
        stop.set()
        # Event.wait() wakes immediately.  Keep completion bounded if the
        # heartbeat is already waiting on another process's jobs-file lock.
        heartbeat_thread.join(timeout=1.0)


def _parse_wake_gate(script_output: str) -> bool:
    """Parse the last non-empty stdout line of a cron job's pre-check script
    as a wake gate.

    The convention (ported from nanoclaw #1232): if the last stdout line is
    JSON like ``{"wakeAgent": false}``, the agent is skipped entirely — no
    LLM run, no delivery. Any other output (non-JSON, missing flag, gate
    absent, or ``wakeAgent: true``) means wake the agent normally.

    Returns True if the agent should wake, False to skip.
    """
    if not script_output:
        return True
    stripped_lines = [line for line in script_output.splitlines() if line.strip()]
    if not stripped_lines:
        return True
    last_line = stripped_lines[-1].strip()
    try:
        gate = json.loads(last_line)
    except (json.JSONDecodeError, ValueError):
        return True
    if not isinstance(gate, dict):
        return True
    return gate.get("wakeAgent", True) is not False


def _build_job_prompt(job: dict, prerun_script: Optional[tuple] = None) -> str:
    """Build the effective prompt for a cron job, optionally loading one or more skills first.

    Args:
        job: The cron job dict.
        prerun_script: Optional ``(success, stdout)`` from a script that has
            already been executed by the caller (e.g. for a wake-gate check).
            When provided, the script is not re-executed and the cached
            result is used for prompt injection. When omitted, the script
            (if any) runs inline as before.
    """
    user_prompt = str(job.get("prompt") or "")
    prompt = user_prompt
    skills = job.get("skills")
    # True when runtime-collected DATA (script stdout, upstream-job output)
    # has been injected into the prompt. Data content legitimately quotes
    # command-shape strings (a triage feed ingesting a bug report that
    # pastes `rm -rf /`), so it must not be scanned with the strict
    # user-prompt pattern set — see _scan_assembled_cron_prompt.
    has_injected_data = False

    # Run data-collection script if configured, inject output as context.
    script_path = job.get("script")
    if script_path:
        if prerun_script is not None:
            success, script_output = prerun_script
        else:
            success, script_output = _run_job_script(
                script_path, timeout_s=job.get("script_timeout_seconds")
            )
        if success:
            if script_output:
                prompt = (
                    "## Script Output\n"
                    "The following data was collected by a pre-run script. "
                    "Use it as context for your analysis.\n\n"
                    f"```\n{script_output}\n```\n\n"
                    f"{prompt}"
                )
                has_injected_data = True
            else:
                # Script produced no output — nothing to report, skip AI call.
                return None
        else:
            prompt = (
                "## Script Error\n"
                "The data-collection script failed. Report this to the user.\n\n"
                f"```\n{script_output}\n```\n\n"
                f"{prompt}"
            )
            has_injected_data = True

    # Inject output from referenced cron jobs as context.
    context_from = job.get("context_from")
    if context_from:
        # Resolve the cron output dir dynamically (per-profile, #4707) so it
        # tracks the active HERMES_HOME, matching where save_job_output() wrote.
        # (cron.jobs.OUTPUT_DIR is only a static back-compat snapshot;
        # get_cron_output_dir() is the dynamic resolver.)
        from cron.jobs import get_cron_output_dir
        output_dir = get_cron_output_dir()
        if isinstance(context_from, str):
            context_from = [context_from]
        for source_job_id in context_from:
            # Guard against path traversal — valid job IDs are 12-char hex strings
            if not source_job_id or not all(c in "0123456789abcdef" for c in source_job_id):
                logger.warning(
                    "context_from: skipping invalid job_id %r for job_id=%r name=%r%s",
                    source_job_id,
                    job.get("id"),
                    job.get("name"),
                    _cron_job_origin_log_suffix(job),
                )
                continue
            try:
                job_output_dir = output_dir / source_job_id
                if not job_output_dir.exists():
                    continue  # silent skip — no output yet
                output_files = sorted(
                    job_output_dir.glob("*.md"),
                    key=lambda f: f.stat().st_mtime,
                    reverse=True,
                )
                if not output_files:
                    continue  # silent skip — no output yet
                latest_output = output_files[0].read_text(encoding="utf-8").strip()
                # Truncate to 8K characters to avoid prompt bloat
                _MAX_CONTEXT_CHARS = 8000
                if len(latest_output) > _MAX_CONTEXT_CHARS:
                    latest_output = latest_output[:_MAX_CONTEXT_CHARS] + "\n\n[... output truncated ...]"
                if latest_output:
                    prompt = (
                        f"## Output from job '{source_job_id}'\n"
                        "The following is the most recent output from a preceding "
                        "cron job. Use it as context for your analysis.\n\n"
                        f"```\n{latest_output}\n```\n\n"
                        f"{prompt}"
                    )
                    has_injected_data = True
                else:
                    continue  # silent skip — empty output
            except (OSError, PermissionError) as e:
                logger.warning("context_from: failed to read output for job %r: %s", source_job_id, e)
                # silent skip — do not pollute the prompt with error messages

    # Always prepend cron execution guidance so the agent knows how
    # delivery works and can suppress delivery when appropriate.
    cron_hint = (
        "[IMPORTANT: You are running as a scheduled cron job. "
        "DELIVERY: Your final response will be automatically delivered "
        "to the user — do NOT use send_message or try to deliver "
        "the output yourself. Just produce your report/output as your "
        "final response and the system handles the rest. "
        "SILENT: If there is genuinely nothing new to report, respond "
        "with exactly \"[SILENT]\" (nothing else) to suppress delivery. "
        "Never combine [SILENT] with content — either report your "
        "findings normally, or say [SILENT] and nothing more.]\n\n"
    )
    prompt = cron_hint + prompt
    if skills is None:
        legacy = job.get("skill")
        skills = [legacy] if legacy else []
    elif isinstance(skills, str):
        skills = [skills]

    skill_names = [str(name).strip() for name in skills if str(name).strip()]
    if not skill_names:
        return _scan_assembled_cron_prompt(
            prompt,
            job,
            has_skills=False,
            has_injected_data=has_injected_data,
            user_prompt=user_prompt,
        )

    from tools.skills_tool import skill_view
    from tools.skill_usage import bump_use
    from agent.skill_bundles import build_bundle_invocation_message, resolve_bundle_command_key
    from agent.skill_utils import normalize_skill_lookup_name

    parts = []
    skipped: list[str] = []
    for skill_name in skill_names:
        # Cron jobs historically accepted only skill names here, but the CLI/gateway
        # slash-command path lets bundles shadow skills with the same slug. Mirror
        # that behavior so `skills: ["my-bundle"]` expands bundle members instead
        # of being treated as a missing skill.
        bundle_key = resolve_bundle_command_key(skill_name.lstrip("/"))
        if bundle_key:
            bundle_payload = build_bundle_invocation_message(
                bundle_key,
                user_instruction="",
                task_id=str(job.get("id") or "") or None,
            )
            if bundle_payload:
                bundle_message, _loaded_bundle_skills, _missing_bundle_skills = bundle_payload
                if parts:
                    parts.append("")
                parts.append(bundle_message)
                continue
            logger.warning(
                "Cron job '%s': bundle '%s' could not load any skills, skipping",
                job.get("name", job.get("id")),
                skill_name,
            )
            skipped.append(skill_name)
            continue

        try:
            loaded = json.loads(skill_view(normalize_skill_lookup_name(skill_name)))
        except (json.JSONDecodeError, TypeError):
            logger.warning("Cron job '%s': skill '%s' returned invalid JSON, skipping", job.get("name", job.get("id")), skill_name)
            skipped.append(skill_name)
            continue
        if not loaded.get("success"):
            error = loaded.get("error") or f"Failed to load skill '{skill_name}'"
            logger.warning("Cron job '%s': skill not found, skipping — %s", job.get("name", job.get("id")), error)
            skipped.append(skill_name)
            continue

        # Bump usage so the curator sees this skill as actively used.
        try:
            bump_use(skill_name)
        except Exception:
            logger.debug("Cron job: failed to bump skill usage for '%s'", skill_name, exc_info=True)

        content = str(loaded.get("content") or "").strip()
        if parts:
            parts.append("")
        parts.extend(
            [
                f'[IMPORTANT: The user has invoked the "{skill_name}" skill, indicating they want you to follow its instructions. The full skill content is loaded below.]',
                "",
                content,
            ]
        )

    if skipped:
        notice = (
            f"[IMPORTANT: The following skill(s) were listed for this job but could not be found "
            f"and were skipped: {', '.join(skipped)}. "
            f"Start your response with a brief notice so the user is aware, e.g.: "
            f"'⚠️ Skill(s) not found and skipped: {', '.join(skipped)}']"
        )
        parts.insert(0, notice)

    if prompt:
        parts.extend(["", f"The user has provided the following instruction alongside the skill invocation: {prompt}"])
    return _scan_assembled_cron_prompt("\n".join(parts), job, has_skills=True)


def _scan_assembled_cron_prompt(
    assembled: str,
    job: dict,
    *,
    has_skills: bool = False,
    has_injected_data: bool = False,
    user_prompt: Optional[str] = None,
) -> str:
    """Scan the fully-assembled cron prompt for injection patterns. Raises
    ``CronPromptInjectionBlocked`` when a match fires so ``run_job`` can
    surface a clear refusal to the operator.

    Plugs the #3968 gap: ``_scan_cron_prompt`` runs on the user-supplied
    prompt at create/update, but skill content is loaded from disk at
    runtime and was never scanned. Since cron runs non-interactively
    (auto-approves tool calls), a malicious skill carrying an injection
    payload bypassed every gate.

    Two pattern tiers, selected by what the assembled prompt CONTAINS,
    not just whether skills are attached:

    - When the assembled prompt is essentially the user prompt + the cron
      hint (no skills, no injected data), the STRICT ``_scan_cron_prompt``
      patterns apply: a bare ``rm -rf /`` in a small directive prompt is a
      smoking gun, not prose.
    - When the assembled prompt includes runtime-loaded content — skill
      markdown (``has_skills=True``) or DATA injected from a job script's
      stdout / an upstream job's output (``has_injected_data=True``) — the
      LOOSER ``_scan_cron_skill_assembled`` pattern set is used: only
      unambiguous prompt-injection directives block; command-shape
      patterns are dropped and invisible unicode is sanitized (stripped +
      logged) rather than blocked, to avoid false-positives that
      permanently kill a job. Skill bodies are vetted at install time by
      ``skills_guard.py``; script output is produced by operator-authored
      code, the same trust class — and data feeds (e.g. a triage bot
      ingesting bug reports) legitimately quote dangerous commands.

    When the looser tier is selected because of injected data only,
    ``user_prompt`` (the raw, pre-assembly prompt) is additionally scanned
    with the STRICT set so the user-authored surface keeps the full
    create/update-time guarantee at runtime (defense-in-depth for legacy
    jobs that predate the create-time scanner).
    """
    from tools.cronjob_tools import _scan_cron_prompt, _scan_cron_skill_assembled

    if has_skills or has_injected_data:
        # Runtime-loaded content (vetted skill markdown and/or data from
        # operator-authored scripts) legitimately contains command-shape
        # strings. Invisible unicode is sanitized (not blocked) so a stray
        # zero-width space can't permanently kill the job; the cleaned
        # prompt is what actually runs.
        cleaned, scan_error = _scan_cron_skill_assembled(assembled)
        assembled = cleaned
        if not scan_error and not has_skills and user_prompt:
            # Data-injection path: keep the strict guarantee on the
            # user-authored prompt itself.
            scan_error = _scan_cron_prompt(user_prompt)
    else:
        scan_error = _scan_cron_prompt(assembled)
    if scan_error:
        job_label = job.get("name") or job.get("id") or "<unknown>"
        logger.warning(
            "Cron job '%s': assembled prompt blocked by injection scanner — %s",
            job_label,
            scan_error,
        )
        raise CronPromptInjectionBlocked(scan_error)
    return assembled


def _guard_job_credential_exfil(job: dict) -> None:
    """Fail closed if a job's stored provider/base_url pair would exfiltrate a
    credential (F8 runtime backstop; CWE-200/CWE-522).

    The model-callable cron tool validates this on create/update, but a job
    persisted before that guard — or written directly to the jobs store —
    reaches the scheduler's provider-resolution sink unchecked. Re-validate the
    EFFECTIVE stored pair with the same guard the tool uses, so a named
    provider's stored key is never paired with an off-host base_url at fire
    time. Raises ``RuntimeError`` (caught by the run_job failure path → the run
    is aborted and reported) when the pair is unsafe; returns ``None`` otherwise.

    Fallback providers come from operator config, not the model-callable job, so
    they are trusted and validated by the caller, not here.
    """
    try:
        from tools.cronjob_tools import _validate_cron_base_url
        err = _validate_cron_base_url(job.get("provider"), job.get("base_url"))
    except Exception as exc:
        # Fail CLOSED: this is the last guard before provider resolution, so an
        # unexpected validator/import error must not silently allow an unvetted
        # pair through. A job that carries no base_url override cannot exfiltrate
        # a stored credential via this path (there is nothing to validate, and
        # the validator would return None), so it still runs — that keeps the
        # overwhelmingly-common no-override jobs from wedging on an unrelated
        # error. But any job that DID set a base_url is refused until the
        # validator can actually vet the pair. Operator fallback providers come
        # from config, not the job, so they are unaffected.
        if job.get("base_url"):
            err = (
                f"could not validate provider/base_url pair "
                f"({exc.__class__.__name__}: {exc}); refusing to run a job with "
                "an unverified base_url override"
            )
        else:
            err = None
    if err:
        job_id = job.get("id")
        logger.error(
            "Job '%s': refusing to run — unsafe provider/base_url pair could "
            "exfiltrate a stored credential: %s",
            job_id, err,
        )
        raise RuntimeError(f"Cron job '{job_id}' blocked for safety: {err}")


# ---------------------------------------------------------------------------
# Observational activity telemetry (enforcement: observe)
# ---------------------------------------------------------------------------
# This adapter only RECORDS what the scheduler already decided. It never gates
# a fire, changes routing, or alters the (success, doc, final_response, error)
# tuple. Every failure below degrades to "no telemetry for this run" and logs
# the exception class only — provider/registry errors can carry credentials or
# response bodies, so exception text must never reach the log.

_ACTIVITY_REGISTRY = None
_ACTIVITY_REGISTRY_LOADED = False
#: tick() dispatches jobs through a ThreadPoolExecutor, so two mapped jobs can
#: reach the first registry load at once. Without this lock the second thread
#: sees LOADED=True while the value is still None and silently loses telemetry.
_ACTIVITY_REGISTRY_LOCK = threading.Lock()

#: Terminal evidence labels per early branch. The plan's ``wakeAgent:false``
#: label is normalized to ``wake_gate_false`` because the telemetry store only
#: accepts single-colon bounded references (``kind:identifier``).
_ACTIVITY_EVIDENCE = {
    "script_missing": "evidence:script_missing",
    "script_failed": "evidence:script_failed",
    "wake_gate_false": "evidence:wake_gate_false",
    "empty_stdout": "evidence:empty_stdout",
    "script_completed": "evidence:script_completed",
    "prompt_injection_blocked": "evidence:prompt_injection_blocked",
    "empty_prompt": "evidence:empty_prompt",
}


def _load_activity_registry():
    """Load the immutable packaged policy registry (seam for tests)."""
    from activity_policy.registry import ActivityRegistry

    return ActivityRegistry.load_default()


def _get_activity_registry():
    """Return the process-cached registry, or None to behave as unmapped."""
    global _ACTIVITY_REGISTRY, _ACTIVITY_REGISTRY_LOADED
    if _ACTIVITY_REGISTRY_LOADED:
        return _ACTIVITY_REGISTRY
    with _ACTIVITY_REGISTRY_LOCK:
        # Re-check inside the lock: a racing thread may have finished the load
        # while this one waited.
        if _ACTIVITY_REGISTRY_LOADED:
            return _ACTIVITY_REGISTRY
        try:
            registry = _load_activity_registry()
        except Exception as exc:
            logger.warning(
                "activity policy registry unavailable: %s", type(exc).__name__
            )
            registry = None
        # Publish the value BEFORE the flag so no thread can observe a
        # "loaded" cache that is still empty.
        _ACTIVITY_REGISTRY = registry
        _ACTIVITY_REGISTRY_LOADED = True
        return registry


def _resolve_cron_activity_policy(job: dict):
    """Resolve explicit ``activity_id`` first, then the exact job-name alias.

    An unknown explicit ID fails closed in the registry; here that only means
    "unmapped", because a policy lookup must never stop a scheduled job.
    """
    registry = _get_activity_registry()
    if registry is None:
        return None
    try:
        explicit = str(job.get("activity_id") or "").strip()
        if explicit:
            return registry.resolve(activity_id=explicit)
        return registry.resolve(alias=str(job.get("name") or ""))
    except Exception as exc:
        logger.warning(
            "activity policy resolution failed for job '%s': %s",
            job.get("id", "?"), type(exc).__name__,
        )
        return None


def _requested_route(job: dict) -> tuple[Optional[str], Optional[str]]:
    """The route the scheduler will actually ask for, per axis.

    Mirrors execution precedence: an explicit per-job pin wins, else the
    creation-time snapshot (what resolution picked when the job was made),
    else the platform default. Recording only the explicit pin would leave
    ``requested_*`` null for every unpinned job — which is most of them —
    and make requested-vs-served drift undetectable, defeating the reason
    the two routes are stored separately.
    """
    def _pick(*candidates: Any) -> Optional[str]:
        for value in candidates:
            text = str(value or "").strip()
            if text:
                return text
        return None

    provider = _pick(job.get("provider"), job.get("provider_snapshot"))
    model = _pick(job.get("model"), job.get("model_snapshot"), os.getenv("HERMES_MODEL"))
    return provider, model


def _open_cron_activity(
    *,
    job: dict,
    policy,
    run_id: str,
    correlation_id: str,
    profile: str,
    effective_hermes_home: str,
):
    """Best-effort open; sanitize failures to exception class and return None."""
    try:
        from activity_telemetry.recorder import ActivityRecorder
        from hermes_constants import get_default_hermes_root

        requested_provider, requested_model = _requested_route(job)

        db_path = Path(get_default_hermes_root()) / "telemetry" / "activity.db"
        # Requested route is the job's declared pre-agent configuration. The
        # SERVED route is recorded only from real responses in
        # conversation_loop.py — never inferred from configuration here.
        return ActivityRecorder.open(
            db_path,
            run_id=run_id,
            correlation_id=correlation_id,
            activity_id=policy.activity_id,
            policy_version=policy.policy_version,
            trigger_source="cron",
            profile=profile,
            effective_hermes_home=effective_hermes_home,
            requested_provider=requested_provider,
            requested_model=requested_model,
        )
    except Exception as exc:
        logger.warning(
            "cron activity telemetry open failed for job '%s': %s",
            job.get("id", "?"), type(exc).__name__,
        )
        return None


def _finish_cron_activity(
    recorder,
    *,
    process: str,
    evidence_refs: tuple = (),
    escalation_reason: Optional[str] = None,
) -> None:
    """Best-effort single terminal enrichment; never alter cron control flow."""
    if recorder is None:
        return
    try:
        from activity_telemetry.schema import OutcomeLayers

        recorder.finish(
            OutcomeLayers(process=process),
            tuple(evidence_refs),
            escalation_reason,
        )
    except Exception as exc:
        logger.warning(
            "cron activity telemetry finish failed: %s", type(exc).__name__
        )


#: cron_triggered attribution for an event-driven wake. The caller names the
#: MECHANISM, not the producer: a wake can be re-queued across ticks (see the
#: re-queue branch in _collect_woken_jobs), so the process that requested it is
#: not necessarily the one whose event this fire answers. The producer is
#: recoverable from the wake_channel INFO line and the dispatcher's activation
#: ledger; the mechanism is what audit.jsonl needs to separate the four causes.
WAKE_TRIGGER_CALLER = "cron.wake_channel"
WAKE_TRIGGER_REASON = "event_wake"


def _collect_woken_jobs(*, exclude_ids: set) -> list:
    """Turn pending event wakes into jobs to execute on this tick.

    Deliberately does NOT advance ``next_run_at``: an event says "there is work
    now", not "the schedule moved". Advancing would let inbound traffic drift a
    job's regular cadence. Each fire emits a ``cron_triggered`` event
    (``reason="event_wake"``) so the run is attributable in audit.jsonl; that
    emit records the transition and must never change it.

    A wake is consumed even when it cannot be *usefully* used (job unknown,
    disabled, or already firing this tick) — otherwise a disabled worker's wake
    would be redelivered on every tick forever.

    The one exception is a job that is currently RUNNING: that wake is
    re-queued, not consumed. See the re-queue branch below for why.

    Never raises into the tick: losing a wake degrades to the deterministic
    reconciler catching the work, while an exception here would stall every
    scheduled job.
    """
    woken_ids = wake_channel.drain_wakes()
    if not woken_ids:
        return []

    try:
        by_id = {j.get("id"): j for j in load_jobs()}
    except Exception as exc:
        logger.warning(
            "cron wake collection failed, dropping %d wake(s): %s",
            len(woken_ids), type(exc).__name__,
        )
        return []

    collected = []
    for job_id in sorted(woken_ids):
        if job_id in exclude_ids:
            continue  # already firing on schedule this tick
        job = by_id.get(job_id)
        if job is None:
            logger.warning("cron wake for unknown job %s — dropped", job_id)
            continue
        if not job.get("enabled"):
            # An operator disabled this on purpose. Unlike trigger_job, a wake
            # never re-enables.
            logger.info("cron wake for disabled job %s — not revived", job_id)
            continue
        if job_id in get_running_job_ids():
            # The job is mid-run, so dispatching now would only hit the
            # duplicate-fire guard and the wake would be lost. Put it back and
            # let a later tick deliver it once the run finishes.
            #
            # Measured 2026-08-18 (canary day 0): dropping it cost every
            # acceleration event dispatch was supposed to buy. The producer and
            # the consumer share a schedule boundary — jobflow-tracker-cycle is
            # `0 */4` and jobflow-matcher `0 */2`, both on the hour — so the
            # tracker's SCORE_REQUESTs landed 10:06:51-54, inside the matcher
            # run that began 10:00:57. Both live wakes were refused, the work
            # waited for the next scheduled run up to two hours later, and the
            # dispatcher's ledger claim aged out unfinished.
            #
            # Bounded without a counter: the channel is a set, so a wake waiting
            # many ticks stays exactly one entry, and a genuinely hung run is
            # ended by the cron wall-clock timeout rather than by this loop.
            # Logged at DEBUG because a busy worker is the normal case and this
            # fires once per tick until the run ends.
            wake_channel.request_wake(
                job_id, caller="cron.tick", reason="requeued_job_running"
            )
            logger.debug(
                "cron wake for %s deferred — job still running; re-queued", job_id
            )
            continue

        # Provenance. Emitted HERE — one event per job that actually fires —
        # and not in wake_channel.request_wake(), which is called once per
        # producer event AND again by the re-queue branch above, so emitting
        # there would count a busy worker's wake once per tick until its run
        # ended. Duplicate producer events also collapse in the channel's set,
        # so request_wake fires more often than the job does; _collect_woken_jobs
        # is the only site where "this job is about to run off its schedule" is
        # true exactly once.
        #
        # previous == new next_run_at is not a placeholder: it is the record
        # that a wake adds a run WITHOUT moving the schedule (see this
        # function's docstring).
        #
        # The try/except is NOT redundant with emit_cron_triggered_safe's own.
        # That one guards the emit's INTERNALS; this one guards the promise
        # this function makes to tick() — that it never raises — which must
        # not depend on a callee keeping its bargain. An unavailable emitter
        # (patched, partially imported, refactored to raise) would otherwise
        # take down every scheduled job on the tick to lose one audit record.
        try:
            emit_cron_triggered_safe(
                job_id=job_id,
                job_name=job.get("name") or job_id,
                caller=WAKE_TRIGGER_CALLER,
                reason=WAKE_TRIGGER_REASON,
                previous_next_run_at=job.get("next_run_at"),
                new_next_run_at=job.get("next_run_at"),
            )
        except Exception:
            logger.exception(
                "cron wake provenance emit failed for job %s — firing anyway",
                job_id,
            )
        collected.append(job)

    if collected:
        logger.info(
            "cron: %d job(s) woken by event: %s",
            len(collected), ", ".join(j.get("name") or j["id"] for j in collected),
        )
    return collected


def run_job(
    job: dict, *, defer_agent_teardown: Optional[list] = None
) -> tuple[bool, str, str, Optional[str]]:
    """Execute a single cron job, applying any per-job profile override.

    Thin wrapper: resolves the job's optional ``profile`` into a context-local
    Hermes-home override (via ``_job_profile_context``) so the entire run —
    no_agent script path included — executes under the profile's HERMES_HOME,
    then delegates to ``_run_job_impl``. Keeping the profile scope here (not
    inside the impl) guarantees the no_agent short-circuit is also profile-scoped.
    """
    job_id = job["id"]
    with _job_profile_context(job_id, job.get("profile")):
        return _run_job_impl(job, defer_agent_teardown=defer_agent_teardown)


def _run_job_impl(
    job: dict, *, defer_agent_teardown: Optional[list] = None
) -> tuple[bool, str, str, Optional[str]]:
    """
    Execute a single cron job.

    ``defer_agent_teardown``: when a caller passes a list, ``run_job`` skips
    the agent's async-resource teardown (``agent.close()`` +
    ``cleanup_stale_async_clients()``) in its ``finally`` block and instead
    appends the live agent to that list. The caller is then responsible for
    calling ``_teardown_cron_agent(agent)`` AFTER it has delivered the result.
    This closes the ordering window in #58720 where delivery ran against a
    torn-down async client (defense-in-depth alongside the interpreter-shutdown
    guard). When ``None`` (the default) teardown happens inline as before, so
    every existing caller is unchanged.

    Returns:
        Tuple of (success, full_output_doc, final_response, error_message)
    """
    job_id = job["id"]
    job_name = str(job.get("name") or job.get("prompt") or job_id or "cron job")

    # Observational telemetry opens here, before any branch, so deterministic
    # and no-work fires are recorded too. Unmapped jobs and every telemetry
    # failure execute exactly as they did before this adapter existed. Resolved
    # inside the profile context so effective_hermes_home reflects the job's
    # profile rather than the scheduler default.
    _activity_recorder = None
    _activity_policy = _resolve_cron_activity_policy(job)
    if _activity_policy is not None:
        try:
            import uuid as _uuid

            _activity_run_id = str(_uuid.uuid4())
            _activity_recorder = _open_cron_activity(
                job=job,
                policy=_activity_policy,
                run_id=_activity_run_id,
                correlation_id=str(job.get("correlation_id") or _activity_run_id),
                profile=str(job.get("profile") or "default").strip() or "default",
                effective_hermes_home=str(_get_hermes_home()),
            )
        except Exception as exc:
            logger.warning(
                "cron activity telemetry setup failed for job '%s': %s",
                job_id, type(exc).__name__,
            )
            _activity_recorder = None

    # ---------------------------------------------------------------
    # no_agent short-circuit — the script IS the job, no LLM involvement.
    # ---------------------------------------------------------------
    # This mirrors the classic "run a bash script on a timer, send its
    # stdout to telegram" watchdog pattern. The agent path is skipped
    # entirely: no AIAgent, no prompt, no tool loop, no token spend.
    #
    # We check this BEFORE importing run_agent / constructing SessionDB so
    # a pure-script tick never pays for the agent machinery it isn't going
    # to use. Keep this block self-contained.
    #
    # Semantics:
    #   - script stdout (trimmed) → delivered verbatim as the final message
    #   - empty stdout            → silent run (no delivery, success=True)
    #   - non-zero exit / timeout → delivered as an error alert, success=False
    #   - wakeAgent=false gate    → treated like empty stdout (silent), since
    #                               the whole point of no_agent is that there
    #                               is no agent to wake
    if job.get("no_agent"):
        script_path = job.get("script")
        if not script_path:
            err = "no_agent=True but no script is set for this job"
            logger.error("Job '%s': %s", job_id, err)
            _finish_cron_activity(
                _activity_recorder,
                process="failed",
                evidence_refs=(_ACTIVITY_EVIDENCE["script_missing"],),
            )
            return False, "", "", err

        # Apply workdir if configured — lets scripts use predictable relative
        # paths. For no_agent jobs this is just the subprocess cwd (not an
        # agent TERMINAL_CWD bridge).
        _job_workdir = (job.get("workdir") or "").strip() or None
        _prior_cwd = None
        if _job_workdir and Path(_job_workdir).is_dir():
            _prior_cwd = os.getcwd()
            try:
                os.chdir(_job_workdir)
            except OSError:
                _prior_cwd = None

        try:
            # Claim-heartbeat wrapper resolves the per-job
            # script_timeout_seconds override internally.
            ok, output = _run_job_script_with_claim_heartbeat(job, script_path)
        finally:
            if _prior_cwd is not None:
                try:
                    os.chdir(_prior_cwd)
                except OSError:
                    pass

        now_iso = _hermes_now().strftime("%Y-%m-%d %H:%M:%S")

        if not ok:
            # Script crashed / timed out / exited non-zero.  Deliver the
            # error so the user knows the watchdog itself broke — silent
            # failure for an alerting job is the worst-case outcome.
            alert = (
                f"⚠ Cron watchdog '{job_name}' script failed\n\n"
                f"{output}\n\n"
                f"Time: {now_iso}"
            )
            doc = (
                f"# Cron Job: {job_name}\n\n"
                f"**Job ID:** {job_id}\n"
                f"**Run Time:** {now_iso}\n"
                f"**Mode:** no_agent (script)\n"
                f"**Status:** script failed\n\n"
                f"{output}\n"
            )
            _finish_cron_activity(
                _activity_recorder,
                process="failed",
                evidence_refs=(_ACTIVITY_EVIDENCE["script_failed"],),
            )
            return False, doc, alert, output

        # Honour the wakeAgent gate as a silent signal — `wakeAgent: false`
        # means "nothing to report this tick", same as empty stdout.
        if not _parse_wake_gate(output):
            logger.info(
                "Job '%s' (no_agent): wakeAgent=false gate — silent run", job_id
            )
            silent_doc = (
                f"# Cron Job: {job_name}\n\n"
                f"**Job ID:** {job_id}\n"
                f"**Run Time:** {now_iso}\n"
                f"**Mode:** no_agent (script)\n"
                f"**Status:** silent (wakeAgent=false)\n"
            )
            _finish_cron_activity(
                _activity_recorder,
                process="no_work",
                evidence_refs=(_ACTIVITY_EVIDENCE["wake_gate_false"],),
            )
            return True, silent_doc, SILENT_MARKER, None

        if not output.strip():
            logger.info("Job '%s' (no_agent): empty stdout — silent run", job_id)
            silent_doc = (
                f"# Cron Job: {job_name}\n\n"
                f"**Job ID:** {job_id}\n"
                f"**Run Time:** {now_iso}\n"
                f"**Mode:** no_agent (script)\n"
                f"**Status:** silent (empty output)\n"
            )
            _finish_cron_activity(
                _activity_recorder,
                process="no_work",
                evidence_refs=(_ACTIVITY_EVIDENCE["empty_stdout"],),
            )
            return True, silent_doc, SILENT_MARKER, None

        doc = (
            f"# Cron Job: {job_name}\n\n"
            f"**Job ID:** {job_id}\n"
            f"**Run Time:** {now_iso}\n"
            f"**Mode:** no_agent (script)\n\n"
            f"---\n\n"
            f"{output}\n"
        )
        # Process-level success only. The script produced output; whether that
        # output was semantically correct or delivered is not evidenced here,
        # so the derived final outcome stays `unknown`.
        _finish_cron_activity(
            _activity_recorder,
            process="succeeded",
            evidence_refs=(_ACTIVITY_EVIDENCE["script_completed"],),
        )
        return True, doc, output, None

    # ---------------------------------------------------------------
    # Wake gate — the inference boundary. This runs BEFORE any model
    # machinery so a "nothing to do" tick costs no AIAgent import, no
    # SessionDB row, and no delivery. The gate used to sit after SessionDB
    # init, which meant every idle poll still opened and migrated state.db
    # and left a cron session behind for work that never happened.
    #
    # The script runs exactly once: its result is threaded into
    # _build_job_prompt below via ``prerun_script``.
    # ---------------------------------------------------------------
    prerun_script = None
    script_path = job.get("script")
    if script_path:
        # Claim-heartbeat wrapper resolves the per-job
        # script_timeout_seconds override internally.
        prerun_script = _run_job_script_with_claim_heartbeat(job, script_path)
        _ran_ok, _script_output = prerun_script
        if _ran_ok and not _parse_wake_gate(_script_output):
            logger.info(
                "Job '%s' (ID: %s): wakeAgent=false, skipping agent run",
                job_name, job_id,
            )
            silent_doc = (
                f"# Cron Job: {job_name}\n\n"
                f"**Job ID:** {job_id}\n"
                f"**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                "Script gate returned `wakeAgent=false` — agent skipped.\n"
            )
            _finish_cron_activity(
                _activity_recorder,
                process="no_work",
                evidence_refs=(_ACTIVITY_EVIDENCE["wake_gate_false"],),
            )
            return True, silent_doc, SILENT_MARKER, None

    # ---------------------------------------------------------------
    # Default (LLM) path — import and construct the agent machinery now
    # that the gate has confirmed there is semantic work to do. Doing these
    # imports here instead of at module top keeps no_agent and no-work ticks
    # from paying for AIAgent / SessionDB construction costs.
    # ---------------------------------------------------------------
    from run_agent import AIAgent

    # Initialize SQLite session store so cron job messages are persisted
    # and discoverable via session_search (same pattern as gateway/run.py).
    #
    # Bounded with its own timeout (separate from HERMES_CRON_TIMEOUT, which
    # only watches the agent's run_conversation below): SessionDB.__init__
    # opens/migrates state.db synchronously and has no timeout of its own
    # against a wedged sqlite3.connect (e.g. a stale flock left by a crashed
    # sibling process). An unbounded hang here is invisible to every other
    # cron safeguard, because it happens BEFORE _submit_with_guard's future
    # exists — the finally block that releases the job from
    # _running_job_ids never runs, so the job stays wedged "running" until
    # the whole gateway process is restarted, silently skipping every
    # scheduled fire in between with "already running — skipping".
    _session_db = None
    try:
        from hermes_state import SessionDB

        # Resolve timeout: env override → config.yaml → default 10s.
        # Mirrors the script_timeout_seconds resolution pattern.
        _session_db_timeout: float | None = None
        _raw_env_timeout = os.getenv("HERMES_CRON_SESSION_DB_TIMEOUT", "").strip()
        if _raw_env_timeout:
            try:
                _session_db_timeout = float(_raw_env_timeout)
            except (ValueError, TypeError):
                logger.warning(
                    "Invalid HERMES_CRON_SESSION_DB_TIMEOUT=%r; using config/default",
                    _raw_env_timeout,
                )
        if _session_db_timeout is None:
            try:
                from hermes_cli.config import load_config
                _cfg = load_config() or {}
                _cron_cfg = _cfg.get("cron", {}) if isinstance(_cfg, dict) else {}
                _configured = _cron_cfg.get("session_db_timeout_seconds")
                if _configured is not None:
                    _session_db_timeout = float(_configured)
            except Exception as exc:
                logger.debug(
                    "Failed to load cron.session_db_timeout_seconds from config: %s",
                    exc,
                )
        if _session_db_timeout is None:
            _session_db_timeout = 10.0

        if _session_db_timeout > 0:
            _session_db_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                _session_db = _session_db_pool.submit(SessionDB).result(timeout=_session_db_timeout)
            finally:
                # Don't wait for a wedged connect() to unwind — abandon the
                # worker thread (same pattern as the agent inactivity timeout
                # further down) rather than blocking shutdown on it too.
                _session_db_pool.shutdown(wait=False)
        else:
            # 0 = unlimited (legacy behavior, opt-in for debugging)
            _session_db = SessionDB()
    except concurrent.futures.TimeoutError:
        # %g, not %.0f: a sub-second timeout rounded to "0s", and 0 is this
        # function's sentinel for *unlimited* — so the log claimed the exact
        # opposite of what had just happened. %g keeps 10.0 as "10" and shows
        # 0.2 as "0.2".
        logger.error(
            "Job '%s': SessionDB init did not return within %gs — proceeding "
            "without a session store for this run instead of blocking it "
            "forever",
            job.get("id", "?"), _session_db_timeout,
        )
    except Exception as e:
        logger.debug("Job '%s': SQLite session store not available: %s", job.get("id", "?"), e)

    try:
        prompt = _build_job_prompt(job, prerun_script=prerun_script)
    except CronPromptInjectionBlocked as block_exc:
        # Assembled prompt (user prompt + loaded skill content) tripped the
        # injection scanner. Refuse to run the agent this tick and surface
        # a clear failure to the operator so they see WHY the scheduled job
        # didn't run and can audit the offending skill.
        logger.warning(
            "Job '%s' (ID: %s): blocked by prompt-injection scanner — %s",
            job_name, job_id, block_exc,
        )
        blocked_doc = (
            f"# Cron Job: {job_name}\n\n"
            f"**Job ID:** {job_id}\n"
            f"**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"**Status:** BLOCKED\n\n"
            "The assembled prompt (user prompt + loaded skill content) tripped "
            "the cron injection scanner and the agent was NOT run.\n\n"
            f"**Scanner result:** {block_exc}\n\n"
            "Audit the skill(s) attached to this job for prompt-injection "
            "payloads or invisible-unicode markers. If the skill is legitimate "
            "and the match is a false positive, rephrase the content to avoid "
            "the threat pattern (`tools/cronjob_tools.py::_CRON_THREAT_PATTERNS`)."
        )
        _finish_cron_activity(
            _activity_recorder,
            process="blocked",
            evidence_refs=(_ACTIVITY_EVIDENCE["prompt_injection_blocked"],),
        )
        return False, blocked_doc, "", str(block_exc)
    if prompt is None:
        logger.info("Job '%s': script produced no output, skipping AI call.", job_name)
        _finish_cron_activity(
            _activity_recorder,
            process="no_work",
            evidence_refs=(_ACTIVITY_EVIDENCE["empty_prompt"],),
        )
        return True, "", SILENT_MARKER, None
    _cron_session_id = f"cron_{job_id}_{_hermes_now().strftime('%Y%m%d_%H%M%S')}"

    # Inference is about to start, so this run now crosses the wake/prompt
    # boundary and has a real session. Deterministic and no-work branches
    # above deliberately never reach here and stay session-less.
    if _activity_recorder is not None:
        _activity_recorder.link_session(_cron_session_id)

    logger.info("Running job '%s' (ID: %s)", job_name, job_id)
    logger.info("Prompt: %s", prompt[:100])

    agent = None

    # Mark this as a cron session so the approval system can apply cron_mode.
    # This env var is process-wide and persists for the lifetime of the
    # scheduler process — every job this process runs is a cron job.
    os.environ["HERMES_CRON_SESSION"] = "1"

    # Use ContextVars for per-job session/delivery state so parallel jobs
    # don't clobber each other's targets (os.environ is process-global).
    from gateway.session_context import set_session_vars, clear_session_vars, _VAR_MAP

    # Cron execution is an internal scheduler context, not a live inbound
    # gateway message. Do not seed HERMES_SESSION_* contextvars from the
    # stored ``origin`` (which is delivery routing metadata, not a sender
    # identity). Several tool consumers branch on these vars during job
    # execution and would otherwise behave as if a real user from the
    # origin chat was driving the agent:
    #   - tools/terminal_tool.py: background-process notification routing
    #     (notify_on_complete / watch_patterns) reads HERMES_SESSION_PLATFORM
    #     and HERMES_SESSION_CHAT_ID to populate watcher_platform / chat_id,
    #     which would route completion notifications to the origin chat
    #     instead of via HERMES_CRON_AUTO_DELIVER_* below.
    #   - tools/tts_tool.py: picks Opus vs MP3 based on
    #     HERMES_SESSION_PLATFORM == "telegram".
    #   - tools/skills_tool.py + agent/prompt_builder.py: per-platform
    #     skill-disable lists and the system-prompt cache key both consume
    #     HERMES_SESSION_PLATFORM.
    #   - tools/send_message_tool.py: mirror source labelling and the
    #     send_message gate read HERMES_SESSION_PLATFORM.
    # Cron output delivery itself reads job["origin"] directly via
    # _resolve_origin(job) and the HERMES_CRON_AUTO_DELIVER_* vars set
    # below, so clearing HERMES_SESSION_* here does not affect delivery.
    _ctx_tokens = set_session_vars(
        platform="",
        chat_id="",
        chat_name="",
        # A cron job cannot receive a completion after its turn ends. We clear the
        # HERMES_SESSION_* routing keys just below, so an async delegation's
        # completion event carries session_key="" — _enrich_async_delegation_routing
        # cannot resolve it and _inject_watch_notification drops it ("no routing
        # metadata"). And by the time a child finishes, run_job has already shipped
        # the job's final response via _deliver_result; there is no turn left to
        # re-enter. (Worse, get_current_session_key() can fall back to the ambient
        # os.environ HERMES_SESSION_KEY, which risks routing a cron subagent's output
        # into an unrelated user chat.)
        #
        # Declaring the channel stateless routes delegate_task to its existing
        # inline/synchronous path, so results return within the job's own turn.
        # See declare_stateless_channel(). Upstream: #53027, #63142.
        async_delivery=False,
    )
    _cron_delivery_vars = (
        "HERMES_CRON_AUTO_DELIVER_PLATFORM",
        "HERMES_CRON_AUTO_DELIVER_CHAT_ID",
        "HERMES_CRON_AUTO_DELIVER_THREAD_ID",
    )
    for _var_name in _cron_delivery_vars:
        _VAR_MAP[_var_name].set("")

    # Per-job working directory.  When set (and validated at create/update
    # time), we point TERMINAL_CWD at it so:
    #   - build_context_files_prompt() picks up AGENTS.md / CLAUDE.md /
    #     .cursorrules from the job's project dir, AND
    #   - the terminal, file, and code-exec tools run commands from there.
    #
    # os.environ["TERMINAL_CWD"] is process-global, so this override is
    # serialized by _terminal_cwd_lock (acquired just below): a workdir job
    # holds it as a writer for its whole run, excluding every other job, while
    # workdir-less jobs hold it as readers and stay parallel with each other.
    # The sequential pool only keeps workdir jobs from overlapping EACH OTHER;
    # the lock is what additionally keeps a concurrently-firing workdir-less
    # parallel-pool job from observing this override and running its shell /
    # file / code-exec commands in the wrong directory.  For workdir-less jobs
    # we leave TERMINAL_CWD untouched — preserves the original behaviour
    # (skip_context_files=True, tools use whatever cwd the scheduler has).
    _job_workdir = (job.get("workdir") or "").strip() or None
    if _job_workdir and not Path(_job_workdir).is_dir():
        # Directory was removed between create-time validation and now.  Log
        # and drop back to old behaviour rather than crashing the job.
        logger.warning(
            "Job '%s': configured workdir %r no longer exists — running without it",
            job_id, _job_workdir,
        )
        _job_workdir = None

    # Snapshot the current env value BEFORE acquiring the lock so the finally
    # below can always restore it, even if an exception fires before we set the
    # override inside the try.  This read can't leak the lock (it precedes the
    # acquire) and is a no-op for workdir-less jobs (they never mutate the env).
    _prior_terminal_cwd = os.environ.get("TERMINAL_CWD", "_UNSET_")

    # Writer status is NOT "has a workdir" — it is "overrides process-global
    # state", which a profile job does too (see _job_mutates_process_globals).
    # Keying on workdir alone let a profile job take the lock as a reader and
    # run beside the parallel pool while its Hermes-home override was live.
    # `job` is used rather than `_job_workdir` so a workdir that vanished
    # between validation and now still degrades to reader for the CWD half,
    # while the profile half is judged on its own.
    _holds_cwd_write = _job_workdir is not None or bool(
        (job.get("profile") or "").strip()
    )
    if _holds_cwd_write:
        _terminal_cwd_lock.acquire_write()
    else:
        _terminal_cwd_lock.acquire_read()

    # Everything after the acquire MUST live inside this try, so the finally
    # below always releases the lock even if the env override or any later
    # statement raises.  A leaked writer would deadlock the whole scheduler
    # (every future job blocks on acquire_*); a leaked reader blocks all
    # future writers.  Acquire itself can't leak (it either blocks or returns).
    try:
        if _job_workdir:
            os.environ["TERMINAL_CWD"] = _job_workdir
            logger.info("Job '%s': using workdir %s", job_id, _job_workdir)

        # Re-read .env and config.yaml fresh every run so provider/key
        # changes take effect without a gateway restart. Route through
        # load_hermes_dotenv (not a bare load_dotenv) and reset the secret-
        # source cache first: startup already applied external secrets and
        # recorded this HERMES_HOME in _APPLIED_HOMES, so a naive reload would
        # re-apply only the .env placeholder and never re-resolve a Bitwarden/
        # BSM-backed secret — leaving cron jobs 401'ing on the placeholder
        # (#33465). Clearing the cache forces the re-pull; the resolved secret
        # overrides the placeholder only when secrets.bitwarden.override_existing
        # is set (mirrors startup), and the Bitwarden value-cache keeps the
        # forced re-pull off the network. load_hermes_dotenv also handles the
        # utf-8/latin-1 encoding fallback internally.
        from hermes_cli.env_loader import (
            load_hermes_dotenv,
            reset_secret_source_cache,
        )
        reset_secret_source_cache()
        load_hermes_dotenv(hermes_home=_get_hermes_home())

        delivery_target = _resolve_delivery_target(job)
        if delivery_target:
            _VAR_MAP["HERMES_CRON_AUTO_DELIVER_PLATFORM"].set(delivery_target["platform"])
            _VAR_MAP["HERMES_CRON_AUTO_DELIVER_CHAT_ID"].set(str(delivery_target["chat_id"]))
            _VAR_MAP["HERMES_CRON_AUTO_DELIVER_THREAD_ID"].set(
                ""
                if delivery_target.get("thread_id") is None
                else str(delivery_target["thread_id"])
            )

        # Model resolution precedence: per-job override > HERMES_MODEL env >
        # config.yaml ``model:`` (string or ``{default: ...}``). The per-job
        # value is intentionally re-read from storage every tick so a
        # ``cronjob action=update model=...`` after a failed run takes effect
        # on the next tick — there is no in-memory cache.
        model = job.get("model") or os.getenv("HERMES_MODEL") or ""

        # Load config.yaml for model, reasoning, prefill, toolsets, provider routing
        _cfg = {}
        _model_cfg = {}
        try:
            import yaml
            _cfg_path = str(_get_hermes_home() / "config.yaml")
            if os.path.exists(_cfg_path):
                with open(_cfg_path, encoding="utf-8") as _f:
                    _cfg = yaml.safe_load(_f) or {}
                # Managed scope: a scheduled job must honor administrator-pinned
                # model / reasoning / toolsets / provider_routing too. This loader
                # builds its own dict, so overlay managed values via the shared
                # helper (fail-open, no-op when no managed scope).
                try:
                    from hermes_cli import managed_scope
                    _cfg = managed_scope.apply_managed_overlay(_cfg)
                except Exception:
                    pass
                _cfg = _expand_env_vars(_cfg)
                # Coerce null/missing to {} so a falsy default never
                # clobbers an already-resolved env value with ``None``.
                _model_cfg = _cfg.get("model") or {}
                if not job.get("model"):
                    if isinstance(_model_cfg, str):
                        model = _model_cfg
                    elif isinstance(_model_cfg, dict):
                        # Mirror the CLI/oneshot resolution: prefer ``default``,
                        # accept a ``model`` alias, overwrite only when truthy.
                        _default = _model_cfg.get("default") or _model_cfg.get("model")
                        if _default:
                            model = _default
        except Exception as e:
            logger.warning("Job '%s': failed to load config.yaml, using defaults: %s", job_id, e)

        # Fail fast if no model resolved from job / env / config.yaml: an empty
        # model otherwise reaches the provider as an opaque 400 (#23979).
        if not (isinstance(model, str) and model.strip()):
            raise RuntimeError(
                f"Cron job '{job_name}' has no model configured "
                f"(job.model={job.get('model')!r}, "
                f"HERMES_MODEL={os.getenv('HERMES_MODEL', '')!r}, "
                "config.yaml model.default missing or empty). "
                f"Set a per-job model via "
                f"`cronjob action=update job_id={job_id} model=<name>` or set a "
                "default with `hermes model <name>`."
            )

        # Apply IPv4 preference if configured.
        try:
            from hermes_constants import apply_ipv4_preference
            _net_cfg = _cfg.get("network", {})
            if isinstance(_net_cfg, dict) and _net_cfg.get("force_ipv4"):
                apply_ipv4_preference(force=True)
        except Exception:
            pass

        # Reasoning config is resolved after provider authentication so an auth
        # fallback can first replace the primary model with its configured model.
        # (Fork: the per-job ``reasoning_effort`` override is applied at that
        # chokepoint below — see the resolve_reasoning_config call site.)
        from hermes_constants import parse_reasoning_effort, resolve_reasoning_config

        # Prefill messages from env or config.yaml. The top-level
        # prefill_messages_file key is canonical; agent.prefill_messages_file is
        # retained as a legacy fallback for older CLI/godmode configs.
        prefill_messages = None
        agent_cfg = _cfg.get("agent", {}) if isinstance(_cfg.get("agent", {}), dict) else {}
        prefill_file = (
            os.getenv("HERMES_PREFILL_MESSAGES_FILE", "")
            or _cfg.get("prefill_messages_file", "")
            or agent_cfg.get("prefill_messages_file", "")
        )
        if prefill_file:
            pfpath = Path(prefill_file).expanduser()
            if not pfpath.is_absolute():
                pfpath = _get_hermes_home() / pfpath
            if pfpath.exists():
                try:
                    with open(pfpath, "r", encoding="utf-8") as _pf:
                        prefill_messages = json.load(_pf)
                    if not isinstance(prefill_messages, list):
                        prefill_messages = None
                except Exception as e:
                    logger.warning("Job '%s': failed to parse prefill messages file '%s': %s", job_id, pfpath, e)
                    prefill_messages = None

        # Max iterations
        max_iterations = _cfg.get("agent", {}).get("max_turns") or _cfg.get("max_turns") or 90

        # Provider routing
        pr = _cfg.get("provider_routing") or {}

        from hermes_cli.runtime_provider import (
            resolve_runtime_provider,
            format_runtime_provider_error,
        )
        from hermes_cli.auth import AuthError

        # F8 runtime backstop: never resolve a stored provider/base_url pair that
        # would ship a named provider's stored credential to an off-host endpoint
        # (CWE-200/CWE-522). The cron tool validates this on create/update, but a
        # job persisted before that guard — or written directly to the jobs store
        # — reaches this sink unchecked. Fail closed before resolution so no
        # off-host call is ever made with a stored key.
        _guard_job_credential_exfil(job)

        primary_model_for_drift = model
        configured_provider_for_drift = (
            str(_model_cfg.get("provider") or "").strip().lower()
            if isinstance(_model_cfg, dict)
            else ""
        )
        primary_provider_for_drift = (
            str(job.get("provider") or "").strip().lower()
            or configured_provider_for_drift
            or None
        )
        try:
            # Do not inject HERMES_INFERENCE_PROVIDER here. resolve_runtime_provider()
            # already prefers persisted config over stale shell/env overrides when
            # no explicit provider is requested. Passing the env var here short-
            # circuits that precedence and can resurrect old providers (for
            # example DeepSeek) for cron jobs that do not pin provider/model.
            runtime_kwargs = {
                "requested": job.get("provider"),
                # Derive provider-specific api_mode from the model this job
                # will actually run (per-job pin > env > config default), not
                # the stale persisted default — mirrors the fallback path
                # below, which already passes its fb_model.
                "target_model": model,
            }
            if job.get("base_url"):
                runtime_kwargs["explicit_base_url"] = job.get("base_url")
            runtime = resolve_runtime_provider(**runtime_kwargs)
            primary_provider_for_drift = (
                str(runtime.get("provider") or "").strip().lower()
                or primary_provider_for_drift
            )
        except AuthError as auth_exc:
            # Primary provider auth failed — try each configured provider/model
            # pair atomically. Keeping the primary model while changing only the
            # provider can silently route a paid GPT model through OpenRouter.
            primary_provider_for_drift = (
                str(getattr(auth_exc, "provider", "") or "").strip().lower()
                or primary_provider_for_drift
            )
            logger.warning("Job '%s': primary auth failed (%s), trying fallback", job_id, auth_exc)
            fb_list = get_fallback_chain(_cfg)
            runtime = None
            for entry in fb_list:
                if not isinstance(entry, dict):
                    continue
                fb_provider = str(entry.get("provider") or "").strip()
                fb_model = str(entry.get("model") or "").strip()
                if not fb_provider or not fb_model:
                    continue
                try:
                    from hermes_cli.fallback_config import resolve_entry_api_key

                    fb_kwargs = {
                        "requested": fb_provider,
                        "target_model": fb_model,
                    }
                    if entry.get("base_url"):
                        fb_kwargs["explicit_base_url"] = entry["base_url"]
                    fb_api_key = resolve_entry_api_key(entry)
                    if fb_api_key:
                        fb_kwargs["explicit_api_key"] = fb_api_key
                    runtime = resolve_runtime_provider(**fb_kwargs)
                    model = fb_model
                    logger.info(
                        "Job '%s': fallback resolved to %s model %s",
                        job_id,
                        runtime.get("provider"),
                        fb_model,
                    )
                    break
                except Exception as fb_exc:
                    logger.debug("Job '%s': fallback %s failed: %s", job_id, fb_provider, fb_exc)
            if runtime is None:
                raise RuntimeError(format_runtime_provider_error(auth_exc)) from auth_exc
        except Exception as exc:
            message = format_runtime_provider_error(exc)
            raise RuntimeError(message) from exc

        # Reasoning: per-job ``reasoning_effort`` override > config chokepoint.
        # Mirrors the per-job model/timeout override pattern and is re-read
        # from storage each tick, so a ``cronjob action=update
        # reasoning_effort=...`` takes effect on the next run with no restart.
        # A missing/empty job value means "no override" and falls through to
        # resolve_reasoning_config (which sees the post-fallback model); an
        # explicit level (or ``false`` to disable thinking) wins. Raw values
        # pass straight to parse_reasoning_effort (a YAML boolean False means
        # thinking disabled — see that helper).
        _job_effort = job.get("reasoning_effort")
        if _job_effort is None or (
            isinstance(_job_effort, str) and not _job_effort.strip()
        ):
            reasoning_config = resolve_reasoning_config(
                _cfg if isinstance(_cfg, dict) else {}, str(model)
            )
        else:
            reasoning_config = parse_reasoning_effort(_job_effort)

        # Provider/model-drift fail-closed guard (#44585).
        #
        # An UNPINNED job (no explicit job["provider"]/["model"]) follows the
        # global default, which can change after the job was created — a switch
        # to a paid PROVIDER (e.g. nous) OR a paid MODEL on the same provider
        # (e.g. claude-fable-5 on openrouter). Without a guard the job would
        # silently inherit that change and spend real money on every tick — the
        # $7.73 incident named BOTH a provider and a model.
        #
        # create_job() snapshots whatever resolution would have picked at
        # creation for each unpinned axis (job["provider_snapshot"] /
        # job["model_snapshot"]). Here, for each axis that (a) has a snapshot and
        # (b) is unpinned and (c) currently resolves to a DIFFERENT value, we
        # fail closed: skip this run, make NO paid call, and deliver a loud,
        # actionable alert telling the user to pin the axis explicitly.
        #
        # Back-compat: an axis with no snapshot (pre-existing jobs, no_agent, or
        # any axis whose creation-time resolution failed) behaves exactly as
        # before — the guard never engages for it. Pinned axes are unaffected.
        _drift: list[str] = []
        _provider_snapshot = (job.get("provider_snapshot") or "").strip().lower()
        if _provider_snapshot and not (job.get("provider") or "").strip():
            _current_provider = str(
                primary_provider_for_drift or runtime.get("provider") or ""
            ).strip().lower()
            if _current_provider and _current_provider != _provider_snapshot:
                _drift.append(
                    f"provider '{_provider_snapshot}' -> '{_current_provider}'"
                )
        _model_snapshot = (job.get("model_snapshot") or "").strip().lower()
        if _model_snapshot and not (job.get("model") or "").strip():
            _current_model = str(primary_model_for_drift or "").strip().lower()
            if _current_model and _current_model != _model_snapshot:
                _drift.append(
                    f"model '{_model_snapshot}' -> '{_current_model}'"
                )
        if _drift:
            _changes = "; ".join(_drift)
            logger.warning(
                "Job '%s': SKIPPED — global inference config drifted since "
                "creation (%s) and this job is unpinned. Skipped to prevent "
                "unintended spend. Pin explicitly to proceed: "
                "`cronjob action=update job_id=%s provider=<p> model=<m>`.",
                job_id,
                _changes,
                job_id,
            )
            raise RuntimeError(
                f"Skipped to prevent unintended spend: global inference config "
                f"drifted since this job was created ({_changes}), and this job "
                f"is unpinned. No inference call was made. To run on the new "
                f"config, pin it explicitly: `cronjob action=update "
                f"job_id={job_id} provider=<provider> model=<model>` "
                f"(or pin the original values to keep them). See #44585."
            )

        fallback_model = get_fallback_chain(_cfg) or None
        credential_pool = None
        runtime_provider = str(runtime.get("provider") or "").strip().lower()
        if runtime_provider:
            try:
                from agent.credential_pool import load_pool
                pool = load_pool(runtime_provider)
                if pool.has_credentials():
                    credential_pool = pool
                    logger.info(
                        "Job '%s': loaded credential pool for provider %s with %d entries",
                        job_id,
                        runtime_provider,
                        len(pool.entries()),
                    )
            except Exception as e:
                logger.debug("Job '%s': failed to load credential pool for %s: %s", job_id, runtime_provider, e)

        # Initialize MCP servers so configured mcp_servers are available to
        # the agent's tool registry before AIAgent is constructed. Without
        # this, cron jobs never saw any MCP tools — only the gateway / CLI
        # paths called discover_mcp_tools() at startup. Idempotent: subsequent
        # ticks short-circuit on already-connected servers inside
        # register_mcp_servers(). Non-fatal on failure: a broken MCP server
        # shouldn't kill an otherwise-working cron job. See #4219.
        try:
            from tools.mcp_tool import discover_mcp_tools
            _mcp_tools = discover_mcp_tools()
            if _mcp_tools:
                logger.info(
                    "Job '%s': %d MCP tool(s) available",
                    job_id, len(_mcp_tools),
                )
        except Exception as _mcp_exc:
            logger.warning(
                "Job '%s': MCP initialization failed (non-fatal): %s",
                job_id, _mcp_exc,
            )

        agent = AIAgent(
            model=model,
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            api_mode=runtime.get("api_mode"),
            acp_command=runtime.get("command"),
            acp_args=runtime.get("args"),
            max_iterations=max_iterations,
            reasoning_config=reasoning_config,
            prefill_messages=prefill_messages,
            fallback_model=fallback_model,
            credential_pool=credential_pool,
            providers_allowed=pr.get("only"),
            providers_ignored=pr.get("ignore"),
            providers_order=pr.get("order"),
            provider_sort=pr.get("sort"),
            openrouter_min_coding_score=(_cfg.get("openrouter") or {}).get("min_coding_score"),
            enabled_toolsets=_resolve_cron_enabled_toolsets(job, _cfg),
            disabled_toolsets=_resolve_cron_disabled_toolsets(_cfg),
            quiet_mode=True,
            # Cron jobs should always inherit the user's SOUL.md identity from
            # HERMES_HOME. When a workdir is configured, also inject project
            # context files (AGENTS.md / CLAUDE.md / .cursorrules) from there.
            # Without a workdir, keep cwd context discovery disabled.
            skip_context_files=not bool(_job_workdir),
            load_soul_identity=True,
            skip_memory=True,  # Cron system prompts would corrupt user representations
            platform="cron",
            session_id=_cron_session_id,
            session_db=_session_db,
            activity_recorder=_activity_recorder,
        )
        
        # Run the agent with an *inactivity*-based timeout: the job can run
        # for hours if it's actively calling tools / receiving stream tokens,
        # but a hung API call or stuck tool with no activity for the configured
        # duration is caught and killed.  Default 600s (10 min inactivity);
        # override via HERMES_CRON_TIMEOUT env var.  0 = unlimited.
        #
        # Uses the agent's built-in activity tracker (updated by
        # _touch_activity() on every tool call, API call, and stream delta).
        _raw_cron_timeout = os.getenv("HERMES_CRON_TIMEOUT", "").strip()
        if _raw_cron_timeout:
            try:
                _cron_timeout = float(_raw_cron_timeout)
            except (ValueError, TypeError):
                logger.warning(
                    "Invalid HERMES_CRON_TIMEOUT=%r; using default 600s",
                    _raw_cron_timeout,
                )
                _cron_timeout = 600.0
        else:
            _cron_timeout = 600.0
        _cron_inactivity_limit = _cron_timeout if _cron_timeout > 0 else None
        # Wall-clock (hard) timeout: bounds the TOTAL duration of the cron
        # job regardless of activity. Default 0 = unlimited (existing
        # behavior preserved). Recommended in production where slow LLM
        # chains can block the scheduler queue (2026-04-26 incident:
        # jobflow-scout actively progressed for 22.8 min, blocking every
        # other due cron from firing because tick() runs jobs sequentially).
        # Same kill mechanism as inactivity: agent.interrupt() + raise.
        _cron_hard_timeout = float(os.getenv("HERMES_CRON_HARD_TIMEOUT", 0))
        _cron_hard_limit = _cron_hard_timeout if _cron_hard_timeout > 0 else None
        import time as _time
        _cron_start_time = _time.monotonic()
        _POLL_INTERVAL = 5.0
        # Keep the one-shot run_claim fresh while the run is alive (#62002):
        # the claim TTL is a dead-owner detector, but without a heartbeat a
        # run that legitimately outlives it (stream stall, laptop asleep
        # mid-run) is indistinguishable from a dead tick — another process
        # re-dispatches it and get_due_jobs stale-removes the job record out
        # from under the live run. Refreshing the claim from this monitor
        # keeps "expired claim" meaning "owner died".
        _job_schedule = job.get("schedule")
        _is_oneshot = (
            isinstance(_job_schedule, dict) and _job_schedule.get("kind") == "once"
        )
        _run_claim = job.get("run_claim")
        _run_claim_owner = (
            str(_run_claim.get("by") or "") if isinstance(_run_claim, dict) else ""
        )
        _last_claim_heartbeat = time.monotonic()

        def _heartbeat_run_claim_if_due():
            nonlocal _last_claim_heartbeat
            if not _is_oneshot or not _run_claim_owner:
                return
            _mono = time.monotonic()
            if _mono - _last_claim_heartbeat < _RUN_CLAIM_HEARTBEAT_SECONDS:
                return
            _last_claim_heartbeat = _mono
            try:
                heartbeat_run_claim(job_id, expected_owner=_run_claim_owner)
            except Exception:
                logger.debug(
                    "Job '%s': run_claim heartbeat failed", job_name, exc_info=True
                )

        _cron_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # Preserve scheduler-scoped ContextVar state (for example skill-declared
        # env passthrough registrations) when the cron run hops into the worker
        # thread used for inactivity timeout monitoring.
        _cron_context = contextvars.copy_context()
        _cron_future = _cron_pool.submit(_cron_context.run, agent.run_conversation, prompt)
        _inactivity_timeout = False
        _wallclock_timeout = False
        try:
            if (
                _cron_inactivity_limit is None
                and _cron_hard_limit is None
                and not _is_oneshot
            ):
                # No inactivity watchdog, no wall-clock limit, and no
                # one-shot run_claim to keep fresh — just wait.
                result = _cron_future.result()
            else:
                # Poll so the loop can service, per iteration: the one-shot
                # run_claim heartbeat (upstream #62002), the inactivity
                # watchdog, and the fork's HERMES_CRON_HARD_TIMEOUT
                # wall-clock limit.
                result = None
                while True:
                    done, _ = concurrent.futures.wait(
                        {_cron_future}, timeout=_POLL_INTERVAL,
                    )
                    if done:
                        result = _cron_future.result()
                        break
                    _heartbeat_run_claim_if_due()
                    # Agent still running — check inactivity.
                    if _cron_inactivity_limit is not None:
                        _idle_secs = 0.0
                        if hasattr(agent, "get_activity_summary"):
                            try:
                                _act = agent.get_activity_summary()
                                _idle_secs = _act.get("seconds_since_activity", 0.0)
                            except Exception:
                                pass
                        if _idle_secs >= _cron_inactivity_limit:
                            _inactivity_timeout = True
                            break
                    # Wall-clock (hard) limit check — fork.
                    if _cron_hard_limit is not None:
                        _elapsed = _time.monotonic() - _cron_start_time
                        if _elapsed >= _cron_hard_limit:
                            _wallclock_timeout = True
                            break
        except Exception:
            _cron_pool.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            _cron_pool.shutdown(wait=False, cancel_futures=True)

        if _wallclock_timeout:
            # Build diagnostic summary from the agent's activity tracker.
            _wc_elapsed = _time.monotonic() - _cron_start_time
            _activity = {}
            if hasattr(agent, "get_activity_summary"):
                try:
                    _activity = agent.get_activity_summary()
                except Exception:
                    pass
            _last_desc = _activity.get("last_activity_desc", "unknown")
            _cur_tool = _activity.get("current_tool")
            _iter_n = _activity.get("api_call_count", 0)
            _iter_max = _activity.get("max_iterations", 0)

            logger.error(
                # %g, not %.0f: HERMES_CRON_HARD_TIMEOUT uses 0 for UNLIMITED,
                # so a sub-second limit rounded to "0s" reads as "no limit was
                # set" at the moment the limit fired.
                "Job '%s' exceeded wall-clock limit %gs (elapsed %gs) "
                "| last_activity=%s | iteration=%s/%s | tool=%s",
                job_name, _cron_hard_limit, _wc_elapsed,
                _last_desc, _iter_n, _iter_max,
                _cur_tool or "none",
            )
            if hasattr(agent, "interrupt"):
                agent.interrupt("Cron job timed out (wall-clock)")
            raise TimeoutError(
                f"Cron job '{job_name}' exceeded wall-clock limit "
                f"{_cron_hard_limit:g}s (elapsed {_wc_elapsed:g}s) "
                f"— last activity: {_last_desc}"
            )

        if _inactivity_timeout:
            # Build diagnostic summary from the agent's activity tracker.
            _activity = {}
            if hasattr(agent, "get_activity_summary"):
                try:
                    _activity = agent.get_activity_summary()
                except Exception:
                    pass
            _last_desc = _activity.get("last_activity_desc", "unknown")
            _secs_ago = _activity.get("seconds_since_activity", 0)
            _cur_tool = _activity.get("current_tool")
            _iter_n = _activity.get("api_call_count", 0)
            _iter_max = _activity.get("max_iterations", 0)

            logger.error(
                # %g, not %.0f — same sentinel hazard as the wall-clock branch
                # above (HERMES_CRON_TIMEOUT documents 0 = unlimited).
                "Job '%s' idle for %gs (inactivity limit %gs) "
                "| last_activity=%s | iteration=%s/%s | tool=%s",
                job_name, _secs_ago, _cron_inactivity_limit,
                _last_desc, _iter_n, _iter_max,
                _cur_tool or "none",
            )
            if hasattr(agent, "interrupt"):
                agent.interrupt("Cron job timed out (inactivity)")
            raise TimeoutError(
                f"Cron job '{job_name}' idle for "
                f"{_secs_ago:g}s (limit {_cron_inactivity_limit:g}s) "
                f"— last activity: {_last_desc}"
            )

        # Guard against non-dict returns from run_conversation under error conditions
        if not isinstance(result, dict):
            raise RuntimeError(
                f"agent.run_conversation returned {type(result).__name__} instead of dict: {result!r}"
            )

        # If the agent itself reported failure (e.g. all retries exhausted on
        # API errors, model abort, mid-run interrupt), do not silently mark the
        # job as successful. run_agent populates `failed=True`/`completed=False`
        # on these paths and may put the error into `final_response`, which
        # would otherwise be delivered as if it were the agent's reply and the
        # job's `last_status` set to "ok". Raise so the except handler below
        # builds the proper failure tuple. (issue #17855)
        turn_exit_reason = str(result.get("turn_exit_reason") or "")
        final_response_text = (result.get("final_response") or "").strip()
        max_iteration_summary = (
            result.get("failed") is not True
            and result.get("completed") is False
            and turn_exit_reason.startswith("max_iterations_reached(")
            and bool(final_response_text)
        )
        if result.get("failed") is True or (result.get("completed") is False and not max_iteration_summary):
            _err_text = (
                result.get("error")
                or final_response_text
                or "agent reported failure"
            )
            raise RuntimeError(_err_text)
        if max_iteration_summary:
            logger.warning(
                "Job '%s' reached the iteration limit but produced a final fallback response; "
                "delivering the response instead of failing the cron run",
                job_name,
            )

        final_response = result.get("final_response", "") or ""
        # Strip leaked placeholder text that upstream may inject on empty completions.
        if final_response.strip() == "(No response generated)":
            final_response = ""
        # Cron silence on abnormal empty turns.  The turn-completion explainer
        # (#34452) replaces a blank/empty model turn with a "⚠️ No reply: …"
        # string so interactive surfaces (CLI/gateway) explain why the box is
        # empty.  In a cron context that turns a previously-silent empty turn
        # into a delivered warning (Manfredi's Telegram symptom).  Detect the
        # explainer text deterministically (via the same formatter that
        # produced it) and treat it as empty so the empty-response suppression
        # and soft-failure marking below apply — restoring pre-#34452 silence
        # for scheduled jobs without disabling the explainer everywhere.
        if final_response.strip() and turn_exit_reason:
            try:
                _explainer_text = AIAgent._format_turn_completion_explanation(turn_exit_reason)
            except Exception:
                _explainer_text = ""
            if _explainer_text and final_response.strip() == _explainer_text.strip():
                logger.info(
                    "Job '%s': abnormal empty turn (%s) — suppressing explainer for cron delivery",
                    job_id,
                    turn_exit_reason,
                )
                final_response = ""
        # Use a separate variable for log display; keep final_response clean
        # for delivery logic (empty response = no delivery).
        logged_response = final_response if final_response else "(No response generated)"
        
        output = f"""# Cron Job: {job_name}

**Job ID:** {job_id}
**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}
**Schedule:** {job.get('schedule_display', 'N/A')}

## Prompt

{prompt}

## Response

{logged_response}
"""
        
        logger.info("Job '%s' completed successfully", job_name)
        # The model loop finished, which is process evidence only. Artifact,
        # domain, and delivery evidence are not available here, so the derived
        # final outcome is `unknown` — never inferred success.
        _finish_cron_activity(
            _activity_recorder,
            process="succeeded",
            evidence_refs=(f"session:{_cron_session_id}",),
        )
        return True, output, final_response, None
        
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        logger.exception("Job '%s' failed: %s", job_name, error_msg)
        
        output = f"""# Cron Job: {job_name} (FAILED)

**Job ID:** {job_id}
**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}
**Schedule:** {job.get('schedule_display', 'N/A')}

## Prompt

{prompt}

## Error

```
{error_msg}
```
"""
        _finish_cron_activity(
            _activity_recorder,
            process="failed",
            evidence_refs=(f"session:{_cron_session_id}",),
        )
        return False, output, "", error_msg

    finally:
        # Restore TERMINAL_CWD to whatever it was before this job ran.  We
        # only ever mutate it when the job has a workdir; see the setup block
        # at the top of run_job for the serialization guarantee.
        if _job_workdir:
            if _prior_terminal_cwd == "_UNSET_":
                os.environ.pop("TERMINAL_CWD", None)
            else:
                os.environ["TERMINAL_CWD"] = _prior_terminal_cwd
        # Release the cwd lock now that the env is restored, so a waiting
        # workdir job (or queued reader) can proceed without seeing the override.
        if _holds_cwd_write:
            _terminal_cwd_lock.release_write()
        else:
            _terminal_cwd_lock.release_read()
        # Clean up ContextVar session/delivery state for this job.
        clear_session_vars(_ctx_tokens)
        for _var_name in _cron_delivery_vars:
            _VAR_MAP[_var_name].set("")
        if _session_db:
            # Title the cron session from the job (name -> id) and PERSIST it
            # BEFORE end_session()/close() tear the connection down, so the
            # close can never run over an in-flight title write (#50536). The
            # run-time suffix keeps it unique against the sessions.title index
            # across runs; _set_cron_session_title dedupes (#50537) and the
            # except-fallback below guarantees a non-blank title (#50535).
            try:
                _title_base = " ".join(job_name.split())[:60].strip() or f"cron {job_id}"
                _cron_title = f"{_title_base} · {_hermes_now().strftime('%b %d %H:%M')}"
                if not _set_cron_session_title(_session_db, _cron_session_id, _cron_title):
                    # Helper returned None (blank base) -> use the id fallback.
                    _set_cron_session_title(
                        _session_db, _cron_session_id, f"cron {job_id}"
                    )
            except (Exception, KeyboardInterrupt) as e:
                logger.debug(
                    "Job '%s': failed to set cron session title: %s", job_id, e
                )
                # Last-resort: never leave the session blank (#50535). Try the
                # next free title in the lineage, then a bare id-stamped title.
                for _fallback in (
                    getattr(_session_db, "get_next_title_in_lineage", lambda b: b)(
                        f"cron {job_id}"
                    ),
                    f"cron {job_id} {_cron_session_id[-6:]}",
                ):
                    try:
                        if _set_cron_session_title(
                            _session_db, _cron_session_id, _fallback
                        ):
                            break
                    except (Exception, KeyboardInterrupt):
                        continue
            try:
                _session_db.end_session(_cron_session_id, "cron_complete")
            except (Exception, KeyboardInterrupt) as e:
                logger.debug("Job '%s': failed to end session: %s", job_id, e)
            try:
                _session_db.close()
            except (Exception, KeyboardInterrupt) as e:
                logger.debug("Job '%s': failed to close SQLite session store: %s", job_id, e)
        # Release subprocesses, terminal sandboxes, browser daemons, and the
        # main OpenAI/httpx client held by this ephemeral cron agent. Without
        # this, a gateway that ticks cron every N minutes leaks fds per job
        # until it hits EMFILE (#10200 / "too many open files").
        #
        # When the caller opted to defer teardown (passed a list), hand the live
        # agent back instead of closing it here — delivery must run against a
        # live async client, and the caller tears down afterwards (#58720).
        if defer_agent_teardown is not None:
            if agent is not None:
                defer_agent_teardown.append(agent)
        else:
            _teardown_cron_agent(agent, job_id)


def _teardown_cron_agent(agent, job_id: str) -> None:
    """Release an ephemeral cron agent's async resources.

    Split out of ``run_job``'s ``finally`` so a caller that defers teardown
    (to deliver first — #58720) can invoke the identical cleanup AFTER delivery.
    Closes the agent (subprocesses, sandboxes, browser daemons, OpenAI/httpx
    client) and reaps stale async clients whose loop has since closed. Idempotent
    and independently guarded, matching the original inline behavior.
    """
    try:
        if agent is not None:
            agent.close()
    except (Exception, KeyboardInterrupt) as e:
        logger.debug("Job '%s': failed to close agent resources: %s", job_id, e)
    # Each cron run spins up a short-lived worker thread whose event loop
    # dies as soon as the ``ThreadPoolExecutor`` shuts down. Any async
    # httpx clients cached under that loop are now unusable — reap them
    # so their transports don't accumulate in the process-global cache.
    try:
        from agent.auxiliary_client import cleanup_stale_async_clients
        cleanup_stale_async_clients()
    except Exception as e:
        logger.debug("Job '%s': failed to reap stale auxiliary clients: %s", job_id, e)


def run_one_job(job: dict, *, adapters=None, loop=None, verbose: bool = False) -> bool:
    """Run ONE due job end-to-end: execute → save output → deliver → mark.

    This is the shared firing body extracted from ``tick``'s per-job closure so
    that BOTH the built-in ticker and an external provider's ``fire_due`` (e.g.
    Chronos) run the identical sequence — no duplicated correctness.

    It does NOT decide whether the job is due, claim it, or compute the next
    run — those are the caller's concern (``tick`` advances ``next_run_at``
    under the file lock before dispatch; an external provider claims via the
    store CAS). This function only fires the given job once.

    Returns True if the job was processed (even if the job itself failed —
    failure is recorded via ``mark_job_run``), False only if processing raised.
    """
    execution_id = job.get("execution_id")
    if not execution_id:
        execution_id = create_execution(job["id"], source="direct")["id"]

    _job_id = job["id"]
    _cron_job_name = job.get("name", _job_id)
    emitter = _get_event_emitter()
    firecrawl_state = None
    firecrawl_token = None
    success = False
    final_response = ""

    # Same-job concurrency guard (Guard #3, 2026-04-30) — fork seam.
    # The built-in ticker enforces this inside ``tick``'s ``_process_job``;
    # registering here too makes the external-provider ``fire_due`` path and
    # direct callers share the SAME registry, so a provider fire racing a
    # tick fire of the same job_id is rejected with the same
    # CRON_SKIPPED_DUPLICATE observability instead of running twice.
    _prior_in_flight = _try_register_in_flight(_job_id, _cron_job_name)
    if _prior_in_flight is not None:
        _emit_cron_skipped_duplicate(
            emitter, _job_id, _cron_job_name, _prior_in_flight
        )
        try:
            finish_execution(
                execution_id,
                success=False,
                error="Duplicate fire blocked; prior run still in flight.",
            )
        except Exception:
            logger.debug(
                "finish_execution failed for duplicate-blocked %s", _job_id
            )
        return False

    try:
        # Pre-run dispatch claim (issue #38758): atomically commit a finite
        # one-shot's dispatch BEFORE its side effect runs, so a tick that dies
        # mid-execution (gateway kill, OOM, segfault, hard-timeout) cannot
        # re-fire the job forever on restart. No-op for recurring jobs (they
        # use advance_next_run) and infinite/no-repeat jobs. This lives here in
        # the shared body so BOTH the built-in ticker and the external provider
        # (Chronos fire_due) get at-most-times semantics.
        if not claim_dispatch(job["id"]):
            logger.info(
                "Job '%s': one-shot dispatch limit reached — skipping",
                job.get("name", job["id"]),
            )
            finish_execution(
                execution_id,
                success=False,
                error="Dispatch claim rejected; execution was not started.",
            )
            return True  # not an error — already handled/removed

        # Emit cron_started (fork seam — same emitter contract as the tick
        # path's _process_job). Capture the event_id so a later
        # duplicate-skip can reference it for cross-event correlation.
        if emitter:
            try:
                _started_event_id = emitter.on_job_started(
                    job_id=_job_id,
                    job_name=_cron_job_name,
                    schedule=job.get("schedule_display", ""),
                )
                _attach_started_event_id(_job_id, _started_event_id)
            except Exception as ee:
                logger.warning("Event emit failed for cron_started: %s", ee)

        # The attempt is claimed durably before executor/provider dispatch and
        # becomes running only immediately before the actual run.
        mark_execution_running(execution_id)

        _run_started_monotonic = time.monotonic()

        # Run the job under the profile's secret scope. get_secret() fails
        # closed outside a scope once profile isolation is in play (multiple
        # gateway profiles / room→profile multiplexing), and cron fires from
        # the ticker thread where no per-turn scope is installed — so
        # resolve_runtime_provider() raised UnscopedSecretError before model
        # selection, breaking every cron job. Mirrors the per-turn pattern in
        # gateway/run.py (_profile_runtime_scope).
        from agent.secret_scope import (
            build_profile_secret_scope,
            reset_secret_scope,
            set_secret_scope,
        )

        _scope_token = set_secret_scope(
            build_profile_secret_scope(_get_hermes_home())
        )
        # Defer the cron agent's async-resource teardown until AFTER delivery.
        # run_job normally closes the agent (and reaps stale async clients) in
        # its finally block; doing that before _deliver_result runs means the
        # live send races a torn-down async client (#58720). Passing a holder
        # list makes run_job hand the agent back instead, and we tear it down
        # below once delivery is done. Defense-in-depth alongside the
        # interpreter-shutdown guard in _deliver_result.
        _deferred_agents: list = []
        try:
            firecrawl_state, firecrawl_token = _install_scout_firecrawl_run(job)
            success, output, final_response, error = run_job(
                job, defer_agent_teardown=_deferred_agents
            )
        except BaseException:
            # run_job's finally still hands back the agent when it raises; tear
            # it down here so a failed run never leaks its async resources
            # (#10200), then re-raise into the outer handler. BaseException
            # (not just Exception) so a KeyboardInterrupt/SystemExit mid-run
            # still triggers teardown before propagating.
            for _deferred_agent in _deferred_agents:
                _teardown_cron_agent(_deferred_agent, job["id"])
            raise
        finally:
            reset_secret_scope(_scope_token)

        # Everything from here through delivery runs with the agent still live
        # (deferred teardown). Wrap it ALL in a try/finally so that if any step
        # between run_job returning and delivery — save_job_output, the [SILENT]
        # / empty-response computation, or _deliver_result itself — raises, the
        # deferred agent is still torn down. Otherwise the outer `except` would
        # swallow the error and leak the agent's subprocesses/clients (#10200).
        delivery_error = None
        try:
            output_file = save_job_output(job["id"], output)
            if verbose:
                logger.info("Output saved to: %s", output_file)

            # If the gateway shutdown killed this job's tool subprocess
            # mid-flight (#60432), the agent may still have produced a
            # plausible-looking final_response from the truncated output --
            # force the failure path so the delivered message is an honest
            # "this run was interrupted" summary instead of that response.
            # Peek-only: the flag stays set for the authoritative check
            # right before mark_job_run below.
            if success and _is_interrupted(job["id"]):
                success = False
                error = (
                    "Interrupted by gateway shutdown before the run finished "
                    "(tool subprocess was killed mid-flight)."
                )

            # Deliver the final response to the origin/target chat.
            # If the agent responded with [SILENT], skip delivery (but
            # output is already saved above).  Failed jobs always deliver.
            deliver_content = final_response if success else _summarize_cron_failure_for_delivery(job, error)
            # Treat whitespace-only final responses the same as empty
            # responses: do not deliver a blank message, and let the
            # empty-response guard below mark the run as a soft failure.
            should_deliver = bool(deliver_content.strip())
            # Cron silence suppression — see _is_cron_silence_response.  Replaces the
            # old `SILENT_MARKER in ...upper()` substring check, which both leaked
            # bracketless near-markers ("SILENT" / "NO_REPLY") and wrongly swallowed
            # a real report that merely quoted "[SILENT]" mid-sentence (#51438,
            # #46917).  Keeps the intentional bracketed-prefix / trailing-line
            # tolerance the cron contract relies on.
            if should_deliver and success and _is_cron_silence_response(deliver_content):
                logger.info("Job '%s': agent returned %s — skipping delivery", job["id"], SILENT_MARKER)
                should_deliver = False

            if should_deliver:
                try:
                    delivery_error = _deliver_result(job, deliver_content, adapters=adapters, loop=loop)
                except Exception as de:
                    delivery_error = str(de)
                    logger.error("Delivery failed for job %s: %s", job["id"], de)
        finally:
            # Tear down the deferred agent(s) now that save + delivery have run
            # (or raised). Must happen on every path so cron agents never leak
            # their subprocesses/clients (#10200).
            for _deferred_agent in _deferred_agents:
                _teardown_cron_agent(_deferred_agent, job["id"])

        # Treat empty final_response as a soft failure so last_status
        # is not "ok" — the agent ran but produced nothing useful.
        # (issue #8585)
        if success and not final_response.strip():
            success = False
            error = "Agent completed but produced empty response (model error, timeout, or misconfiguration)"

        if not _consume_interrupted_flag(job["id"]):
            mark_job_run(job["id"], success, error, delivery_error=delivery_error)
        finish_execution(execution_id, success=success, error=error)

        # Emit completion/failure event (fork seam — mirrors the tick path's
        # _process_job so provider/direct fires are equally observable).
        if emitter:
            try:
                from cron.jobs import load_jobs
                current_job = next(
                    (j for j in load_jobs() if j["id"] == job["id"]), None
                )
                consecutive = current_job.get("consecutive_errors", 0) if current_job else 0
                summary = _summarize_for_event_bus(final_response) if success else None
                emitter.on_job_completed(
                    job_id=job["id"],
                    job_name=_cron_job_name,
                    success=success,
                    duration=round(time.monotonic() - _run_started_monotonic, 1),
                    output_summary=summary,
                    error=error,
                    consecutive_errors=consecutive,
                )
            except Exception as ee:
                logger.warning(
                    "Event emit failed for cron_completed/cron_failed: %s", ee
                )

        # Tailor / generic agent-iteration structured events (fork seam —
        # gated inside the helpers; see _emit_tailor_iteration_event and
        # _emit_agent_iteration_event docstrings).
        if success and emitter:
            _emit_tailor_iteration_event(emitter, job, final_response or "")

        return True

    except Exception as e:
        logger.error("Error processing job %s: %s", job['id'], e)
        if not _consume_interrupted_flag(job["id"]):
            mark_job_run(job["id"], False, str(e))
        finish_execution(execution_id, success=False, error=str(e))
        return False

    finally:
        try:
            try:
                _finalize_agent_iteration_event(
                    emitter,
                    job,
                    final_response or "",
                    success=success,
                    firecrawl_state=firecrawl_state,
                )
            finally:
                _reset_scout_firecrawl_run(firecrawl_token)
        finally:
            # Release the fork's same-job concurrency slot on every exit path
            # (claim-refused, success, agent failure, raised exception).
            _release_in_flight(_job_id)


def _notify_provider_jobs_changed() -> None:
    """Best-effort: tell the active scheduler provider the job set changed.

    Called by the consumer surfaces (model tool / CLI / REST) AFTER a
    successful store mutation (create/update/remove/pause/resume) so an external
    provider (Chronos) can re-provision/cancel the affected one-shot via NAS.
    No-op for the built-in (it re-reads jobs.json each tick), so the default
    path is unchanged. Lives here (not in cron/jobs.py) to keep the store free
    of provider imports — avoids an import cycle and keeps jobs.py low-coupling.
    Never raises into the caller.
    """
    try:
        from cron.scheduler_provider import resolve_cron_scheduler
        resolve_cron_scheduler().on_jobs_changed()
    except Exception as e:
        logger.debug("on_jobs_changed notify failed: %s", e)


def tick(
    verbose: bool = True,
    adapters=None,
    loop=None,
    sync: bool = True,
    *,
    can_dispatch=None,
):
    """
    Check and run all due jobs.
    
    Uses a file lock so only one tick runs at a time, even if the gateway's
    in-process ticker and a standalone daemon or manual tick overlap.
    
    Args:
        verbose: Whether to print status messages
        adapters: Optional dict mapping Platform → live adapter (from gateway)
        loop: Optional asyncio event loop (from gateway) for live adapter sends
        can_dispatch: Optional synchronous gate; false leaves due jobs untouched
            for the next allowed tick

    Returns:
        Number of jobs executed (0 if another tick is already running)
    """
    lock_dir, lock_file = _get_lock_paths()
    lock_dir.mkdir(parents=True, exist_ok=True)

    # Cross-platform file locking: fcntl on Unix, msvcrt on Windows
    lock_fd = None
    try:
        lock_fd = open(lock_file, "w", encoding="utf-8")
        if fcntl:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
    except (OSError, IOError):
        logger.debug("Tick skipped — another instance holds the lock")
        if lock_fd is not None:
            lock_fd.close()
        return 0

    try:
        if can_dispatch is not None and not can_dispatch():
            logger.debug("Cron dispatch paused while gateway drains existing work")
            return 0

        due_jobs, skipped_jobs = get_due_and_skipped_jobs()

        # Emit CRON_SKIPPED for any job whose missed fire was fast-forwarded.
        # Done before due-job processing so visibility is immediate even if
        # this tick has 0 due jobs. Lazy-load the emitter (matches existing
        # pattern). Failures emitting events must NOT block tick progress.
        if skipped_jobs:
            try:
                emitter = _get_event_emitter()
                if emitter is not None:
                    for s in skipped_jobs:
                        emitter.on_job_skipped(
                            job_id=s["job_id"],
                            job_name=s["name"],
                            missed_at=s["missed_at"],
                            missed_seconds=s["missed_seconds"],
                            schedule_kind=s["schedule_kind"],
                            reason=s["reason"],
                        )
            except Exception:
                logger.exception("Failed to emit cron_skipped events")

        # Event-driven wakes. Drained before the no-work early return so a tick
        # with zero SCHEDULED jobs still runs work an event asked for.
        woken_jobs = _collect_woken_jobs(
            exclude_ids={j["id"] for j in due_jobs}
        )

        if verbose and not due_jobs and not woken_jobs:
            logger.info("%s - No jobs due", _hermes_now().strftime('%H:%M:%S'))
            return 0

        if verbose:
            logger.info("%s - %s job(s) due", _hermes_now().strftime('%H:%M:%S'), len(due_jobs))

        # Advance next_run_at for all recurring jobs FIRST, under the file lock,
        # before any execution begins.  This preserves at-most-once semantics.
        # For parallel jobs that are already running, advance_next_run keeps
        # bumping next_run_at forward so the grace window never expires.
        # mark_job_run() overwrites next_run_at on completion.
        for job in due_jobs:
            advance_next_run(job["id"])

        # Woken jobs execute alongside the due ones but are appended AFTER the
        # advance loop on purpose: an event says "there is work now", not "the
        # schedule moved", so their next_run_at is left exactly where it was.
        if woken_jobs:
            due_jobs = due_jobs + woken_jobs

        # Resolve max parallel workers: env var > config.yaml > unbounded.
        # Set HERMES_CRON_MAX_PARALLEL=1 to restore old serial behaviour.
        _max_workers: Optional[int] = None
        try:
            _env_par = os.getenv("HERMES_CRON_MAX_PARALLEL", "").strip()
            if _env_par:
                _max_workers = int(_env_par) or None
        except (ValueError, TypeError):
            logger.warning("Invalid HERMES_CRON_MAX_PARALLEL value; defaulting to unbounded")
        if _max_workers is None:
            try:
                _ucfg = load_config() or {}
                _cfg_par = (
                    _ucfg.get("cron", {}) if isinstance(_ucfg, dict) else {}
                ).get("max_parallel_jobs")
                if _cfg_par is not None:
                    _max_workers = int(_cfg_par) or None
            except Exception:
                pass

        if verbose:
            logger.info(
                "Running %d job(s) in parallel (max_workers=%s)",
                len(due_jobs),
                _max_workers if _max_workers else "unbounded",
            )

        def _process_job(job: dict, _abandoned=None, _deadline_box=None) -> bool:
            """Run one due job end-to-end: execute, save, deliver, mark.

            ``_abandoned`` is set by the soft-deadline wrapper when this
            run was already declared failed at its deadline — late side
            effects (delivery, marking, events, slot release) must then
            be suppressed; only the output file is still written.
            """
            _job_id = job["id"]
            _cron_job_name = job.get("name", _job_id)
            # Execution-ledger id stamped by _submit_with_guard's
            # create_execution (upstream 0.19.0). Every terminal path below
            # must finish it so the recovery classifier never sees a
            # dangling "claimed"/"running" row for a completed attempt.
            _execution_id = job.get("execution_id")
            emitter = _get_event_emitter()
            firecrawl_state = None
            firecrawl_token = None
            success = False
            final_response = ""

            # Same-job concurrency guard (Guard #3, 2026-04-30) -----------
            # Reject a second fire while a prior fire for this job_id is
            # still in-flight. Both reasons reject the new fire; the
            # difference is operator-visible only.  Canonical case:
            # event_id 4edcb4b1-aa07-4dbb-b799-8af167d4f92e.
            prior = _try_register_in_flight(_job_id, _cron_job_name)
            if prior is not None:
                _emit_cron_skipped_duplicate(
                    emitter, _job_id, _cron_job_name, prior
                )
                if _execution_id:
                    try:
                        finish_execution(
                            _execution_id,
                            success=False,
                            error="Duplicate fire blocked; prior run still in flight.",
                        )
                    except Exception:
                        logger.debug(
                            "finish_execution failed for duplicate-blocked %s",
                            _job_id,
                        )
                return False

            # We hold the in-flight slot for _job_id from here; release it
            # on every exit path (the existing inner try/except below was
            # extended with a `finally: _release_in_flight(_job_id)`).

            # Min-seconds-between-fires guard (Guard #4, 2026-04-30 follow-up)
            # ------------------------------------------------------------
            # Catches the SEQUENTIAL-burst pattern Guard #3 misses (where
            # fires are spaced minutes apart, not concurrent).  Per-job
            # opt-in via ``min_seconds_between_fires``; global opt-in via
            # HERMES_CRON_MIN_SECONDS_BETWEEN_FIRES.  Default 0 = off.
            # See sentinel-vip-burst-rc-2026-04-30.md §6.
            #
            # Note: must release the in-flight slot we just acquired on
            # every reject path so the legitimate next fire is not blocked
            # by the rejected one's still-held slot.
            _min_interval = _job_min_seconds_between_fires(job)
            _last_run_at_str = job.get("last_run_at")
            if _min_interval > 0 and _last_run_at_str:
                from datetime import datetime as _datetime
                from cron.jobs import _ensure_aware as _cron_ensure_aware
                try:
                    _last_run_dt = _cron_ensure_aware(
                        _datetime.fromisoformat(_last_run_at_str)
                    )
                    _elapsed_since_last = (
                        _hermes_now() - _last_run_dt
                    ).total_seconds()
                except (TypeError, ValueError) as ee:
                    # Malformed last_run_at -> never block on it; let the
                    # job proceed.  Defensive: production data should never
                    # land here, but we don't want a parse bug to wedge the
                    # scheduler.
                    logger.debug(
                        "Min-interval guard: failed to parse last_run_at=%r "
                        "for job_id=%s (%s); proceeding without check",
                        _last_run_at_str, _job_id, ee,
                    )
                    _elapsed_since_last = float("inf")

                if _elapsed_since_last < _min_interval:
                    logger.warning(
                        "Cron min-interval fire blocked: job_id=%s job_name=%s "
                        "elapsed=%.1fs min=%ds last_run=%s",
                        _job_id, _cron_job_name,
                        _elapsed_since_last, _min_interval, _last_run_at_str,
                    )
                    if emitter:
                        try:
                            emitter.on_job_skipped_min_interval(
                                job_id=_job_id,
                                job_name=_cron_job_name,
                                last_run_at=_last_run_at_str,
                                elapsed_since_last_seconds=round(
                                    _elapsed_since_last, 1
                                ),
                                min_seconds_between_fires=_min_interval,
                            )
                        except Exception as ee:
                            logger.warning(
                                "Event emit failed for "
                                "cron_skipped_min_interval: %s", ee
                            )
                    _release_in_flight(_job_id)
                    if _execution_id:
                        try:
                            finish_execution(
                                _execution_id,
                                success=False,
                                error="Min-interval guard rejected the fire.",
                            )
                        except Exception:
                            logger.debug(
                                "finish_execution failed for min-interval "
                                "reject %s", _job_id,
                            )
                    return False

            # Emit cron_started event.  Capture the event_id so the next
            # duplicate-skip event can reference it for cross-event correlation.
            if emitter:
                try:
                    _started_event_id = emitter.on_job_started(
                        job_id=_job_id,
                        job_name=_cron_job_name,
                        schedule=job.get("schedule_display", ""),
                    )
                    _attach_started_event_id(_job_id, _started_event_id)
                except Exception as ee:
                    logger.warning("Event emit failed for cron_started: %s", ee)

            import time as _time
            # OTel: one span per cron fire. Covers run_job + delivery + event emission.
            _cron_cm = _start_cron_span(f"cron.run_job:{_cron_job_name}")
            try:
                _cron_span = _cron_cm.__enter__()  # real Span (or _NoopCronSpan)
            except Exception:
                # start_as_current_span returns a LAZY (generator-based)
                # context manager — an SDK/API version skew (e.g. sdk 1.44
                # against api 1.39: TraceFlags.RANDOM_TRACE_ID missing)
                # only surfaces here at __enter__, outside _start_cron_span's
                # own guard. OTel must never break a cron fire: degrade to
                # the no-op span, same contract as every other guard here.
                _cron_cm = _NoopCronSpan()
                _cron_span = _cron_cm.__enter__()
            try:
                _cron_span.set_attribute("cron.job_id", job["id"])
                _cron_span.set_attribute("cron.job_name", _cron_job_name)
                _cron_span.set_attribute("cron.schedule", str(job.get("schedule_display", "")))
                # SR-535 remainder: Langfuse session/user/tags grouping.
                _stamp_cron_langfuse(_cron_span, job, _cron_job_name)
            except Exception:
                pass
            _job_start = _time.monotonic()
            try:
                # The attempt was claimed durably in _submit_with_guard;
                # it becomes running only immediately before the actual run
                # (upstream execution-ledger contract).
                if _execution_id:
                    try:
                        mark_execution_running(_execution_id)
                    except Exception:
                        logger.debug(
                            "mark_execution_running failed for %s", _job_id
                        )
                firecrawl_state, firecrawl_token = _install_scout_firecrawl_run(job)
                if _deadline_box is not None and firecrawl_state is not None:
                    _deadline_box["firecrawl_state"] = firecrawl_state
                success, output, final_response, error = run_job(job)
                _job_duration = _time.monotonic() - _job_start

                # Persist the run transcript before choosing the normal or late
                # terminal path. Output persistence can itself cross the soft
                # deadline; checking only before this call let the watchdog
                # abandon/release the slot while this worker later delivered and
                # overwrote a successor through the normal completion writer.
                output_file = save_job_output(job["id"], output)
                if verbose:
                    logger.info("Output saved to: %s", output_file)

                if _deadline_has_elapsed(_abandoned, _deadline_box):
                    # The deadline owner already emitted its alert, released
                    # the slot, and suppressed delivery. Preserve those race
                    # boundaries, but replace its PROVISIONAL failure in the
                    # durable records with the worker's real terminal verdict.
                    if success and _is_interrupted(job["id"]):
                        success = False
                        error = (
                            "Interrupted by gateway shutdown before the run finished "
                            "(tool subprocess was killed mid-flight)."
                        )
                    if success and not final_response.strip():
                        success = False
                        error = (
                            "Agent completed but produced empty response "
                            "(model error, timeout, or misconfiguration)"
                        )
                    amended = _amend_late_deadline_outcome(
                        job,
                        success=success,
                        error=error,
                        deadline_box=_deadline_box,
                    )
                    logger.warning(
                        "Job '%s': finished %.1fs after deadline abandon -- "
                        "delivery suppressed; durable outcome amended=%s (success=%s)",
                        job["id"], _job_duration, amended, success,
                    )
                    try:
                        _cron_span.set_attribute("cron.deadline_abandoned", True)
                        _cron_cm.__exit__(None, None, None)
                    except Exception:
                        pass
                    return False

                # If the gateway shutdown killed this job's tool subprocess
                # mid-flight (#60432), the agent may still have produced a
                # plausible-looking final_response from the truncated output —
                # force the failure path so the delivered message is an honest
                # "this run was interrupted" summary instead of that response.
                # Peek-only: the flag stays set for the authoritative check
                # right before mark_job_run below.
                if success and _is_interrupted(job["id"]):
                    success = False
                    error = (
                        "Interrupted by gateway shutdown before the run finished "
                        "(tool subprocess was killed mid-flight)."
                    )

                # Deliver the final response to the origin/target chat.
                # If the agent responded with [SILENT], skip delivery (but
                # output is already saved above).  Failed jobs always deliver.
                deliver_content = final_response if success else f"⚠️ Cron job '{job.get('name', job['id'])}' failed:\n{error}"
                # Treat whitespace-only final responses the same as empty
                # responses: do not deliver a blank message, and let the
                # empty-response guard below mark the run as a soft failure.
                should_deliver = bool(deliver_content.strip())
                # Cron silence suppression — use the shared _is_cron_silence_response
                # matcher (same as run_one_job) rather than the old
                # `SILENT_MARKER in ...upper()` substring check, which both leaked
                # bracketless near-markers and wrongly swallowed a real report that
                # merely quoted "[SILENT]" mid-sentence (#51438, #46917).
                if should_deliver and success and _is_cron_silence_response(deliver_content):
                    logger.info("Job '%s': agent returned %s — skipping delivery", job["id"], SILENT_MARKER)
                    should_deliver = False

                delivery_error = None
                if should_deliver:
                    try:
                        delivery_error = _deliver_result(job, deliver_content, adapters=adapters, loop=loop)
                    except Exception as de:
                        delivery_error = str(de)
                        logger.error("Delivery failed for job %s: %s", job["id"], de)

                # Treat empty final_response as a soft failure so last_status
                # is not "ok" — the agent ran but produced nothing useful.
                # (issue #8585)
                if success and not final_response.strip():
                    success = False
                    error = "Agent completed but produced empty response (model error, timeout, or misconfiguration)"

                # The shutdown path may have already written an authoritative
                # interrupted last_status for this job (#60432) — never
                # clobber it with a late completion write.
                if not _consume_interrupted_flag(job["id"]):
                    mark_job_run(job["id"], success, error, delivery_error=delivery_error)
                if _execution_id:
                    try:
                        finish_execution(
                            _execution_id, success=success, error=error
                        )
                    except Exception:
                        logger.debug(
                            "finish_execution failed for %s", _job_id
                        )

                # Emit completion/failure event
                if emitter:
                    try:
                        from cron.jobs import load_jobs
                        current_job = next(
                            (j for j in load_jobs() if j["id"] == job["id"]), None
                        )
                        consecutive = current_job.get("consecutive_errors", 0) if current_job else 0
                        summary = _summarize_for_event_bus(final_response) if success else None
                        emitter.on_job_completed(
                            job_id=job["id"],
                            job_name=job.get("name", job["id"]),
                            success=success,
                            duration=round(_job_duration, 1),
                            output_summary=summary,
                            error=error,
                            consecutive_errors=consecutive,
                        )
                    except Exception as ee:
                        logger.warning(
                            "Event emit failed for cron_completed/cron_failed: %s", ee
                        )

                # Tailor structured iteration event (2026-04-29).
                # Gated by job name inside the helper; runs ALONGSIDE the
                # existing [SILENT] / on_job_completed pathway, not in place
                # of it. See _emit_tailor_iteration_event docstring + the
                # design doc spec/2026-04-29-tailor-structured-iteration-
                # event-design.md for failure-mode handling.
                if success and emitter:
                    _emit_tailor_iteration_event(emitter, job, final_response or "")

                # OTel: finalize cron span with execution results + exit ctx.
                try:
                    _cron_span.set_attribute("cron.success", bool(success))
                    _cron_span.set_attribute("cron.duration_s", round(_job_duration, 3))
                    _cron_span.set_attribute("cron.output_chars", len(output or ""))
                    if not success:
                        _cron_span.set_attribute("cron.error", (error or "")[:500])
                        try:
                            from opentelemetry.trace import Status, StatusCode
                            _cron_span.set_status(Status(StatusCode.ERROR, (error or "unknown")[:200]))
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    _cron_cm.__exit__(None, None, None)
                except Exception:
                    pass

                return True

            except Exception as e:
                # OTel: record unexpected exception on the cron span if one is open.
                try:
                    if '_cron_span' in locals() and '_cron_cm' in locals():
                        _cron_span.record_exception(e)
                        try:
                            from opentelemetry.trace import Status, StatusCode
                            _cron_span.set_status(Status(StatusCode.ERROR, str(e)[:200]))
                        except Exception:
                            pass
                        _cron_cm.__exit__(type(e), e, e.__traceback__)
                except Exception:
                    pass
                logger.error("Error processing job %s: %s", job['id'], e)
                # A deadline-abandoned worker can fail by raising instead of
                # returning a structured failure. Preserve that real cause too.
                if _deadline_has_elapsed(_abandoned, _deadline_box):
                    _amend_late_deadline_outcome(
                        job,
                        success=False,
                        error=str(e),
                        deadline_box=_deadline_box,
                    )
                else:
                    if not _consume_interrupted_flag(job["id"]):
                        mark_job_run(job["id"], False, str(e))
                    if _execution_id:
                        try:
                            finish_execution(
                                _execution_id, success=False, error=str(e)
                            )
                        except Exception:
                            logger.debug(
                                "finish_execution failed for %s", _job_id
                            )
                return False

            finally:
                try:
                    try:
                        if not _deadline_has_elapsed(_abandoned, _deadline_box):
                            _finalize_agent_iteration_event(
                                emitter,
                                job,
                                final_response or "",
                                success=success,
                                firecrawl_state=firecrawl_state,
                            )
                        elif firecrawl_state and firecrawl_state.first_failure:
                            # The deadline owner finalizes any credits already
                            # recorded at abandonment. If the 402 arrives later,
                            # the worker performs the same failure-only attempt;
                            # the run-state claim lock makes the two paths one-shot.
                            _finalize_agent_iteration_event(
                                emitter,
                                job,
                                "",
                                success=False,
                                firecrawl_state=firecrawl_state,
                            )
                    finally:
                        _reset_scout_firecrawl_run(firecrawl_token)
                finally:
                    # Release the same-job concurrency slot regardless of how
                    # the run exited (success, agent failure, raised exception).
                    # See `_in_flight` registry block at module top — Guard #3.
                    # EXCEPT when the deadline handler abandoned this run: it
                    # already released the slot, and a successor fire may have
                    # registered a fresh record that must not be popped here.
                    if not _deadline_has_elapsed(_abandoned, _deadline_box):
                        _release_in_flight(_job_id)

        # Partition due jobs: jobs with a per-job workdir and/or profile touch
        # process-global runtime state inside run_job. Workdir jobs temporarily
        # set os.environ["TERMINAL_CWD"]; profile jobs use a context-local
        # Hermes home override, scheduler _hermes_home hook, and temporary
        # profile .env load into os.environ with snapshot/restore. They MUST run
        # sequentially to avoid corrupting each other. Jobs without either field
        # stay parallel-safe.
        # (Fork's Guard #3 in-flight release above is preserved; upstream's
        # workdir+profile partition supersedes the fork's workdir-only pass.)
        # Same predicate the readers-writer lock uses — see
        # _job_mutates_process_globals. These were duplicated literals and
        # drifted (partition: workdir OR profile; lock: workdir alone), which is
        # exactly how profile jobs came to race the parallel pool.
        sequential_jobs = [
            j for j in due_jobs
            if _job_mutates_process_globals(j)
        ]
        parallel_jobs = [
            j for j in due_jobs
            if not _job_mutates_process_globals(j)
        ]

        _results: list = []
        _all_futures: list = []

        # Merge resolution (0.16.0 catch-up): adopt upstream's persistent-pool
        # NON-BLOCKING dispatch (sequential jobs no longer starve the ticker),
        # but preserve the fork's per-job SOFT DEADLINE by wrapping _process_job
        # in _run_callable_with_deadline inside the submitted callable.
        #   - parallel jobs: abandon_on_timeout=True — a runaway worker is
        #     marked failed and its slot released so the next fire isn't blocked.
        #   - sequential (workdir/profile) jobs: abandon_on_timeout=False —
        #     alert-only, because abandoning one mid os.environ snapshot/restore
        #     would corrupt the next job's environment.
        # Two dedup layers coexist by design and cover different race windows:
        # upstream's _running_job_ids guard blocks re-submitting a job still
        # running from a PRIOR tick; the fork's Guard #3 (_in_flight, inside
        # _process_job) blocks the trigger_job()-vs-tick race that bypasses the
        # tick scheduler entirely.
        def _submit_with_guard(
            job: dict,
            pool: concurrent.futures.ThreadPoolExecutor,
            abandon_on_timeout: bool,
        ):
            """Submit a job fire-and-forget with the in-flight dedup guard.

            Returns the future, or None if the job was skipped because a prior
            tick's run of the same job is still in flight.  The running-set
            membership is released in the worker's finally block.  The submitted
            callable runs under the fork's per-job soft-deadline watchdog.
            """
            job_id = job["id"]
            # A tick can race gateway teardown: once the interpreter is
            # finalizing, ``pool.submit`` raises "cannot schedule new futures
            # after interpreter shutdown" and crashes the tick. Skip cleanly —
            # the job stays due and will fire on the next healthy tick
            # (#58720, #55924).
            if _interpreter_shutting_down():
                logger.warning(
                    "Job '%s' not dispatched — interpreter is shutting down",
                    job.get("name", job_id),
                )
                return None
            with _running_lock:
                _running_dup = job_id in _running_job_ids
                if not _running_dup:
                    _running_job_ids.add(job_id)
            if _running_dup:
                logger.info("Job '%s' already running — skipping", job.get("name", job_id))
                # Restore Guard #3 observability at the point the duplicate is
                # ACTUALLY blocked. Upstream's running-set guard (0.16.0
                # non-blocking dispatch) lands before _process_job, so the
                # trigger_job()-vs-tick cross-tick duplicate is rejected here and
                # never reaches Guard #3 — surface the skip event here when a
                # live in-flight record exists to correlate against (it does once
                # the prior fire has crossed _process_job; the brief
                # pre-registration window falls back to a silent skip, matching
                # the prior behaviour). Done after releasing _running_lock so the
                # event-bus call never blocks the hot dispatch lock.
                with _in_flight_lock:
                    _dup_prior = _in_flight.get(job_id)
                if _dup_prior is not None:
                    _emit_cron_skipped_duplicate(
                        _get_event_emitter(),
                        job_id,
                        job.get("name", job_id),
                        _dup_prior,
                    )
                return None
            # Record the attempt before executor dispatch. Recovery classifies
            # abandoned records as unknown; it never automatically retries them.
            execution = create_execution(job_id, source="builtin")
            dispatched_job = dict(job, execution_id=execution["id"])
            _ctx = contextvars.copy_context()

            def _run_and_release(j=dispatched_job, ctx=_ctx, abandon=abandon_on_timeout):
                try:
                    return _run_callable_with_deadline(j, _process_job, abandon, ctx)
                finally:
                    with _running_lock:
                        _running_job_ids.discard(j["id"])

            try:
                return pool.submit(_run_and_release)
            except Exception as submit_err:
                with _running_lock:
                    _running_job_ids.discard(job_id)
                finish_execution(
                    execution["id"],
                    success=False,
                    error=f"Executor dispatch failed: {submit_err}",
                )
                # Interpreter began finalizing between the guard above and the
                # submit — release the in-flight claim we just took and skip.
                if isinstance(submit_err, RuntimeError) and _interpreter_shutting_down(submit_err):
                    logger.warning(
                        "Job '%s' not dispatched — interpreter is shutting down",
                        job.get("name", job_id),
                    )
                    return None
                logger.error(
                    "Job '%s' not dispatched: %s",
                    job.get("name", job_id),
                    submit_err,
                )
                return None

        # Sequential pass for env-mutating (workdir) jobs.
        # Queued to a persistent single-thread pool so they run one at a time
        # WITHOUT blocking the ticker thread — a long workdir job no
        # longer starves the rest of the schedule (same fix as the parallel
        # pass, just serialized).  The in-flight guard prevents a still-running
        # job from being re-queued on the next tick.
        if sequential_jobs:
            seq_pool = _get_sequential_pool()
            for job in sequential_jobs:
                fut = _submit_with_guard(job, seq_pool, False)
                if fut is None:
                    continue
                _all_futures.append(fut)
                if not sync:
                    _results.append(True)  # optimistically counted

        # Parallel pass — persistent pool, non-blocking dispatch.
        # Jobs that are already running (from a previous tick) are skipped.
        # mark_job_run() updates next_run_at on completion, so the next tick
        # after completion finds the job due again naturally.  No catch-up
        # queue needed.
        if parallel_jobs:
            pool = _get_parallel_pool(_max_workers)
            for job in parallel_jobs:
                fut = _submit_with_guard(job, pool, True)
                if fut is None:
                    continue
                _all_futures.append(fut)
                if not sync:
                    _results.append(True)  # optimistically counted

        # Best-effort sweep of MCP stdio subprocesses that survived their
        # session teardown.  Must run AFTER jobs finish so active sessions
        # (including live user chats) are never touched — only PIDs explicitly
        # detected as orphans in tools.mcp_tool._run_stdio's finally block are
        # reaped.
        def _sweep_mcp_orphans() -> None:
            try:
                from tools.mcp_tool import _kill_orphaned_mcp_children
                _kill_orphaned_mcp_children()
            except Exception as _e:
                logger.debug("Post-tick MCP orphan cleanup failed: %s", _e)

        if sync:
            # Sync mode (tests / manual ticks): wait for all dispatched jobs,
            # collect results, then sweep once.
            for f in concurrent.futures.as_completed(_all_futures):
                try:
                    _results.append(f.result())
                except Exception as exc:
                    logger.error("Cron job future failed: %s", exc)
                    _results.append(False)
            _sweep_mcp_orphans()
            return sum(_results)

        # Async (gateway ticker) mode: don't block.  Sweep orphans via a
        # done-callback fired after the LAST dispatched job completes, so the
        # sweep still happens after jobs finish without stalling the tick.
        if _all_futures:
            _remaining = [len(_all_futures)]

            def _on_done(_f: concurrent.futures.Future) -> None:
                _remaining[0] -= 1
                try:
                    _exc = _f.exception()
                    if _exc is not None:
                        logger.error("Cron job future failed in async mode: %s", _exc, exc_info=(type(_exc), _exc, _exc.__traceback__))
                except Exception:
                    pass
                if _remaining[0] <= 0:
                    _sweep_mcp_orphans()

            for _f in _all_futures:
                _f.add_done_callback(_on_done)
        else:
            # Nothing dispatched (all skipped / no due jobs) — sweep inline.
            _sweep_mcp_orphans()

        return sum(_results)
    finally:
        if fcntl:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
        elif msvcrt:
            try:
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
        lock_fd.close()


if __name__ == "__main__":
    tick(verbose=True)
