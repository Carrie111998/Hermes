"""Command-line control plane for the cross-harness session bridge."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, replace
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
from typing import Any, NamedTuple, Protocol, cast

from agent.transports.codex_app_server import CodexAppServerClient
from hermes_constants import (
    reset_hermes_home_override,
    set_hermes_home_override,
)
from hermes_state import SessionDB

from .catalog import UnifiedCatalog
from .characterize import (
    CharacterizationGateError,
    LiveCharacterizationError,
    _read_characterization_record,
    resolve_characterization_gate,
    resolve_cli_executable,
    claim_claude_visibility_characterization_abort,
    characterization_source_root,
    characterize_claude_visibility,
    cleanup_characterized_claude_visibility,
    load_codex_characterization_origins,
    retire_aborted_claude_visibility_characterization,
    run_live_characterization,
)
from .claude_adapter import ClaudeSourceAdapter, ClaudeTargetAdapter
from .claude_registrar import (
    ClaudeNativeRegistrar,
    _canonical_claude_startup_settings,
)
from .claude_visibility import (
    ClaudeVisibilityCandidate,
    build_claude_visibility_candidate,
    derive_claude_visibility_identity,
    normalized_claude_visibility_repair_rows,
)
from .claude_visibility_codes import (
    CLAUDE_VISIBILITY_FATAL_CODES,
    CLAUDE_VISIBILITY_RETRY_CODES,
    CLAUDE_VISIBILITY_STATUS_FATAL_CODES,
)
from .codex_adapter import (
    CodexSourceAdapter,
    CodexTargetAdapter,
    SidebarThreadVerifier,
    _VisibilityInventoryCancelled,
)
from .codex_client import RecoveringCodexAppServerClient
from .config import BridgeConfig, is_canonical_sidebar_string
from .context_pack import ContextPackBuilder
from .coordinator import (
    ClaudeVisibilityCoordinator,
    ClaudeVisibilityRunResult,
    SessionBridgeCoordinator,
    _VisibilityCycleCancelled,
)
from .listener_watchdog import (
    DEAF_LISTENER_REASON,
    ListenerWatchdog,
    make_deaf_listener_handler,
)
from .mcp_server import create_app, resolve_bearer_token, resolve_marker_key
from .mirror_float import (
    ClaudeMirrorFloatWorker,
    IdleChipArchiveWorker,
    discover_ccd_registry_roots,
)
from .mirror import (
    BatchProgress,
    DiscoveryMode,
    EligibilityContext,
    MirrorPolicy,
    classify_mirror_eligibility,
    enqueue_mirror_job,
    should_halt_batch,
)
from .models import (
    BridgeMarkerPayload,
    HydrationMarkerPayload,
    MirrorJobState,
    Provider,
    SidebarHydrationState,
    SidebarJobState,
    canonical_session_id,
    encode_bridge_marker,
)
from .claude_skill import install_claude_skill
from .sidebar import (
    SidebarCandidate,
    SidebarInitialPromptKind,
    classify_sidebar_initial_prompt,
    encode_hydration_marker,
    sidebar_bridge_id,
    sidebar_create_recovery_key,
    sidebar_idempotency_key,
    validate_sidebar_create_reservation,
)
from .preview import build_session_preview
from .sidebar_skill import install_sidebar_skill
from .sidebar_executor import (
    CodexAppServerSidebarDelivery,
    NativeThreadUnrecoverable,
    SidebarExecutor,
)
from .sidebar_hydration_executor import SidebarHydrationExecutor
from .sidebar_runtime import (
    configured_mcp_server_names,
    sidebar_registration_app_server_args,
)
from .store import (
    HYDRATION_FATAL_ERRORS,
    HYDRATION_RETRYABLE_ERRORS,
    SIDEBAR_FATAL_ERRORS,
    SIDEBAR_BOUND_RETRY_CONFIRMATION,
    SIDEBAR_PRECREATE_RESOLUTION_CODE,
    SIDEBAR_RETRYABLE_ERRORS,
    SIDEBAR_SOURCE_CWD_REPAIR_CONFIRMATION,
    SIDEBAR_TERMINAL_RESOLUTION_CODE,
    SIDEBAR_UNBOUND_RESOLUTION_CODE,
    SessionBridgeStore,
    SidebarSource,
    redact_codex_thread_id,
    sidebar_precreate_terminal_evidence_digest,
    sidebar_bound_retry_authority_matches,
    sidebar_terminal_evidence_digest,
    sidebar_unbound_terminal_evidence_digest,
)


EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_DEGRADED = 3
EXIT_ROLLOUT_GATE = 4
_MAX_BACKFILL_CREATE = 10
_BACKFILL_PAGE_SIZE = 1_000
_MAX_PLANNED_SESSIONS = 10_000
_CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# A scoped refresh re-proves one CLI without creating a real session for the
# provider that did not drift.  "all" stays the default: install still requires
# both providers to resolve.
_CHARACTERIZATION_PROVIDER_SELECTIONS: dict[str, tuple[str, ...]] = {
    "all": ("claude", "codex"),
    "claude": ("claude",),
    "codex": ("codex",),
}
# Bumped 2026-08-26 from 2.1.216. The pin was pinned to the npm global, which
# had silently drifted to 2.1.216 while the Desktop app ran 2.1.237; that npm
# copy was UNINSTALLED 2026-08-25 (see resolve_claude_command), so the resolver
# correctly began returning the Desktop-shipped CLI and every preflight refused
# version_unpinned from then on -- 46 of 50 continuous cycles in one 49-minute
# window, with the whole visibility lane dying at cli.py raise ProviderDegraded
# BEFORE discovery. A stale pin does not announce itself: the status blob kept
# serving a 21-hour-old degraded/bridge_conflict, which reads as a registrar
# fault. 2.1.246 is what claude_registrar's screen model was actually measured
# against -- the two live TUI frames it was built on were captured 2026-08-25
# and 2026-08-26 through this repo's own isolation argv on this CLI.
_CLAUDE_VISIBILITY_PINNED_VERSION = "2.1.246"
_CLAUDE_VISIBILITY_VERSION_OUTPUTS = frozenset({
    _CLAUDE_VISIBILITY_PINNED_VERSION,
    f"{_CLAUDE_VISIBILITY_PINNED_VERSION} (Claude Code)",
})
_CLAUDE_FORCED_ONBOARDING = frozenset({"banner", "step"})
_CLAUDE_FORCED_ONBOARDING_ENVIRONMENTS = (
    "CLAUDE_CODE_POWERUP_ONBOARDING",
    "CLAUDE_CODE_TEAM_ONBOARDING",
)
_MAX_CLAUDE_AUTH_STATUS_BYTES = 16_384
_MAX_CLAUDE_GLOBAL_CONFIG_BYTES = 4 * 1024 * 1024
_MAX_CLAUDE_USER_SETTINGS_BYTES = 4 * 1024 * 1024
_CLAUDE_VISIBILITY_PREFLIGHT_TTL_SECONDS = 300.0
_CLAUDE_CHARACTERIZATION_SYNC_LIMIT = 100
_SIDEBAR_CREATE_RESERVATION_CUTOVER_STATE_KEY = (
    "session-bridge:sidebar:create-reservation-cutover:v1"
)
_SIDEBAR_EXECUTION_BLOCKER_ORDER = (
    "sidebar_failed",
    "sidebar_terminal_resolution_mismatch",
    "sidebar_terminal_resolution_ledger_invalid",
    "unknown_retry_code",
)
_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "bearer",
    "context_pack",
    "credential",
    "marker_key",
    "native_path",
    "password",
    "payload",
    "secret",
    "source_cursor",
    "source_hash",
    "token",
)
_LOG = logging.getLogger(__name__)
_CLAUDE_LINEAGE_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}")


def _run_continuous_visibility_worker(
    *,
    run_once: Callable[..., object],
    close: Callable[[], object],
    stop: Any,
    interval_seconds: float = 60.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Run one local visibility cycle per interval without AI heartbeats."""

    try:
        while not stop.is_set():
            started = monotonic()
            try:
                run_once(stop=stop)
            except _VisibilityCycleCancelled:
                break
            except Exception:
                _LOG.exception("continuous Claude visibility cycle failed")
            elapsed = max(0.0, monotonic() - started)
            if stop.wait(max(0.0, interval_seconds - elapsed)):
                break
    finally:
        close()


def _run_continuous_sidebar_recovery_worker(
    *,
    run_once: Callable[[], Mapping[str, Any]],
    close: Callable[[], object],
    stop: Any,
    actionable_interval_seconds: float = 0.05,
    idle_interval_seconds: float = 2.0,
    unsettled_interval_seconds: float = 5.0,
) -> None:
    """Drain durable sidebar work without AI heartbeats or provider-scan timing."""

    try:
        while not stop.is_set():
            try:
                result = run_once()
                status = result.get("status") if isinstance(result, Mapping) else None
            except Exception:
                _LOG.exception("continuous sidebar recovery cycle failed")
                status = "unsettled"
            if status == "idle":
                interval = idle_interval_seconds
            elif status == "unsettled":
                interval = unsettled_interval_seconds
            else:
                interval = actionable_interval_seconds
            if stop.wait(interval):
                break
    finally:
        close()


def _kill_process_tree(pid: int) -> bool:
    """Kill a process AND its descendants. Best-effort; never raises."""
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10.0,
                check=False,
            )
            return completed.returncode == 0
        os.killpg(os.getpgid(pid), 9)  # pragma: no cover - posix path
        return True  # pragma: no cover - posix path
    except Exception:
        return False


def _bounded_run(
    args: Sequence[str],
    *,
    capture_output: bool = True,
    text: bool = True,
    timeout: float = 15.0,
    stdin: object = subprocess.DEVNULL,
    shell: bool = False,
    check: bool = False,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """A ``subprocess.run`` whose ``timeout`` actually bounds the call.

    2026-08-13: ``subprocess.run(timeout=...)`` does NOT bound this workload. On
    timeout it kills the DIRECT child and then re-enters ``communicate()`` with no
    timeout to reap it. ``claude`` is a Node CLI that spawns grandchildren which
    inherit the stdout/stderr pipe handles, so those pipes never reach EOF, the
    reader threads never finish, and ``_communicate`` blocks on ``join()`` forever.

    Captured live via py-spy on the wedged service -- the
    ``session-bridge-claude-visibility`` thread parked indefinitely at::

        _wait_for_tstate_lock -> join -> _communicate -> communicate -> run
        -> _claude_visibility_preflight (cli.py:271)

    which silently killed Claude visibility processing for the process lifetime:
    the 15s bound existed but could never fire.

    This implementation never lets an unbounded read block the caller:
      * ``proc.wait(timeout=...)`` bounds the wait WITHOUT touching the pipes, so
        a grandchild holding them cannot extend it;
      * output is drained on daemon threads that are joined with their own small
        bound and simply abandoned if a leaked handle keeps them alive;
      * on timeout the whole process TREE is killed (``taskkill /T``), which is
        what actually releases the inherited handles.

    Raises ``subprocess.TimeoutExpired`` on timeout, matching the contract callers
    already handle.
    """
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": stdin,
        "shell": shell,
        "env": dict(env) if env is not None else None,
    }
    if text:
        popen_kwargs.update(text=True, encoding="utf-8", errors="replace")

    proc = subprocess.Popen(list(args), **popen_kwargs)
    out_chunks: list[str] = []
    err_chunks: list[str] = []

    def _drain(stream: Any, sink: list[str]) -> None:
        if stream is None:
            return
        try:
            for line in iter(stream.readline, "" if text else b""):
                sink.append(line if text else line.decode("utf-8", "replace"))
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, out_chunks), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err_chunks), daemon=True),
    ]
    for reader in readers:
        reader.start()

    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        tree_killed = _kill_process_tree(proc.pid)
        reaped = False
        try:
            proc.wait(timeout=2.0)
            reaped = True
        except (OSError, subprocess.SubprocessError):
            pass
        if not tree_killed or not reaped:
            _LOG.warning(
                "bounded subprocess cleanup incomplete pid=%s tree_killed=%s reaped=%s",
                proc.pid,
                tree_killed,
                reaped,
            )
        raise
    finally:
        # Bounded: a leaked grandchild handle must not strand the caller.
        for reader in readers:
            reader.join(timeout=2.0)

    result: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
        list(args), returncode, "".join(out_chunks), "".join(err_chunks)
    )
    if check and returncode != 0:
        raise subprocess.CalledProcessError(
            returncode, list(args), result.stdout, result.stderr
        )
    return result


class _ClaudeVisibilityPreflight(NamedTuple):
    """Startup state, plus a fixed code naming the gate that refused it.

    ``failure_code`` is None exactly when ``startup`` is not, and is always a
    member of ``CLAUDE_VISIBILITY_PREFLIGHT_FAILURE_CODES``. It is diagnostic
    only: ``main`` deliberately collapses ProviderDegraded to
    ``{"error": "provider_degraded"}``, so these codes reach the service log
    and never public output.
    """

    startup: dict[str, str] | None
    failure_code: str | None


def _claude_visibility_local_preflight_detail(
    *,
    global_config_path: Path | str | None = None,
    user_settings_path: Path | str | None = None,
) -> _ClaudeVisibilityPreflight:
    """Recheck cheap local startup gates without launching Claude."""

    def _refused(code: str) -> _ClaudeVisibilityPreflight:
        return _ClaudeVisibilityPreflight(None, code)

    if "CLAUDE_CONFIG_DIR" in os.environ:
        return _refused("claude_visibility_preflight_failed_config_dir_override")
    if any(
        os.environ.get(name) in _CLAUDE_FORCED_ONBOARDING
        for name in _CLAUDE_FORCED_ONBOARDING_ENVIRONMENTS
    ):
        return _refused("claude_visibility_preflight_failed_forced_onboarding")
    selected_config = (
        Path(global_config_path)
        if global_config_path is not None
        else _resolve_default_claude_global_config_path()
    )
    if not _read_claude_completed_onboarding(selected_config):
        return _refused("claude_visibility_preflight_failed_onboarding_incomplete")
    selected_settings = (
        Path(user_settings_path)
        if user_settings_path is not None
        else Path.home() / ".claude" / "settings.json"
    )
    theme = _read_claude_startup_theme(selected_settings)
    if theme is None:
        return _refused("claude_visibility_preflight_failed_theme_unavailable")
    return _ClaudeVisibilityPreflight(
        {
            "version": _CLAUDE_VISIBILITY_PINNED_VERSION,
            "authentication": "available",
            "theme": theme,
        },
        None,
    )


def _run_claude_preflight_command(
    stage: str,
    argv: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    child_env: Mapping[str, str],
) -> subprocess.CompletedProcess[str] | None:
    started = time.monotonic()
    try:
        completed = runner(
            list(argv),
            capture_output=True,
            text=True,
            timeout=15.0,
            stdin=subprocess.DEVNULL,
            shell=False,
            check=False,
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        _LOG.warning(
            "Claude visibility preflight command failed stage=%s kind=timeout elapsed_ms=%d",
            stage,
            round((time.monotonic() - started) * 1000),
        )
        return None
    except (OSError, subprocess.SubprocessError):
        _LOG.warning(
            "Claude visibility preflight command failed stage=%s kind=subprocess_error elapsed_ms=%d",
            stage,
            round((time.monotonic() - started) * 1000),
        )
        return None
    _LOG.debug(
        "Claude visibility preflight command completed stage=%s returncode=%d elapsed_ms=%d",
        stage,
        completed.returncode,
        round((time.monotonic() - started) * 1000),
    )
    return completed


def _claude_visibility_preflight_detail(
    command: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _bounded_run,
    global_config_path: Path | str | None = None,
    user_settings_path: Path | str | None = None,
) -> _ClaudeVisibilityPreflight:
    """Read pinned version, auth, and startup state without starting a session.

    Each refusal names its own gate. Ten distinct reasons previously shared one
    bare ``None``, so a failure in production said only that *something* in
    startup was wrong -- reconstructing which one needed a hand-written probe.
    """

    def _refused(code: str) -> _ClaudeVisibilityPreflight:
        return _ClaudeVisibilityPreflight(None, code)

    # Transcript discovery is fixed to ~/.claude/projects. An alternate config
    # root would make the startup state and transcript roots disagree.
    local = _claude_visibility_local_preflight_detail(
        global_config_path=global_config_path,
        user_settings_path=user_settings_path,
    )
    if local.startup is None:
        return local

    child_env = os.environ.copy()
    child_env["DISABLE_UPDATES"] = "1"
    version = _run_claude_preflight_command(
        "version",
        [*command, "--version"],
        runner=runner,
        child_env=child_env,
    )
    if version is None:
        return _refused("claude_visibility_preflight_failed_command_error")
    authentication = _run_claude_preflight_command(
        "auth_status",
        [*command, "auth", "status", "--json"],
        runner=runner,
        child_env=child_env,
    )
    if authentication is None:
        return _refused("claude_visibility_preflight_failed_command_error")
    auth_output = authentication.stdout
    if type(auth_output) is not str:
        return _refused("claude_visibility_preflight_failed_auth_output_invalid")
    try:
        auth_output_bytes = len(auth_output.encode("utf-8"))
    except UnicodeEncodeError:
        return _refused("claude_visibility_preflight_failed_auth_output_invalid")
    version_text = version.stdout.strip() if version.returncode == 0 else ""
    # Split from one combined condition purely to name the gate; the order is
    # preserved, so exactly the same inputs are refused as before.
    if version_text not in _CLAUDE_VISIBILITY_VERSION_OUTPUTS:
        return _refused("claude_visibility_preflight_failed_version_unpinned")
    if authentication.returncode != 0:
        return _refused("claude_visibility_preflight_failed_auth_unavailable")
    if auth_output_bytes > _MAX_CLAUDE_AUTH_STATUS_BYTES:
        return _refused("claude_visibility_preflight_failed_auth_output_too_large")
    auth_status = _strict_json_object(auth_output)
    if auth_status is None:
        return _refused("claude_visibility_preflight_failed_auth_output_invalid")
    if auth_status.get("loggedIn") is not True:
        return _refused("claude_visibility_preflight_failed_not_logged_in")
    theme = cast(dict[str, str], local.startup)["theme"]
    return _ClaudeVisibilityPreflight(
        {
            "version": _CLAUDE_VISIBILITY_PINNED_VERSION,
            "authentication": "available",
            "theme": theme,
        },
        None,
    )


def _claude_visibility_preflight(
    command: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _bounded_run,
    global_config_path: Path | str | None = None,
    user_settings_path: Path | str | None = None,
) -> dict[str, str] | None:
    """Startup state only, or None when any gate refused.

    The original ``dict | None`` contract, kept because the refusal *reason*
    is irrelevant to a caller that only needs to know whether startup is
    usable. Production raise sites use ``_claude_visibility_preflight_detail``
    so the gate reaches the log.
    """

    return _claude_visibility_preflight_detail(
        command,
        runner=runner,
        global_config_path=global_config_path,
        user_settings_path=user_settings_path,
    ).startup


def _resolve_default_claude_global_config_path() -> Path:
    """Resolve Claude 2.1.246 global state for the fixed default config root."""

    home = Path.home()
    modern = home / ".claude" / ".config.json"
    try:
        if modern.exists():
            return modern
    except OSError:
        return modern
    suffix = "-custom-oauth" if os.environ.get("CLAUDE_CODE_CUSTOM_OAUTH_URL") else ""
    return home / f".claude{suffix}.json"


def _read_claude_completed_onboarding(global_config_path: Path) -> bool:
    """Read only Claude's exact global onboarding-complete flag."""

    document = _read_strict_claude_json(
        global_config_path, max_bytes=_MAX_CLAUDE_GLOBAL_CONFIG_BYTES
    )
    return document is not None and document.get("hasCompletedOnboarding") is True


def _read_claude_startup_theme(user_settings_path: Path) -> str | None:
    """Read only the allowlisted theme from Claude's default user settings."""

    document = _read_strict_claude_json(
        user_settings_path, max_bytes=_MAX_CLAUDE_USER_SETTINGS_BYTES
    )
    if document is None:
        return None
    theme = document.get("theme")
    try:
        _canonical_claude_startup_settings(theme)
    except ValueError:
        return None
    return cast(str, theme)


def _read_strict_claude_json(path: Path, *, max_bytes: int) -> dict[str, Any] | None:
    """Read one bounded regular JSON object without exposing unrelated fields."""

    try:
        with path.open("rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
                return None
            payload = handle.read(max_bytes + 1)
    except OSError:
        return None
    if not payload or len(payload) > max_bytes:
        return None
    return _strict_json_object(payload)


def _strict_json_object(payload: str | bytes) -> dict[str, Any] | None:
    """Parse one JSON object while rejecting duplicates and nonstandard numbers."""

    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("nonstandard JSON constant")

    try:
        document = json.loads(
            payload,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return None
    return document if type(document) is dict else None


def _production_codex_permission_preflight(cwd: str) -> bool:
    """Verify the production broker process can traverse the exact Codex cwd.

    Native task sandbox authorization is additionally proven by the rollout
    canary; this check is the fail-closed host-side gate available before
    handing a continuation back to Codex.
    """

    if (
        type(cwd) is not str
        or not cwd
        or any(character in cwd for character in "\x00\r\n")
    ):
        return False
    try:
        path = Path(cwd)
        if not path.is_absolute():
            return False
        resolved = path.resolve(strict=True)
        if not resolved.is_dir() or not os.access(resolved, os.R_OK | os.X_OK):
            return False
        with os.scandir(resolved) as entries:
            next(entries, None)
    except OSError:
        return False
    return True


def _claude_characterization_evidence(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ConfigurationFailure("characterization_record_invalid") from None
    return hashlib.sha256(encoded).hexdigest()


def _record_claude_characterization_payload(
    *,
    store: Any,
    payload: Mapping[str, Any],
    marker_secret: bytes,
    cleanup_completed: bool,
    launch_aborted: bool = False,
    ensure_registered: bool = False,
) -> Mapping[str, Any]:
    if type(ensure_registered) is not bool or (
        ensure_registered and (cleanup_completed or launch_aborted)
    ):
        raise ConfigurationFailure("characterization_record_invalid")
    required_text = (
        "operation_id",
        "source_session_id",
        "bridge_id",
        "job_id",
        "reserved_claude_uuid",
        "native_name",
        "source_cwd",
        "signed_marker",
    )
    if (
        payload.get("schema_version") != 2
        or payload.get("source_provider") != Provider.CODEX.value
        or any(
            not isinstance(payload.get(key), str) or not payload.get(key)
            for key in required_text
        )
    ):
        raise ConfigurationFailure("characterization_record_invalid")
    operation_id = str(payload["operation_id"])
    source_session_id = str(payload["source_session_id"])
    if source_session_id != f"codex:{operation_id}":
        raise ConfigurationFailure("characterization_record_invalid")
    candidate = ClaudeVisibilityCandidate(
        source_session_id=source_session_id,
        source_provider=Provider.CODEX,
        native_name=str(payload["native_name"]),
        source_cwd=str(payload["source_cwd"]),
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=float(payload.get("created_at", 0.0)),
    )
    identity = derive_claude_visibility_identity(candidate, marker_secret)
    if (
        identity.job_id != payload["job_id"]
        or identity.bridge_id != payload["bridge_id"]
        or identity.claude_uuid != payload["reserved_claude_uuid"]
        or identity.signed_marker != payload["signed_marker"]
    ):
        raise ConfigurationFailure("characterization_record_invalid")
    evidence_digest = _claude_characterization_evidence(payload)
    try:
        if ensure_registered:
            result = store.enqueue_claude_visibility_characterization(
                candidate,
                identity,
                marker_secret,
                operation_id=operation_id,
                evidence_digest=evidence_digest,
            )
        else:
            result = store.record_claude_visibility_characterization(
                job_id=identity.job_id,
                operation_id=operation_id,
                source_session_id=source_session_id,
                bridge_id=identity.bridge_id,
                idempotency_key=identity.idempotency_key,
                reserved_claude_uuid=identity.claude_uuid,
                native_name=candidate.native_name,
                source_cwd=candidate.source_cwd,
                signed_marker=identity.signed_marker,
                evidence_digest=evidence_digest,
                marker_secret=marker_secret,
                cleanup_completed=cleanup_completed,
                launch_aborted=launch_aborted,
            )
    except (TypeError, ValueError):
        raise ConfigurationFailure("characterization_record_invalid") from None
    if not isinstance(result, Mapping):
        raise ConfigurationFailure("characterization_record_invalid")
    return result


def _sync_claude_characterization_records(
    *,
    store: Any,
    source_root: Path,
    marker_secret: bytes,
    include_active: bool,
    include_completed: bool,
) -> dict[str, int]:
    """Authenticate and bind only bounded, canonical characterization records."""

    if type(include_active) is not bool or type(include_completed) is not bool:
        raise ConfigurationFailure("characterization_record_invalid")
    root = Path(source_root).expanduser().absolute()
    if not root.exists():
        return {"registered": 0, "cleanup_completed": 0}
    paths: list[tuple[Path, bool]] = []
    if include_active:
        active = root / ".claude-visibility-operation.json"
        if active.exists():
            paths.append((active, False))
    if include_completed:
        completed_root = root / ".cleanup-completed"
        if completed_root.exists():
            completed_paths = sorted(
                path
                for path in completed_root.iterdir()
                if path.is_file() and path.suffix == ".json"
            )
            if len(completed_paths) > _CLAUDE_CHARACTERIZATION_SYNC_LIMIT:
                raise ConfigurationFailure("characterization_record_limit")
            paths.extend((path, True) for path in completed_paths)
    if len(paths) > _CLAUDE_CHARACTERIZATION_SYNC_LIMIT + 1:
        raise ConfigurationFailure("characterization_record_limit")

    result = {"registered": 0, "cleanup_completed": 0}
    for path, completed in paths:
        try:
            payload = _read_characterization_record(path, marker_secret)
        except RuntimeError:
            raise ConfigurationFailure("characterization_record_invalid") from None
        phase = payload.get("phase")
        if completed:
            if phase != "completed" or path.stem != payload.get("operation_id"):
                raise ConfigurationFailure("characterization_record_invalid")
        elif phase == "prepared":
            # The job identity is not persisted until the reservation callback.
            continue
        elif phase not in {"reserved", "launching", "launched", "ready"}:
            raise ConfigurationFailure("characterization_record_invalid")
        _record_claude_characterization_payload(
            store=store,
            payload=payload,
            marker_secret=marker_secret,
            cleanup_completed=completed,
            ensure_registered=not completed,
        )
        result["registered"] += 1
        result["cleanup_completed"] += int(completed)
    return result


class _Backend(Protocol):
    def close(self) -> None: ...
    def serve(self) -> None: ...
    def scan(
        self, *, provider: str, all_history: bool, newest_first: bool
    ) -> Mapping[str, Any]: ...
    def status(self) -> Mapping[str, Any]: ...
    def sidebar_status(self) -> Mapping[str, Any]: ...
    def configure_sidebar_broker(
        self,
        *,
        thread_id: str,
        project_id: str,
        cwd: str,
        inbox_cwd: str,
    ) -> Mapping[str, Any]: ...
    def sidebar_backfill(
        self, *, days: int | None, limit: int, apply: bool
    ) -> Mapping[str, Any]: ...
    def set_sidebar_continuous(self, *, enabled: bool) -> Mapping[str, Any]: ...
    def set_sidebar_readable_preview(self, *, enabled: bool) -> Mapping[str, Any]: ...
    def set_sidebar_hydration(self, *, enabled: bool) -> Mapping[str, Any]: ...
    def sidebar_hydration_seed(
        self,
        *,
        source_session_id: str,
        codex_thread_id: str,
        confirmation: str,
    ) -> Mapping[str, Any]: ...
    def sidebar_hydration_seed_backfill(
        self,
        *,
        days: int | None,
        limit: int,
        apply: bool,
        confirmation: str | None,
    ) -> Mapping[str, Any]: ...
    def sidebar_hydration_status(self) -> Mapping[str, Any]: ...
    def sidebar_run_once(self) -> Mapping[str, Any]: ...
    def sidebar_retry_bound(
        self,
        *,
        job_id: str,
        source_session_id: str,
        codex_thread_id: str,
        expected_error_code: str,
        confirmation: str,
    ) -> Mapping[str, Any]: ...
    def sidebar_acknowledge_unrecoverable(
        self,
        *,
        job_id: str,
        codex_thread_id: str,
        expected_error_code: str,
    ) -> Mapping[str, Any]: ...
    def sidebar_acknowledge_precreate_unrecoverable(
        self,
        *,
        job_id: str,
        expected_error_code: str,
    ) -> Mapping[str, Any]: ...
    def sidebar_acknowledge_unbound_unrecoverable(
        self,
        *,
        job_id: str,
        expected_error_code: str,
    ) -> Mapping[str, Any]: ...
    def claude_visibility_status(self) -> Mapping[str, Any]: ...
    def claude_visibility_backfill(
        self, *, days: int, limit: int, apply: bool
    ) -> Mapping[str, Any]: ...
    def reconcile_claude_visibility_lineage(
        self,
        *,
        limit: int,
        apply: bool,
        cursor: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...
    def set_claude_visibility_continuous(
        self, *, enabled: bool
    ) -> Mapping[str, Any]: ...
    def claude_visibility_run_once(self, *, stop: Any = None) -> Mapping[str, Any]: ...
    def characterize_claude_visibility(
        self, cleanup_token: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]: ...
    def abort_claude_visibility_characterization(
        self, *, expected_job_id: str, expected_reserved_claude_uuid: str
    ) -> Mapping[str, Any]: ...
    def inspect_failed_claude_visibility_job(
        self,
        *,
        job_id: str,
        reserved_claude_uuid: str,
        expected_error_code: str,
    ) -> Mapping[str, Any]: ...
    def repair_failed_claude_visibility_job(
        self,
        *,
        job_id: str,
        reserved_claude_uuid: str,
        expected_error_code: str,
    ) -> Mapping[str, Any]: ...
    def dismiss_claude_visibility_job(
        self, *, job_id: str, expected_error_code: str
    ) -> Mapping[str, Any]: ...
    def characterize(self, *, provider: str) -> Mapping[str, Any]: ...
    def characterization_status(self) -> str: ...
    def backfill_candidates(self, *, days: int) -> list[dict[str, Any]]: ...
    def apply_backfill(
        self, *, candidates: list[dict[str, Any]]
    ) -> Mapping[str, Any]: ...
    def mirror_preview(self, *, session_id: str, target: str) -> Mapping[str, Any]: ...
    def apply_mirror(self, *, session_id: str, target: str) -> Mapping[str, Any]: ...


class ConfigurationFailure(RuntimeError):
    """A fixed-code local configuration or authorization failure."""


class ProviderDegraded(RuntimeError):
    """A provider operation failed after local validation passed."""


class RolloutGateBlocked(RuntimeError):
    """A mutation was refused before its first durable write."""

    def __init__(self, gate: str) -> None:
        super().__init__(gate)
        self.gate = gate


class ProductionBackend:
    """Lazy production composition; tests inject a small fake backend."""

    def __init__(
        self, config: BridgeConfig, *, db_path: Path | None = None
    ) -> None:
        if not isinstance(config, BridgeConfig):
            raise TypeError("config must be a BridgeConfig")
        self.config = config
        self._db_path = Path(db_path) if db_path is not None else None
        self._db: SessionDB | None = None
        self._store: SessionBridgeStore | None = None
        self._catalog: UnifiedCatalog | None = None
        self._coordinator: SessionBridgeCoordinator | None = None
        self._claude_visibility_coordinator: ClaudeVisibilityCoordinator | None = None
        self._claude_visibility_startup_identity: tuple[tuple[str, ...], str] | None = (
            None
        )
        self._claude_visibility_preflight_command: tuple[str, ...] | None = None
        self._claude_visibility_preflight_at: float | None = None
        self._claude_visibility_stop: threading.Event | None = None
        self._codex_client: RecoveringCodexAppServerClient | None = None
        # Cached Codex source adapter for visibility discovery. The adapter
        # holds the cross-call projection cache; rebuilding it per inventory
        # call discarded that cache every continuous cycle (2026-08-23).
        self._claude_visibility_codex_adapter: CodexSourceAdapter | None = None
        self._sidebar_codex_client: CodexAppServerClient | None = None
        self._sidebar_registration_codex_client: CodexAppServerClient | None = None
        self._sidebar_executor: SidebarExecutor | None = None
        self._sidebar_hydration_executor: SidebarHydrationExecutor | None = None

    def close(self) -> None:
        provider_client, self._codex_client = self._codex_client, None
        self._claude_visibility_codex_adapter = None
        sidebar_client, self._sidebar_codex_client = (
            self._sidebar_codex_client,
            None,
        )
        registration_client, self._sidebar_registration_codex_client = (
            self._sidebar_registration_codex_client,
            None,
        )
        db, self._db = self._db, None
        self._sidebar_executor = None
        self._sidebar_hydration_executor = None
        self._store = None
        self._catalog = None
        self._coordinator = None
        self._claude_visibility_coordinator = None
        self._claude_visibility_startup_identity = None
        self._claude_visibility_preflight_command = None
        self._claude_visibility_preflight_at = None

        first_error: BaseException | None = None
        closed_clients: set[int] = set()
        for client in (provider_client, sidebar_client, registration_client):
            if client is not None and id(client) not in closed_clients:
                closed_clients.add(id(client))
                try:
                    client.close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
        if db is not None:
            try:
                db.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def serve(self) -> None:
        visibility_stop: threading.Event | None = None
        visibility_thread: threading.Thread | None = None
        listener_watchdog: ListenerWatchdog | None = None
        try:
            if self.config.mirrors.automatic_creation:
                try:
                    resolve_characterization_gate()
                except CharacterizationGateError as exc:
                    raise RolloutGateBlocked(f"characterization_{exc.code}") from exc
            self._apply_sidebar_create_reservation_cutover()
            coordinator = self._provider_runtime(
                targets=True,
                catalog_only=False,
                providers=(Provider.CLAUDE, Provider.CODEX),
            )
            catalog = self._require_catalog()
            store = self._require_store()
            token = resolve_bearer_token()
            app = create_app(
                catalog=catalog,
                coordinator=coordinator,
                store=store,
                config=self.config,
                token=token,
            )
            if (
                self.config.claude_visibility.enabled
                and self.config.claude_visibility.continuous
            ):
                visibility_stop = threading.Event()
                visibility_backend = ProductionBackend(
                    self.config, db_path=self._db_path
                )
                visibility_backend._claude_visibility_stop = visibility_stop
                visibility_thread = threading.Thread(
                    target=_run_continuous_visibility_worker,
                    kwargs={
                        "run_once": visibility_backend.claude_visibility_run_once,
                        "close": visibility_backend.close,
                        "stop": visibility_stop,
                    },
                    name="session-bridge-claude-visibility",
                    daemon=False,
                )
                visibility_thread.start()
            import uvicorn

            # uvicorn.run() builds exactly this and throws the Server away. We
            # keep it so the listener watchdog has something to ask for a
            # shutdown when it decides the accept chain is gone -- see
            # session_bridge.listener_watchdog for why an exit is the fix.
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=self.config.service.host,
                    port=self.config.service.port,
                    log_level="info",
                )
            )
            listener_watchdog = ListenerWatchdog(
                host=self.config.service.host,
                port=self.config.service.port,
                on_deaf=make_deaf_listener_handler(
                    lambda: setattr(server, "should_exit", True)
                ),
            )
            listener_watchdog.start()
            server.run()
        except RolloutGateBlocked:
            raise
        except ConfigurationFailure:
            raise
        except (OSError, PermissionError, ValueError) as exc:
            raise ConfigurationFailure("service_configuration_failed") from exc
        except RuntimeError as exc:
            if "token" in str(exc).casefold() or "marker" in str(exc).casefold():
                raise ConfigurationFailure("service_authorization_failed") from exc
            raise ProviderDegraded("service_start_failed") from exc
        finally:
            if listener_watchdog is not None:
                listener_watchdog.stop()
            if visibility_stop is not None:
                visibility_stop.set()
            if visibility_thread is not None:
                visibility_thread.join(timeout=5.0)
                if visibility_thread.is_alive():
                    raise RuntimeError(
                        "continuous Claude visibility worker did not stop"
                    )
        # Raised outside the try so it keeps its own reason: the `except
        # RuntimeError` arm above would otherwise relabel it
        # service_start_failed. Either way it lands on EXIT_DEGRADED (3), which
        # is what the supervisor acts on.
        if listener_watchdog is not None and listener_watchdog.fired:
            raise ProviderDegraded(DEAF_LISTENER_REASON)

    def scan(
        self, *, provider: str, all_history: bool, newest_first: bool
    ) -> Mapping[str, Any]:
        if not all_history or not newest_first:
            raise ConfigurationFailure("unsupported_scan_mode")
        try:
            selected = None if provider == "all" else Provider(provider)
            if selected is None:
                summaries: list[dict[str, Any]] = []
                for candidate in (Provider.CLAUDE, Provider.CODEX):
                    try:
                        coordinator = self._provider_runtime(
                            targets=False,
                            catalog_only=True,
                            providers=(candidate,),
                        )
                        summaries.append(
                            asdict(asyncio.run(coordinator.scan_all_history(candidate)))
                        )
                    except ConfigurationFailure:
                        raise
                    except Exception:
                        summaries.append({
                            "provider": candidate.value,
                            "discovered": 0,
                            "indexed": 0,
                            "rebuilt": 0,
                            "failed": 1,
                            "duration_ms": 0,
                        })
                    finally:
                        self._release_provider_runtime()
                return {
                    "provider": None,
                    **{
                        field: sum(
                            float(summary.get(field, 0))
                            if field == "duration_ms"
                            else int(summary.get(field, 0))
                            for summary in summaries
                        )
                        for field in (
                            "discovered",
                            "indexed",
                            "rebuilt",
                            "failed",
                            "duration_ms",
                        )
                    },
                }
            coordinator = self._provider_runtime(
                targets=False,
                catalog_only=True,
                providers=(selected,),
            )
            summary = asyncio.run(coordinator.scan_all_history(selected))
            return asdict(summary)
        except ConfigurationFailure:
            raise
        except (OSError, PermissionError, ValueError) as exc:
            raise ConfigurationFailure("scan_configuration_failed") from exc
        except Exception as exc:
            raise ProviderDegraded("provider_scan_failed") from exc

    def status(self) -> Mapping[str, Any]:
        catalog = self._require_catalog()
        store = self._require_store()
        catalog_status = catalog.status()
        queue_counts = store.mirror_job_counts()
        breaker = store.get_mirror_breaker_progress()
        policy = self._policy()
        progress = BatchProgress(
            attempts=int(breaker.get("attempts", 0)),
            errors=int(breaker.get("errors", 0)),
        )
        degraded_catalog = any(
            int(value.get("degraded", 0)) > 0
            for value in catalog_status.get("providers", {}).values()
            if isinstance(value, Mapping)
        )
        manual_failures = int(queue_counts.get(MirrorJobState.MANUAL_FAILURE.value, 0))
        return {
            **catalog_status,
            "healthy": not degraded_catalog and manual_failures == 0,
            "mirror_mode": (
                "automatic" if self.config.mirrors.automatic_creation else "manual"
            ),
            "queue_counts": queue_counts,
            "rollout_breaker": {
                "attempts": progress.attempts,
                "errors": progress.errors,
                "halted": should_halt_batch(progress, policy),
            },
        }

    def sidebar_status(self) -> Mapping[str, Any]:
        status_time = time.time()
        raw = self._require_store().sidebar_delivery_status(
            now=status_time,
            inbox_cwd=self.config.sidebar.inbox_cwd,
            placement_generation=self.config.sidebar.placement_generation,
        )
        return _public_sidebar_status(
            raw,
            now=status_time,
            heartbeat_interval_seconds=self.config.sidebar.heartbeat_interval_seconds,
            heartbeat_grace_seconds=self.config.sidebar.heartbeat_grace_seconds,
            oldest_job_alert_seconds=self.config.sidebar.oldest_job_alert_seconds,
            broker_thread_id=self.config.sidebar.broker_thread_id,
            broker_project_id=self.config.sidebar.broker_project_id,
            broker_cwd=self.config.sidebar.broker_cwd,
        )

    def configure_sidebar_broker(
        self,
        *,
        thread_id: str,
        project_id: str,
        cwd: str,
        inbox_cwd: str,
    ) -> Mapping[str, Any]:
        values = {
            "broker_thread_id": _canonical_sidebar_broker_value(thread_id),
            "broker_project_id": _canonical_sidebar_broker_value(project_id),
            "broker_cwd": _canonical_sidebar_broker_value(cwd),
            "inbox_cwd": _canonical_sidebar_broker_value(inbox_cwd),
        }
        persisted_values = {
            "delivery_mode": "desktop_broker",
            **values,
            "heartbeat_interval_seconds": 60,
            "heartbeat_grace_seconds": 120,
            "oldest_job_alert_seconds": 300,
            "readable_preview_enabled": True,
        }
        from hermes_cli.config import ConfigPersistenceRejected, mutate_config

        def _mutate(document: dict[str, Any]) -> None:
            session_bridge = document.setdefault("session_bridge", {})
            if not isinstance(session_bridge, dict):
                raise ConfigurationFailure("invalid_session_bridge_config")
            sidebar = session_bridge.setdefault("sidebar", {})
            if not isinstance(sidebar, dict):
                raise ConfigurationFailure("invalid_sidebar_config")
            sidebar.update(persisted_values)

        try:
            persisted = mutate_config(
                _mutate,
                preserve_keys={
                    ("session_bridge", "sidebar", key)
                    for key in persisted_values
                },
            )
        except ConfigPersistenceRejected as exc:
            raise ConfigurationFailure("config_persistence_rejected") from exc
        sidebar = _persisted_sidebar_values(persisted)
        if {key: sidebar.get(key) for key in persisted_values} != persisted_values:
            raise ConfigurationFailure("sidebar_broker_not_persisted")
        reloaded = BridgeConfig.load()
        configured = reloaded.sidebar
        if {
            "delivery_mode": configured.delivery_mode,
            "broker_thread_id": configured.broker_thread_id,
            "broker_project_id": configured.broker_project_id,
            "broker_cwd": configured.broker_cwd,
            "inbox_cwd": configured.inbox_cwd,
            "heartbeat_interval_seconds": configured.heartbeat_interval_seconds,
            "heartbeat_grace_seconds": configured.heartbeat_grace_seconds,
            "oldest_job_alert_seconds": configured.oldest_job_alert_seconds,
            "readable_preview_enabled": configured.readable_preview_enabled,
        } != persisted_values:
            raise ConfigurationFailure("sidebar_broker_reload_mismatch")
        self.config = reloaded
        return persisted_values

    def sidebar_backfill(
        self, *, days: int | None, limit: int, apply: bool
    ) -> Mapping[str, Any]:
        coordinator = SessionBridgeCoordinator(
            config=self.config,
            store=self._require_store(),
            adapters={},
            target_adapters={},
            clock=time.time,
        )
        summary = asyncio.run(
            coordinator.backfill_sidebar_jobs_once(
                days=days,
                limit=limit,
                apply=apply,
            )
        )
        payload = asdict(summary)
        result = {
            "mode": "apply" if apply else "dry_run",
            "scope": "all_history" if days is None else "days",
            "days": days,
            "limit": limit,
            **payload,
        }
        if not apply:
            result["would_queue"] = payload["queued"]
            result["queued"] = 0
        return result

    def set_sidebar_continuous(self, *, enabled: bool) -> Mapping[str, Any]:
        if type(enabled) is not bool:
            raise ConfigurationFailure("invalid_sidebar_continuous_mode")
        from hermes_cli.config import ConfigPersistenceRejected, mutate_config

        def _mutate(document: dict[str, Any]) -> None:
            session_bridge = document.get("session_bridge")
            if session_bridge is None:
                session_bridge = {}
                document["session_bridge"] = session_bridge
            if not isinstance(session_bridge, dict):
                raise ConfigurationFailure("invalid_session_bridge_config")
            sidebar = session_bridge.get("sidebar")
            if sidebar is None:
                sidebar = {}
                session_bridge["sidebar"] = sidebar
            if not isinstance(sidebar, dict):
                raise ConfigurationFailure("invalid_sidebar_config")
            sidebar["continuous"] = enabled

        try:
            persisted = mutate_config(
                _mutate,
                preserve_keys={("session_bridge", "sidebar", "continuous")},
            )
        except ConfigPersistenceRejected as exc:
            raise ConfigurationFailure("config_persistence_rejected") from exc

        persisted_bridge = persisted.get("session_bridge")
        persisted_sidebar = (
            persisted_bridge.get("sidebar")
            if isinstance(persisted_bridge, dict)
            else None
        )
        persisted_continuous = (
            persisted_sidebar.get("continuous")
            if isinstance(persisted_sidebar, dict)
            else None
        )
        if type(persisted_continuous) is not bool:
            raise ConfigurationFailure("invalid_persisted_sidebar_config")
        if persisted_continuous is not enabled:
            raise ConfigurationFailure("sidebar_continuous_not_persisted")
        self.config = replace(
            self.config,
            sidebar=replace(
                self.config.sidebar,
                continuous=persisted_continuous,
            ),
        )
        return {
            "enabled": self.config.sidebar.enabled,
            "continuous": persisted_continuous,
        }

    def set_sidebar_readable_preview(self, *, enabled: bool) -> Mapping[str, Any]:
        persisted = self._set_sidebar_feature_flag(
            "readable_preview_enabled",
            enabled=enabled,
        )
        return {"readable_preview_enabled": persisted}

    def set_sidebar_hydration(self, *, enabled: bool) -> Mapping[str, Any]:
        persisted = self._set_sidebar_feature_flag(
            "legacy_hydration_enabled",
            enabled=enabled,
        )
        return {"legacy_hydration_enabled": persisted}

    def _set_sidebar_feature_flag(self, key: str, *, enabled: bool) -> bool:
        if (
            key not in {"readable_preview_enabled", "legacy_hydration_enabled"}
            or type(enabled) is not bool
        ):
            raise ConfigurationFailure("invalid_sidebar_feature_mode")
        from hermes_cli.config import ConfigPersistenceRejected, mutate_config

        def _mutate(document: dict[str, Any]) -> None:
            session_bridge = document.setdefault("session_bridge", {})
            if not isinstance(session_bridge, dict):
                raise ConfigurationFailure("invalid_session_bridge_config")
            sidebar = session_bridge.setdefault("sidebar", {})
            if not isinstance(sidebar, dict):
                raise ConfigurationFailure("invalid_sidebar_config")
            sidebar[key] = enabled

        try:
            persisted = mutate_config(
                _mutate,
                preserve_keys={("session_bridge", "sidebar", key)},
            )
        except ConfigPersistenceRejected as exc:
            raise ConfigurationFailure("config_persistence_rejected") from exc
        persisted_bridge = persisted.get("session_bridge")
        persisted_sidebar = (
            persisted_bridge.get("sidebar")
            if isinstance(persisted_bridge, dict)
            else None
        )
        value = (
            persisted_sidebar.get(key)
            if isinstance(persisted_sidebar, dict)
            else None
        )
        if type(value) is not bool or value is not enabled:
            raise ConfigurationFailure("sidebar_feature_not_persisted")
        self.config = replace(
            self.config,
            sidebar=replace(self.config.sidebar, **{key: value}),
        )
        return value

    def sidebar_hydration_seed(
        self,
        *,
        source_session_id: str,
        codex_thread_id: str,
        confirmation: str,
    ) -> Mapping[str, Any]:
        if confirmation != "HYDRATE_EXACT_EXISTING_TASK":
            raise RolloutGateBlocked("sidebar_hydration_confirmation_required")
        store = self._require_store()
        job = store.get_sidebar_job_for_source(source_session_id)
        if (
            job is None
            or job.get("state") != SidebarJobState.VISIBLE.value
            or job.get("codex_thread_id") != codex_thread_id
        ):
            raise RolloutGateBlocked("sidebar_hydration_target_mismatch")
        bridge_id = str(job.get("bridge_id") or "")
        with store.db._lock:
            conn = store.db._conn
            assert conn is not None
            lineage = conn.execute(
                """SELECT 1
                     FROM session_links AS link
                     JOIN external_sessions AS target
                       ON target.session_id = link.to_session_id
                    WHERE link.bridge_id = ?
                      AND link.from_session_id = ?
                      AND link.relation = ?
                      AND target.provider = ?
                      AND target.native_id = ?
                      AND target.origin_bridge_id = ?
                    LIMIT 1""",
                (
                    bridge_id,
                    source_session_id,
                    "mirrors",
                    Provider.CODEX.value,
                    codex_thread_id,
                    bridge_id,
                ),
            ).fetchone()
        if lineage is None:
            raise RolloutGateBlocked("sidebar_hydration_lineage_mismatch")

        candidate = store.get_sidebar_candidate_for_delivery(source_session_id)
        snapshot = store.get_sidebar_preview_source(source_session_id)
        if (
            candidate.bridge_id != bridge_id
            or snapshot.get("source_session_id") != source_session_id
            or snapshot.get("provider") != candidate.provider.value
        ):
            raise RolloutGateBlocked("sidebar_hydration_source_mismatch")
        preview = build_session_preview(
            source_session_id=source_session_id,
            source_cursor=str(snapshot["source_cursor"]),
            source_hash=str(snapshot["source_hash"]),
            title=cast(str | None, snapshot.get("title")),
            provider=candidate.provider.value,
            cwd=candidate.cwd,
            captured_at=float(snapshot["captured_at"]),
            messages=cast(Sequence[Mapping[str, Any]], snapshot["messages"]),
            git_root=candidate.git_root,
            git_branch=candidate.git_branch,
            git_head=candidate.git_head,
            worktree_id=candidate.worktree_id,
            budget_chars=self.config.sidebar.preview_budget_chars,
        )
        marker = encode_hydration_marker(
            HydrationMarkerPayload(
                bridge_id=bridge_id,
                codex_thread_id=codex_thread_id,
                preview_digest=preview.digest,
                preview_version=preview.version,
                source_cursor=preview.source_cursor,
                source_hash=preview.source_hash,
                source_session_id=source_session_id,
            ),
            resolve_marker_key(),
        )
        seeded = store.seed_sidebar_hydration_job(
            source_session_id=source_session_id,
            bridge_id=bridge_id,
            codex_thread_id=codex_thread_id,
            source_cursor=preview.source_cursor,
            source_hash=preview.source_hash,
            preview_version=preview.version,
            preview_digest=preview.digest,
            hydration_marker=marker,
            now=time.time(),
        )
        return {
            "job_id": seeded["id"],
            "source_session_id": seeded["source_session_id"],
            "codex_thread_id": seeded["codex_thread_id"],
            "state": seeded["state"],
            "preview_version": int(seeded["preview_version"]),
            "preview_digest": seeded["preview_digest"],
        }

    def sidebar_hydration_seed_backfill(
        self,
        *,
        days: int | None,
        limit: int,
        apply: bool,
        confirmation: str | None,
    ) -> Mapping[str, Any]:
        if not self.config.sidebar.legacy_hydration_enabled:
            raise RolloutGateBlocked("sidebar_hydration_disabled")
        if type(apply) is not bool:
            raise ConfigurationFailure("invalid_sidebar_hydration_backfill_mode")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ConfigurationFailure("invalid_sidebar_hydration_backfill_limit")
        if apply and confirmation != "HYDRATE_ALL_EXACT_EXISTING_TASKS":
            raise RolloutGateBlocked(
                "sidebar_hydration_backfill_confirmation_required"
            )
        if not apply and confirmation is not None:
            raise RolloutGateBlocked(
                "sidebar_hydration_backfill_confirmation_without_apply"
            )

        store = self._require_store()
        inventory = store.list_sidebar_hydration_candidates(
            now=time.time(),
            backfill_days=days,
            limit=limit,
        )

        marker_secret = resolve_marker_key()
        native = self._require_sidebar_terminal_delivery()
        eligible: list[dict[str, Any]] = []
        already_readable = 0
        blocked_codes: dict[str, int] = {}
        for row in inventory:
            source_session_id = str(row.get("source_session_id") or "")
            bridge_id = str(row.get("bridge_id") or "")
            thread_id = str(row.get("codex_thread_id") or "")
            try:
                if (
                    not source_session_id
                    or not bridge_id
                    or not thread_id
                    or sidebar_bridge_id(source_session_id) != bridge_id
                ):
                    raise ValueError("hydration inventory identity mismatch")
                prompt = native.read_thread_initial_prompt(
                    thread_id=thread_id,
                    deadline=time.monotonic() + 60.0,
                )
                kind = classify_sidebar_initial_prompt(prompt, marker_secret)
                expected_source_line = (
                    "Source session ID: "
                    + json.dumps(
                        source_session_id,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                if expected_source_line not in prompt.splitlines():
                    code = "hydration_target_identity_mismatch"
                elif kind is SidebarInitialPromptKind.LEGACY_PLACEHOLDER:
                    eligible.append(row)
                    continue
                elif kind is SidebarInitialPromptKind.READABLE_REGISTRATION:
                    already_readable += 1
                    continue
                else:
                    code = "hydration_target_unrelated"
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                code = "hydration_target_unreadable"
            blocked_codes[code] = blocked_codes.get(code, 0) + 1

        candidate_payloads = [
            {
                "source_session_id": str(row["source_session_id"]),
                "codex_thread_id": str(row["codex_thread_id"]),
                "visible_at": float(row["visible_at"]),
                "hydration_state": "not_seeded",
            }
            for row in eligible
        ]
        result = {
            "mode": "apply" if apply else "dry_run",
            "scope": "all_history" if days is None else "days",
            "days": days,
            "limit": limit,
            "examined": len(inventory),
            "eligible": len(eligible),
            "already_readable": already_readable,
            "seeded": 0,
            "blocked": sum(blocked_codes.values()),
            "blocked_codes": dict(sorted(blocked_codes.items())),
            "candidates": candidate_payloads,
        }
        if not apply or blocked_codes:
            return result

        seeded = 0
        seeded_candidates: list[dict[str, Any]] = []
        for row, candidate in zip(eligible, candidate_payloads, strict=True):
            seeded_result = self.sidebar_hydration_seed(
                source_session_id=str(row["source_session_id"]),
                codex_thread_id=str(row["codex_thread_id"]),
                confirmation="HYDRATE_EXACT_EXISTING_TASK",
            )
            if (
                seeded_result.get("source_session_id")
                != candidate["source_session_id"]
                or seeded_result.get("codex_thread_id")
                != candidate["codex_thread_id"]
            ):
                raise ConfigurationFailure(
                    "sidebar_hydration_backfill_seed_identity_mismatch"
                )
            hydration_state = seeded_result.get("state")
            if not isinstance(hydration_state, str) or not hydration_state:
                raise ConfigurationFailure(
                    "sidebar_hydration_backfill_seed_state_invalid"
                )
            seeded_candidates.append(
                {**candidate, "hydration_state": hydration_state}
            )
            seeded += 1
        return {
            **result,
            "seeded": seeded,
            "candidates": seeded_candidates,
        }

    def sidebar_hydration_status(self) -> Mapping[str, Any]:
        return _public_sidebar_hydration_status(
            self._require_store().sidebar_hydration_status(time.time()),
            enabled=self.config.sidebar.legacy_hydration_enabled,
        )

    def sidebar_run_once(self) -> Mapping[str, Any]:
        raise RolloutGateBlocked("desktop_broker_required")

    def run_sidebar_recovery_once(self) -> Mapping[str, Any]:
        raise RolloutGateBlocked("desktop_broker_required")

    def _register_sidebar_catalog_once(self):
        coordinator = SessionBridgeCoordinator(
            config=self.config,
            store=self._require_store(),
            adapters={},
            target_adapters={},
            clock=time.time,
        )
        return asyncio.run(
            coordinator.register_sidebar_jobs_once(
                limit=self.config.sidebar.continuous_batch_limit,
            )
        )

    def _record_sidebar_recovery_progress(self, *, lane: str, status: str) -> None:
        try:
            self._require_store().record_sidebar_recovery_progress(
                lane=lane,
                status=status,
                now=time.time(),
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _LOG.warning(
                "sidebar recovery progress write failed",
                exc_info=True,
            )

    def _recycle_sidebar_delivery_runtime(self) -> None:
        sidebar_client, self._sidebar_codex_client = (
            self._sidebar_codex_client,
            None,
        )
        registration_client, self._sidebar_registration_codex_client = (
            self._sidebar_registration_codex_client,
            None,
        )
        self._sidebar_executor = None
        self._sidebar_hydration_executor = None
        closed_clients: set[int] = set()
        for client in (sidebar_client, registration_client):
            if client is None or id(client) in closed_clients:
                continue
            closed_clients.add(id(client))
            try:
                client.close()
            except Exception:
                _LOG.warning("sidebar Codex client recycle failed", exc_info=True)

    def sidebar_retry_bound(
        self,
        *,
        job_id: str,
        source_session_id: str,
        codex_thread_id: str,
        expected_error_code: str,
        confirmation: str,
    ) -> Mapping[str, Any]:
        """Requeue one exact failed bound task without replacement authority."""

        if (
            re.fullmatch(r"sidebar-job:[0-9a-f]{64}", job_id) is None
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,511}", codex_thread_id)
            is None
            or not sidebar_bound_retry_authority_matches(
                expected_error_code,
                confirmation,
            )
        ):
            raise RolloutGateBlocked("sidebar_bound_retry_snapshot_mismatch")
        try:
            result = self._require_store().retry_failed_bound_sidebar_job(
                job_id=job_id,
                source_session_id=source_session_id,
                codex_thread_id=codex_thread_id,
                expected_error_code=expected_error_code,
                confirmation=confirmation,
                now=time.time(),
            )
        except (TypeError, ValueError):
            raise RolloutGateBlocked(
                "sidebar_bound_retry_snapshot_mismatch"
            ) from None
        return {
            "status": "requeued",
            "job_id": result["id"],
            "codex_thread_id": result["codex_thread_id"],
            "error_code": expected_error_code,
            "state": result["state"],
        }

    def _apply_sidebar_create_reservation_cutover(
        self,
        *,
        marker_secret: bytes | None = None,
    ) -> Mapping[str, Any]:
        secret = resolve_marker_key() if marker_secret is None else marker_secret
        try:
            return self._require_store().apply_sidebar_create_reservation_cutover(
                marker_secret=secret,
                now=time.time(),
            )
        except ConfigurationFailure:
            raise
        except Exception as exc:
            raise ConfigurationFailure(
                "sidebar_create_reservation_cutover_failed"
            ) from exc

    def sidebar_acknowledge_unrecoverable(
        self,
        *,
        job_id: str,
        codex_thread_id: str,
        expected_error_code: str,
    ) -> Mapping[str, Any]:
        """Prove one exact bound native task is unrecoverable, then append evidence."""

        if (
            re.fullmatch(r"sidebar-job:[0-9a-f]{64}", job_id) is None
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,511}", codex_thread_id)
            is None
            or expected_error_code != "native_create_ambiguous"
        ):
            raise RolloutGateBlocked("sidebar_terminal_snapshot_mismatch")

        store = self._require_store()
        try:
            job = store.get_sidebar_job_by_id(job_id)
            if job is None:
                raise ValueError("missing sidebar job")
            source_session_id = job.get("source_session_id")
            if not isinstance(source_session_id, str):
                raise ValueError("missing sidebar source identity")
            expected_idempotency_key = sidebar_idempotency_key(source_session_id)
            expected_bridge_id = sidebar_bridge_id(source_session_id)
            expected_job_id = (
                "sidebar-job:"
                + hashlib.sha256(expected_idempotency_key.encode("utf-8")).hexdigest()
            )
            attempts = job.get("attempts")
            next_attempt_at = job.get("next_attempt_at")
            updated_at = job.get("updated_at")
            if (
                job.get("id") != job_id
                or job_id != expected_job_id
                or job.get("idempotency_key") != expected_idempotency_key
                or job.get("bridge_id") != expected_bridge_id
                or job.get("codex_thread_id") != codex_thread_id
                or job.get("state") != SidebarJobState.FAILED.value
                or job.get("error_code") != expected_error_code
                or type(attempts) is not int
                or cast(int, attempts) < 0
                or not _is_finite_number(next_attempt_at)
                or not _is_finite_number(updated_at)
                or job.get("lease_digest") is not None
                or job.get("lease_expires_at") is not None
                or job.get("completion_digest") is not None
                or job.get("visible_at") is not None
            ):
                raise ValueError("sidebar terminal snapshot mismatch")
            reservation = store.get_sidebar_create_reservation(source_session_id)
            if (
                reservation is None
                or reservation.get("job_id") != job_id
                or reservation.get("bridge_id") != expected_bridge_id
            ):
                raise ValueError("sidebar terminal reservation mismatch")
        except (TypeError, ValueError):
            raise RolloutGateBlocked("sidebar_terminal_snapshot_mismatch") from None

        delivery = self._require_sidebar_terminal_delivery()
        try:
            native_state = delivery.read_thread_state(
                thread_id=codex_thread_id,
                deadline=time.monotonic() + 240.0,
            )
        except NativeThreadUnrecoverable as exc:
            if exc.thread_id != codex_thread_id:
                raise ProviderDegraded("sidebar_terminal_probe_failed") from None
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise ProviderDegraded("sidebar_terminal_probe_failed") from None
        else:
            del native_state
            raise RolloutGateBlocked("native_thread_materialized")

        evidence_digest = sidebar_terminal_evidence_digest(
            job=job,
            reservation=reservation,
        )
        try:
            result = store.acknowledge_sidebar_terminal_resolution(
                job_id=job_id,
                codex_thread_id=codex_thread_id,
                expected_error_code=expected_error_code,
                expected_attempts=job["attempts"],
                expected_next_attempt_at=job["next_attempt_at"],
                expected_updated_at=job["updated_at"],
                evidence_digest=evidence_digest,
                now=time.time(),
            )
        except (TypeError, ValueError):
            raise RolloutGateBlocked("sidebar_terminal_snapshot_mismatch") from None
        return {
            "status": (
                "acknowledged"
                if result.get("created") is True
                else "already_acknowledged"
            ),
            "error_code": "native_create_ambiguous",
            "resolution_code": SIDEBAR_TERMINAL_RESOLUTION_CODE,
        }

    def sidebar_acknowledge_precreate_unrecoverable(
        self,
        *,
        job_id: str,
        expected_error_code: str,
    ) -> Mapping[str, Any]:
        """Prove one quarantined no-ID create has no native task, then audit it."""

        if (
            re.fullmatch(r"sidebar-job:[0-9a-f]{64}", job_id) is None
            or expected_error_code != "native_create_ambiguous"
        ):
            raise RolloutGateBlocked("sidebar_precreate_snapshot_mismatch")

        try:
            marker_secret = resolve_marker_key()
            if type(marker_secret) is not bytes or not marker_secret:
                raise ValueError("marker key is unavailable")
        except (OSError, PermissionError, RuntimeError, TypeError, ValueError):
            raise ConfigurationFailure("sidebar_precreate_probe_unavailable") from None

        store = self._require_store()
        try:
            job = store.get_sidebar_job_by_id(job_id)
            if job is None:
                raise ValueError("missing sidebar job")
            source_session_id = job.get("source_session_id")
            if not isinstance(source_session_id, str):
                raise ValueError("missing sidebar source identity")
            expected_idempotency_key = sidebar_idempotency_key(source_session_id)
            expected_bridge_id = sidebar_bridge_id(source_session_id)
            expected_job_id = (
                "sidebar-job:"
                + hashlib.sha256(expected_idempotency_key.encode("utf-8")).hexdigest()
            )
            attempts = job.get("attempts")
            next_attempt_at = job.get("next_attempt_at")
            updated_at = job.get("updated_at")
            if (
                job.get("id") != job_id
                or job_id != expected_job_id
                or job.get("idempotency_key") != expected_idempotency_key
                or job.get("bridge_id") != expected_bridge_id
                or job.get("codex_thread_id") is not None
                or job.get("state") != SidebarJobState.FAILED.value
                or job.get("error_code") != expected_error_code
                or type(attempts) is not int
                or attempts != 0
                or not _is_finite_number(next_attempt_at)
                or not _is_finite_number(updated_at)
                or not _is_finite_number(job.get("eligible_at"))
                or not _is_finite_number(job.get("created_at"))
                or job.get("lease_digest") is not None
                or job.get("lease_expires_at") is not None
                or job.get("completion_digest") is not None
                or job.get("visible_at") is not None
            ):
                raise ValueError("sidebar precreate snapshot mismatch")

            candidate = store.get_sidebar_candidate_for_delivery(source_session_id)
            if (
                not isinstance(candidate, SidebarCandidate)
                or candidate.source_session_id != source_session_id
                or candidate.bridge_id != expected_bridge_id
                or candidate.eligible_at != float(job["eligible_at"])
            ):
                raise ValueError("sidebar precreate candidate mismatch")
            expected_marker = BridgeMarkerPayload(
                bridge_id=expected_bridge_id,
                source_session_id=source_session_id,
                target_provider=Provider.CODEX,
                policy_generation=1,
            )
            marker = encode_bridge_marker(expected_marker, marker_secret)
            expected_recovery_key = sidebar_create_recovery_key(
                marker,
                marker_secret,
            )

            reservation = store.get_sidebar_create_reservation(source_session_id)
            if (
                reservation is None
                or set(reservation)
                != {
                    "version",
                    "job_id",
                    "source_session_id",
                    "bridge_id",
                    "recovery_key",
                    "reserved_at",
                }
                or reservation.get("version") != 1
                or reservation.get("job_id") != job_id
                or reservation.get("source_session_id") != source_session_id
                or reservation.get("bridge_id") != expected_bridge_id
                or reservation.get("recovery_key") != expected_recovery_key
                or not _is_finite_number(reservation.get("reserved_at"))
            ):
                raise ValueError("sidebar precreate reservation mismatch")

            cutover = store.get_state(_SIDEBAR_CREATE_RESERVATION_CUTOVER_STATE_KEY)
            if cutover is None:
                raise ValueError("missing sidebar precreate cutover")
            quarantined_job_ids = cutover.get("quarantined_job_ids")
            if (
                set(cutover) != {"version", "applied_at", "quarantined_job_ids"}
                or cutover.get("version") != 1
                or not _is_finite_number(cutover.get("applied_at"))
                or not isinstance(quarantined_job_ids, list)
                or any(
                    not isinstance(value, str)
                    or re.fullmatch(r"sidebar-job:[0-9a-f]{64}", value) is None
                    for value in quarantined_job_ids
                )
                or quarantined_job_ids != sorted(set(quarantined_job_ids))
                or job_id not in quarantined_job_ids
                or reservation["reserved_at"] != cutover["applied_at"]
            ):
                raise ValueError("sidebar precreate cutover mismatch")
        except (KeyError, TypeError, ValueError):
            raise RolloutGateBlocked("sidebar_precreate_snapshot_mismatch") from None

        verifier = self._require_sidebar_terminal_verifier(
            marker_secret=marker_secret,
        )
        try:
            marker_match = verifier.find_by_marker_including_archived(expected_marker)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise ProviderDegraded("sidebar_precreate_probe_failed") from None
        if marker_match is not None:
            raise RolloutGateBlocked("native_thread_materialized")
        try:
            recovery_match = verifier.find_by_recovery_key(
                reservation["recovery_key"],
                expected_cwd=candidate.cwd,
                deadline=time.monotonic() + 240.0,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise ProviderDegraded("sidebar_precreate_probe_failed") from None
        if recovery_match is not None:
            raise RolloutGateBlocked("native_thread_materialized")

        evidence_digest = sidebar_precreate_terminal_evidence_digest(
            job=job,
            reservation=reservation,
            cutover=cutover,
            candidate=candidate,
        )
        try:
            result = store.acknowledge_sidebar_precreate_resolution(
                job_id=job_id,
                expected_error_code=expected_error_code,
                expected_attempts=job["attempts"],
                expected_next_attempt_at=job["next_attempt_at"],
                expected_updated_at=job["updated_at"],
                evidence_digest=evidence_digest,
                marker_secret=marker_secret,
                now=time.time(),
            )
        except (TypeError, ValueError):
            raise RolloutGateBlocked("sidebar_precreate_snapshot_mismatch") from None
        return {
            "status": (
                "acknowledged"
                if result.get("created") is True
                else "already_acknowledged"
            ),
            "error_code": "native_create_ambiguous",
            "resolution_code": SIDEBAR_PRECREATE_RESOLUTION_CODE,
        }

    def sidebar_acknowledge_unbound_unrecoverable(
        self,
        *,
        job_id: str,
        expected_error_code: str,
    ) -> Mapping[str, Any]:
        """Prove one post-dispatch no-ID create has no native task, then audit it."""

        if (
            re.fullmatch(r"sidebar-job:[0-9a-f]{64}", job_id) is None
            or expected_error_code != "native_create_ambiguous"
        ):
            raise RolloutGateBlocked("sidebar_unbound_snapshot_mismatch")
        try:
            marker_secret = resolve_marker_key()
            if type(marker_secret) is not bytes or not marker_secret:
                raise ValueError("marker key is unavailable")
        except (OSError, PermissionError, RuntimeError, TypeError, ValueError):
            raise ConfigurationFailure("sidebar_unbound_probe_unavailable") from None

        store = self._require_store()
        try:
            job = store.get_sidebar_job_by_id(job_id)
            if job is None:
                raise ValueError("missing sidebar job")
            source_session_id = job.get("source_session_id")
            if not isinstance(source_session_id, str):
                raise ValueError("missing sidebar source identity")
            expected_idempotency_key = sidebar_idempotency_key(source_session_id)
            expected_bridge_id = sidebar_bridge_id(source_session_id)
            expected_job_id = (
                "sidebar-job:"
                + hashlib.sha256(expected_idempotency_key.encode("utf-8")).hexdigest()
            )
            attempts = job.get("attempts")
            next_attempt_at = job.get("next_attempt_at")
            updated_at = job.get("updated_at")
            if (
                job.get("id") != job_id
                or job_id != expected_job_id
                or job.get("idempotency_key") != expected_idempotency_key
                or job.get("bridge_id") != expected_bridge_id
                or job.get("codex_thread_id") is not None
                or job.get("state") != SidebarJobState.FAILED.value
                or job.get("error_code") != expected_error_code
                or type(attempts) is not int
                or attempts <= 0
                or not _is_finite_number(next_attempt_at)
                or not _is_finite_number(updated_at)
                or not _is_finite_number(job.get("eligible_at"))
                or not _is_finite_number(job.get("created_at"))
                or job.get("lease_digest") is not None
                or job.get("lease_expires_at") is not None
                or job.get("completion_digest") is not None
                or job.get("visible_at") is not None
            ):
                raise ValueError("sidebar unbound snapshot mismatch")

            candidate = store.get_sidebar_candidate_for_delivery(source_session_id)
            if (
                not isinstance(candidate, SidebarCandidate)
                or candidate.source_session_id != source_session_id
                or candidate.bridge_id != expected_bridge_id
                or candidate.eligible_at != float(job["eligible_at"])
            ):
                raise ValueError("sidebar unbound candidate mismatch")
            expected_marker = BridgeMarkerPayload(
                bridge_id=expected_bridge_id,
                source_session_id=source_session_id,
                target_provider=Provider.CODEX,
                policy_generation=1,
            )
            marker = encode_bridge_marker(expected_marker, marker_secret)
            expected_recovery_key = sidebar_create_recovery_key(
                marker,
                marker_secret,
            )
            reservation = store.get_sidebar_create_reservation(source_session_id)
            validate_sidebar_create_reservation(
                reservation,
                job_id=job_id,
                source_session_id=source_session_id,
                bridge_id=expected_bridge_id,
                expected_recovery_key=expected_recovery_key,
            )
        except (KeyError, TypeError, ValueError):
            raise RolloutGateBlocked("sidebar_unbound_snapshot_mismatch") from None

        verifier = self._require_sidebar_terminal_verifier(
            marker_secret=marker_secret,
        )
        try:
            marker_match = verifier.find_by_marker_including_archived(expected_marker)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise ProviderDegraded("sidebar_unbound_probe_failed") from None
        if marker_match is not None:
            raise RolloutGateBlocked("native_thread_materialized")
        try:
            recovery_match = verifier.find_by_recovery_key(
                reservation["recovery_key"],
                expected_cwd=candidate.cwd,
                deadline=time.monotonic() + 240.0,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise ProviderDegraded("sidebar_unbound_probe_failed") from None
        if recovery_match is not None:
            raise RolloutGateBlocked("native_thread_materialized")

        evidence_digest = sidebar_unbound_terminal_evidence_digest(
            job=job,
            reservation=reservation,
            candidate=candidate,
        )
        try:
            result = store.acknowledge_sidebar_unbound_resolution(
                job_id=job_id,
                expected_error_code=expected_error_code,
                expected_attempts=job["attempts"],
                expected_next_attempt_at=job["next_attempt_at"],
                expected_updated_at=job["updated_at"],
                evidence_digest=evidence_digest,
                marker_secret=marker_secret,
                now=time.time(),
            )
        except (TypeError, ValueError):
            raise RolloutGateBlocked("sidebar_unbound_snapshot_mismatch") from None
        return {
            "status": (
                "acknowledged"
                if result.get("created") is True
                else "already_acknowledged"
            ),
            "error_code": "native_create_ambiguous",
            "resolution_code": SIDEBAR_UNBOUND_RESOLUTION_CODE,
        }

    def claude_visibility_status(self) -> Mapping[str, Any]:
        config = self.config.claude_visibility
        store = self._require_store()
        raw = store.claude_visibility_status(time.time())
        status_fatal = _claude_visibility_fatal_reasons(raw)
        lineage = _public_claude_visibility_lineage(raw)
        degraded = list(status_fatal)
        if lineage["unlinked_visible"] > 0:
            degraded.append("unlinked_visible_lineage")
        return {
            "enabled": config.enabled,
            "continuous": config.continuous,
            "counts": dict(raw["counts"]),
            "retry_codes": dict(raw["retry_codes"]),
            "failed_codes": dict(raw["failed_codes"]),
            "usage": dict(raw["usage"]),
            "lineage": lineage,
            "fatal": list(raw.get("fatal", [])),
            "repair_required": _claude_visibility_repair_required(raw),
            "candidates": [],
            "exclusions": [],
            "open_reasons": _claude_visibility_open_reasons(raw),
            "fatal_reasons": status_fatal,
            "degraded_reasons": sorted(set(degraded)),
            "last_cycle": dict(
                raw.get("last_cycle", {"tracked": False, "value": None})
            ),
            "last_empty_cycle": dict(
                raw.get("last_empty_cycle", {"tracked": False, "value": None})
            ),
            "last_registrar_result": dict(
                raw.get("last_registrar_result", {"tracked": False, "value": None})
            ),
        }

    def reconcile_claude_visibility_lineage(
        self,
        *,
        limit: int,
        apply: bool,
        cursor: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        marker_secret = resolve_marker_key()
        store = self._require_store()
        if apply:
            source_root = characterization_source_root()
            _sync_claude_characterization_records(
                store=store,
                source_root=source_root,
                marker_secret=marker_secret,
                include_active=False,
                include_completed=True,
            )
        result = store.reconcile_claude_visibility_lineage(
            limit=limit,
            marker_secret=marker_secret,
            apply=apply,
            cursor=cursor,
        )
        return {
            "mode": "apply" if apply else "dry_run",
            "scanned": int(result["scanned"]),
            "repairable": int(result["repairable"]),
            "repaired": int(result["repaired"]),
            "remaining": int(result["remaining"]),
            "blocker_codes": dict(result["blocker_codes"]),
            "next_cursor": result["next_cursor"],
            "has_more": bool(result["has_more"]),
            "complete": bool(result["complete"]),
        }

    def claude_visibility_backfill(
        self, *, days: int, limit: int, apply: bool
    ) -> Mapping[str, Any]:
        if not self.config.claude_visibility.enabled:
            return {
                **_disabled_claude_visibility_payload(
                    self.config.claude_visibility.continuous
                ),
                "mode": "disabled",
                "dry_run": not apply,
                "applied": False,
                "enqueued": 0,
            }
        result = self._claude_visibility_runtime().backfill(
            days=days, limit=limit, apply=apply
        )
        return _public_claude_apply(
            result, continuous=self.config.claude_visibility.continuous
        )

    def set_claude_visibility_continuous(self, *, enabled: bool) -> Mapping[str, Any]:
        if type(enabled) is not bool:
            raise ConfigurationFailure("invalid_claude_visibility_continuous_mode")
        from hermes_cli.config import ConfigPersistenceRejected, mutate_config

        def _mutate(document: dict[str, Any]) -> None:
            session_bridge = document.get("session_bridge")
            if session_bridge is None:
                session_bridge = {}
                document["session_bridge"] = session_bridge
            if not isinstance(session_bridge, dict):
                raise ConfigurationFailure("invalid_session_bridge_config")
            visibility = session_bridge.get("claude_visibility")
            if visibility is None:
                visibility = {}
                session_bridge["claude_visibility"] = visibility
            if not isinstance(visibility, dict):
                raise ConfigurationFailure("invalid_claude_visibility_config")
            visibility["continuous"] = enabled

        try:
            persisted = mutate_config(
                _mutate,
                preserve_keys={("session_bridge", "claude_visibility", "continuous")},
            )
        except ConfigPersistenceRejected as exc:
            raise ConfigurationFailure("config_persistence_rejected") from exc
        bridge = persisted.get("session_bridge")
        visibility = (
            bridge.get("claude_visibility") if isinstance(bridge, dict) else None
        )
        value = visibility.get("continuous") if isinstance(visibility, dict) else None
        if type(value) is not bool:
            raise ConfigurationFailure("invalid_persisted_claude_visibility_config")
        if value is not enabled:
            raise ConfigurationFailure("claude_visibility_continuous_not_persisted")
        self.config = replace(
            self.config,
            claude_visibility=replace(self.config.claude_visibility, continuous=value),
        )
        return {"enabled": self.config.claude_visibility.enabled, "continuous": value}

    def claude_visibility_run_once(self, *, stop: Any = None) -> Mapping[str, Any]:
        if not self.config.claude_visibility.enabled:
            return {
                "enabled": False,
                "continuous": self.config.claude_visibility.continuous,
                "status": "disabled",
                "degraded": False,
                "fatal": False,
            }
        if stop is not None and stop.is_set():
            raise _VisibilityCycleCancelled()
        result = self._claude_visibility_runtime().run_once(
            discover_continuous=True, stop=stop
        )
        return _public_claude_run(
            result, continuous=self.config.claude_visibility.continuous
        )

    def inspect_failed_claude_visibility_job(
        self,
        *,
        job_id: str,
        reserved_claude_uuid: str,
        expected_error_code: str,
    ) -> Mapping[str, Any]:
        """Inspect one exact terminal repair target without acquiring a lease."""

        if expected_error_code != "bridge_conflict":
            raise RolloutGateBlocked("visibility_repair_error_code_mismatch")
        try:
            return self._require_store().inspect_failed_claude_visibility_reconciliation(
                expected_job_id=job_id,
                expected_reserved_claude_uuid=reserved_claude_uuid,
                expected_error_code=expected_error_code,
            )
        except ValueError as exc:
            raise RolloutGateBlocked("visibility_repair_identity_mismatch") from exc

    def repair_failed_claude_visibility_job(
        self,
        *,
        job_id: str,
        reserved_claude_uuid: str,
        expected_error_code: str,
    ) -> Mapping[str, Any]:
        """Reconcile one exact terminal failure without native launch authority."""

        if expected_error_code != "bridge_conflict":
            raise RolloutGateBlocked("visibility_repair_error_code_mismatch")
        store = self._require_store()
        policy = self.config.claude_visibility
        try:
            claim = store.claim_failed_claude_visibility_reconciliation(
                time.time(),
                policy.lease_seconds,
                expected_job_id=job_id,
                expected_reserved_claude_uuid=reserved_claude_uuid,
                expected_error_code=expected_error_code,
            )
        except ValueError as exc:
            raise RolloutGateBlocked("visibility_repair_identity_mismatch") from exc
        authority = (
            getattr(claim, "lease_kind", None),
            getattr(claim, "launch_permitted", None),
            getattr(claim, "registration_reserved", None),
            getattr(claim, "requires_exact_id_reconciliation", None),
        )
        if (
            not getattr(claim, "claimed", False)
            or getattr(claim, "job_id", None) != job_id
            or getattr(claim, "reserved_claude_uuid", None) != reserved_claude_uuid
            or authority != ("reconciliation", False, False, True)
        ):
            raise RolloutGateBlocked("visibility_repair_authority_mismatch")
        marker_secret = resolve_marker_key()
        source = ClaudeSourceAdapter(_CLAUDE_PROJECTS_ROOT, marker_secret=marker_secret)
        registrar = ClaudeNativeRegistrar(
            store,
            source,
            marker_secret=marker_secret,
            startup_theme="light",
            claude_command=(),
            process_timeout=policy.process_timeout_seconds,
            discovery_timeout=policy.discovery_timeout_seconds,
        )
        outcome = registrar.process(claim, allow_absence=False)
        if (
            outcome.job_id != job_id
            or outcome.reserved_claude_uuid != reserved_claude_uuid
        ):
            raise RolloutGateBlocked("visibility_repair_result_identity_mismatch")
        if outcome.status != "visible":
            raise RolloutGateBlocked("visibility_repair_not_committed_visible")
        return {
            "status": outcome.status,
            "job_id": outcome.job_id,
            "reserved_claude_uuid": outcome.reserved_claude_uuid,
            "error_code": outcome.error_code,
        }

    def dismiss_claude_visibility_job(
        self, *, job_id: str, expected_error_code: str
    ) -> Mapping[str, Any]:
        """Acknowledge one terminally failed job so discovery can resume.

        A claude_failed job holds the discovery gate shut on purpose: it is
        open work AND its code is fatal, so the lane keeps cycling and
        enqueues nothing until a human adjudicates the failure. This is the
        adjudication. It stamps the row as operator-cleared; it does not
        rewrite the verdict and it does not touch the paid-attempt ledger.
        """

        store = self._require_store()
        try:
            return store.dismiss_claude_visibility_job(
                job_id=job_id, expected_error_code=expected_error_code
            )
        except ValueError as exc:
            # The guarded UPDATE matched no row: wrong id, a state that is not
            # claude_failed, a different error_code, or already cleared. Say
            # so as a gate refusal rather than a generic configuration error.
            raise RolloutGateBlocked("visibility_dismiss_identity_mismatch") from exc

    def abort_claude_visibility_characterization(
        self, *, expected_job_id: str, expected_reserved_claude_uuid: str
    ) -> Mapping[str, Any]:
        """Terminally retire one exact-UUID probe only after durable absence."""

        if os.environ.get("HERMES_SESSION_BRIDGE_LIVE_TESTS") != "1":
            raise ConfigurationFailure("live_characterization_not_enabled")
        source_root = characterization_source_root()
        active_record = source_root / ".claude-visibility-operation.json"
        marker_secret = resolve_marker_key()
        try:
            if active_record.exists():
                operation = _read_characterization_record(active_record, marker_secret)
            else:
                roots = (
                    source_root / ".abort-completed",
                    source_root / ".abort-claims",
                )
                paths_by_root: list[list[Path]] = []
                for record_root in roots:
                    paths_by_root.append(
                        []
                        if not record_root.exists()
                        else sorted(
                            path
                            for path in record_root.iterdir()
                            if path.is_file() and path.suffix == ".json"
                        )
                    )
                if (
                    sum(len(paths) for paths in paths_by_root)
                    > _CLAUDE_CHARACTERIZATION_SYNC_LIMIT
                ):
                    raise ConfigurationFailure("characterization_record_limit")
                operation = None
                for paths in paths_by_root:
                    matches: list[dict[str, Any]] = []
                    for path in paths:
                        candidate = _read_characterization_record(path, marker_secret)
                        if (
                            candidate.get("job_id") == expected_job_id
                            and candidate.get("reserved_claude_uuid")
                            == expected_reserved_claude_uuid
                        ):
                            matches.append(candidate)
                    if len(matches) > 1:
                        raise ConfigurationFailure("characterization_record_invalid")
                    if len(matches) == 1:
                        operation = matches[0]
                        break
                if operation is None:
                    raise ConfigurationFailure("characterization_record_invalid")
        except RuntimeError:
            raise ConfigurationFailure("characterization_record_invalid") from None
        if operation.get("phase") not in {
            "reserved",
            "launching",
            "abort_disposable_removing",
            "abort_disposable_removed",
            "aborted",
        }:
            raise RolloutGateBlocked("characterization_abort_not_active")
        if (
            operation.get("job_id") != expected_job_id
            or operation.get("reserved_claude_uuid") != expected_reserved_claude_uuid
        ):
            raise RolloutGateBlocked("characterization_abort_identity_mismatch")
        expected_operation_id = str(operation["operation_id"])
        claimed_abort = claim_claude_visibility_characterization_abort(
            source_root=source_root,
            marker_secret=marker_secret,
            expected_operation_id=expected_operation_id,
            expected_job_id=str(operation["job_id"]),
            expected_reserved_claude_uuid=str(operation["reserved_claude_uuid"]),
        )
        if claimed_abort.get("job_id") != operation.get("job_id") or claimed_abort.get(
            "reserved_claude_uuid"
        ) != operation.get("reserved_claude_uuid"):
            raise RolloutGateBlocked("characterization_abort_identity_mismatch")
        claimed_operation = claimed_abort.get("operation")
        if not isinstance(claimed_operation, Mapping):
            raise RolloutGateBlocked("characterization_abort_identity_mismatch")
        operation = dict(claimed_operation)
        if (
            operation.get("operation_id") != expected_operation_id
            or operation.get("job_id") != expected_job_id
            or operation.get("reserved_claude_uuid") != expected_reserved_claude_uuid
            or operation.get("phase")
            not in {
                "reserved",
                "launching",
                "abort_disposable_removing",
                "abort_disposable_removed",
                "aborted",
            }
        ):
            raise RolloutGateBlocked("characterization_abort_identity_mismatch")
        store = self._require_store()
        registered = _record_claude_characterization_payload(
            store=store,
            payload=operation,
            marker_secret=marker_secret,
            cleanup_completed=False,
            ensure_registered=True,
        )
        if registered.get("job_id") != operation.get("job_id") or registered.get(
            "reserved_claude_uuid"
        ) != operation.get("reserved_claude_uuid"):
            raise RolloutGateBlocked("characterization_abort_identity_mismatch")

        def request_abort() -> Mapping[str, Any]:
            result = _record_claude_characterization_payload(
                store=store,
                payload=operation,
                marker_secret=marker_secret,
                cleanup_completed=False,
                launch_aborted=True,
            )
            if result.get("job_id") != operation.get("job_id") or result.get(
                "reserved_claude_uuid"
            ) != operation.get("reserved_claude_uuid"):
                raise RolloutGateBlocked("characterization_abort_identity_mismatch")
            return result

        def terminal_result(*, replayed: bool = False) -> Mapping[str, Any]:
            payload: dict[str, Any] = {
                "status": "aborted_exact_absence",
                "job_id": operation["job_id"],
                "reserved_claude_uuid": operation["reserved_claude_uuid"],
                "replacement_created": False,
                "active_record_retired": True,
            }
            if replayed:
                payload["replayed"] = True
            return payload

        abort = request_abort()
        if abort.get("status") in {"launch_aborted", "already_aborted"}:
            retire_aborted_claude_visibility_characterization(
                source_root=source_root,
                marker_secret=marker_secret,
                expected_operation_id=str(operation["operation_id"]),
                expected_job_id=str(operation["job_id"]),
                expected_reserved_claude_uuid=str(operation["reserved_claude_uuid"]),
            )
            return terminal_result(replayed=abort.get("status") == "already_aborted")
        if abort.get("status") != "reconciliation_required":
            raise RolloutGateBlocked("characterization_abort_not_available")

        policy = self.config.claude_visibility
        claim = store.claim_claude_visibility_reconciliation(
            time.time(),
            policy.lease_seconds,
            expected_job_id=str(operation["job_id"]),
        )
        authority = (
            getattr(claim, "lease_kind", None),
            getattr(claim, "launch_permitted", None),
            getattr(claim, "registration_reserved", None),
            getattr(claim, "requires_exact_id_reconciliation", None),
        )
        if (
            not getattr(claim, "claimed", False)
            or getattr(claim, "job_id", None) != operation["job_id"]
            or getattr(claim, "reserved_claude_uuid", None)
            != operation["reserved_claude_uuid"]
            or authority != ("reconciliation", False, False, True)
        ):
            raise RolloutGateBlocked("characterization_reconciliation_not_available")

        source = ClaudeSourceAdapter(_CLAUDE_PROJECTS_ROOT, marker_secret=marker_secret)
        registrar = ClaudeNativeRegistrar(
            store,
            source,
            marker_secret=marker_secret,
            startup_theme="light",
            claude_command=(),
            process_timeout=policy.process_timeout_seconds,
            discovery_timeout=policy.discovery_timeout_seconds,
        )
        outcome = registrar.process(claim)
        if (
            getattr(outcome, "job_id", None) != operation["job_id"]
            or getattr(outcome, "reserved_claude_uuid", None)
            != operation["reserved_claude_uuid"]
        ):
            raise RolloutGateBlocked("characterization_abort_identity_mismatch")
        if getattr(outcome, "status", None) == "visible":
            raise RolloutGateBlocked("characterization_native_session_materialized")
        if getattr(outcome, "status", None) == "failed":
            raise RolloutGateBlocked("characterization_exact_id_conflict")
        if getattr(outcome, "status", None) != "absent":
            raise ProviderDegraded(
                str(
                    getattr(outcome, "error_code", None)
                    or "characterization_exact_id_lookup_unavailable"
                )
            )

        completed = request_abort()
        if completed.get("status") not in {"launch_aborted", "already_aborted"}:
            raise RolloutGateBlocked("characterization_abort_not_committed")
        retire_aborted_claude_visibility_characterization(
            source_root=source_root,
            marker_secret=marker_secret,
            expected_operation_id=str(operation["operation_id"]),
            expected_job_id=str(operation["job_id"]),
            expected_reserved_claude_uuid=str(operation["reserved_claude_uuid"]),
        )
        return terminal_result(replayed=completed.get("status") == "already_aborted")

    def characterize_claude_visibility(
        self, cleanup_token: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        if os.environ.get("HERMES_SESSION_BRIDGE_LIVE_TESTS") != "1":
            raise ConfigurationFailure("live_characterization_not_enabled")
        source_root = characterization_source_root()
        if cleanup_token is not None:
            marker_secret = resolve_marker_key()
            result = cleanup_characterized_claude_visibility(
                cleanup_token=cleanup_token,
                source_root=source_root,
                projects_root=_CLAUDE_PROJECTS_ROOT,
                restarted_source=lambda: ClaudeSourceAdapter(
                    _CLAUDE_PROJECTS_ROOT, marker_secret=marker_secret
                ),
                marker_secret=marker_secret,
            )
            operation_id = cleanup_token.get("id")
            if not isinstance(operation_id, str) or not operation_id:
                raise ConfigurationFailure("characterization_record_invalid")
            completed_path = source_root / ".cleanup-completed" / f"{operation_id}.json"
            try:
                completed = _read_characterization_record(completed_path, marker_secret)
            except RuntimeError:
                raise ConfigurationFailure("characterization_record_invalid") from None
            if (
                completed.get("phase") != "completed"
                or completed.get("operation_id") != operation_id
            ):
                raise ConfigurationFailure("characterization_record_invalid")
            _record_claude_characterization_payload(
                store=self._require_store(),
                payload=completed,
                marker_secret=marker_secret,
                cleanup_completed=True,
                ensure_registered=False,
            )
            return result
        active_path = source_root / ".claude-visibility-operation.json"
        claude_command: Sequence[str] = ()
        startup: Mapping[str, Any] | None = None
        if not active_path.exists():
            claude_command = resolve_cli_executable("claude")
            preflight = _claude_visibility_preflight_detail(claude_command)
            if preflight.startup is None:
                raise ProviderDegraded(cast(str, preflight.failure_code))
            startup = preflight.startup
        marker_secret = resolve_marker_key()
        store = self._require_store()
        _sync_claude_characterization_records(
            store=store,
            source_root=source_root,
            marker_secret=marker_secret,
            include_active=True,
            include_completed=True,
        )
        active_job_id: str | None = None
        active_payload: Mapping[str, Any] | None = None
        if active_path.exists():
            try:
                active_payload = _read_characterization_record(
                    active_path, marker_secret
                )
            except RuntimeError:
                raise ConfigurationFailure("characterization_record_invalid") from None
            value = active_payload.get("job_id")
            if isinstance(value, str) and value:
                active_job_id = value
        raw = store.claude_visibility_status(time.time())
        auth_recovery_allowed = _claude_characterization_auth_recovery_allowed(
            raw,
            active_operation=active_path.exists(),
            active_job_id=active_job_id,
        )
        status_fatal = _claude_visibility_fatal_reasons(raw)
        if status_fatal and not (
            auth_recovery_allowed and status_fatal == ["bridge_conflict"]
        ):
            raise RolloutGateBlocked("claude_visibility_not_idle")
        has_open_work = any(
            int(raw.get("counts", {}).get(state, 0))
            for state in (
                "claude_pending",
                "claude_leased",
                "claude_retry",
                "claude_failed",
            )
        )
        if has_open_work and not _claude_characterization_open_work_allowed(
            raw,
            active_operation=active_path.exists(),
            active_job_id=active_job_id,
        ):
            raise RolloutGateBlocked("claude_visibility_not_idle")
        characterizations = raw.get("characterizations")
        if not isinstance(characterizations, list):
            raise RolloutGateBlocked("claude_visibility_not_idle")
        active_matches = (
            []
            if active_job_id is None
            else [
                item
                for item in characterizations
                if isinstance(item, Mapping) and item.get("job_id") == active_job_id
            ]
        )
        if len(active_matches) > 1:
            raise RolloutGateBlocked("characterization_status_identity_mismatch")
        if active_job_id is not None and not active_matches:
            assert active_payload is not None
            terminal = _record_claude_characterization_payload(
                store=store,
                payload=active_payload,
                marker_secret=marker_secret,
                cleanup_completed=False,
                ensure_registered=False,
                launch_aborted=True,
            )
            if terminal.get("status") != "already_aborted":
                raise RolloutGateBlocked("characterization_status_identity_missing")
            retire_aborted_claude_visibility_characterization(
                source_root=source_root,
                marker_secret=marker_secret,
                expected_operation_id=str(active_payload["operation_id"]),
                expected_job_id=str(active_payload["job_id"]),
                expected_reserved_claude_uuid=str(
                    active_payload["reserved_claude_uuid"]
                ),
            )
            return {
                "status": "aborted_exact_absence",
                "job_id": active_payload["job_id"],
                "reserved_claude_uuid": active_payload["reserved_claude_uuid"],
                "replacement_created": False,
                "active_record_retired": True,
                "replayed": True,
            }
        active_state = active_matches[0].get("state") if active_matches else None
        active_phase = (
            active_payload.get("phase") if active_payload is not None else None
        )
        local_visible_recovery = active_state == "claude_visible" and active_phase in {
            "launching",
            "launched",
            "ready",
        }
        local_exact_reconciliation = active_state in {
            "claude_pending",
            "claude_leased",
            "claude_retry",
        } and active_phase in {"launched", "ready"}
        local_recovery = local_visible_recovery or local_exact_reconciliation
        if not local_recovery and startup is None:
            claude_command = resolve_cli_executable("claude")
            preflight = _claude_visibility_preflight_detail(claude_command)
            if preflight.startup is None:
                raise ProviderDegraded(cast(str, preflight.failure_code))
            startup = preflight.startup
        registrar: Any = None
        if local_exact_reconciliation:
            source = ClaudeSourceAdapter(
                _CLAUDE_PROJECTS_ROOT, marker_secret=marker_secret
            )
            registrar = ClaudeNativeRegistrar(
                store,
                source,
                marker_secret=marker_secret,
                startup_theme="light",
                claude_command=(),
                process_timeout=self.config.claude_visibility.process_timeout_seconds,
                discovery_timeout=self.config.claude_visibility.discovery_timeout_seconds,
            )
        elif not local_visible_recovery:
            assert startup is not None
            source = ClaudeSourceAdapter(
                _CLAUDE_PROJECTS_ROOT, marker_secret=marker_secret
            )
            registrar = ClaudeNativeRegistrar(
                store,
                source,
                marker_secret=marker_secret,
                startup_theme=startup["theme"],
                claude_command=claude_command,
                process_timeout=self.config.claude_visibility.process_timeout_seconds,
                discovery_timeout=self.config.claude_visibility.discovery_timeout_seconds,
            )
        policy = self.config.claude_visibility

        def _reserve(projection: Any) -> Any:
            candidate = build_claude_visibility_candidate(
                projection, eligible_at=float(projection.last_active)
            )
            identity = derive_claude_visibility_identity(candidate, marker_secret)
            try:
                prepared = _read_characterization_record(
                    source_root / ".claude-visibility-operation.json",
                    marker_secret,
                )
            except RuntimeError:
                raise RolloutGateBlocked("characterization_record_invalid") from None
            operation_id = prepared.get("operation_id")
            if (
                not isinstance(operation_id, str)
                or candidate.source_session_id != f"codex:{operation_id}"
                or prepared.get("source_provider") != Provider.CODEX.value
                or prepared.get("source_cwd") != candidate.source_cwd
                or prepared.get("source_session_id") != candidate.source_session_id
                or prepared.get("bridge_id") != identity.bridge_id
                or prepared.get("job_id") != identity.job_id
                or prepared.get("reserved_claude_uuid") != identity.claude_uuid
                or prepared.get("native_name") != candidate.native_name
                or prepared.get("signed_marker") != identity.signed_marker
                or prepared.get("phase")
                not in {"prepared", "reserved", "launching", "launched", "ready"}
            ):
                raise RolloutGateBlocked("characterization_record_invalid")
            try:
                store.enqueue_claude_visibility_characterization(
                    candidate,
                    identity,
                    marker_secret,
                    operation_id=operation_id,
                    evidence_digest=_claude_characterization_evidence(prepared),
                )
            except (TypeError, ValueError):
                raise RolloutGateBlocked("characterization_record_invalid") from None
            claim = store.claim_claude_visibility_job(
                time.time(),
                policy.lease_seconds,
                policy.daily_registration_limit,
                policy.emergency_daily_cost_usd,
                policy.reserved_cost_per_attempt_usd,
                policy.max_attempts,
                expected_job_id=identity.job_id,
            )
            if claim.job_id != identity.job_id:
                raise RolloutGateBlocked("characterization_claim_mismatch")
            return claim

        def _reconcile_existing(projection: Any) -> Any:
            candidate = build_claude_visibility_candidate(
                projection, eligible_at=float(projection.last_active)
            )
            identity = derive_claude_visibility_identity(candidate, marker_secret)
            claim = store.claim_claude_visibility_reconciliation(
                time.time(),
                policy.lease_seconds,
                expected_job_id=identity.job_id,
            )
            if claim.job_id != identity.job_id:
                raise RolloutGateBlocked("characterization_claim_mismatch")
            return claim

        def _registration_is_visible(operation: Mapping[str, Any]) -> bool:
            job_id = operation.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                raise RolloutGateBlocked("characterization_record_invalid")
            current = store.claude_visibility_status(time.time())
            characterizations = current.get("characterizations")
            if not isinstance(characterizations, list):
                raise RolloutGateBlocked("characterization_record_invalid")
            matches = [
                item
                for item in characterizations
                if isinstance(item, Mapping) and item.get("job_id") == job_id
            ]
            if not matches:
                return False
            if len(matches) != 1:
                raise RolloutGateBlocked("characterization_registration_not_visible")
            state = matches[0].get("state")
            if state == "claude_visible":
                return True
            if state in {"claude_pending", "claude_leased", "claude_retry"}:
                return False
            raise RolloutGateBlocked("characterization_registration_not_visible")

        def _recover_auth_failure(
            operation: Mapping[str, Any], evidence_digest: str, prompt: str
        ) -> Mapping[str, Any]:
            if registrar is None:
                raise RolloutGateBlocked("characterization_registration_not_visible")
            job_id = operation.get("job_id")
            reserved_uuid = operation.get("reserved_claude_uuid")
            operation_id = operation.get("operation_id")
            if any(
                not isinstance(value, str) or not value
                for value in (job_id, reserved_uuid, operation_id)
            ):
                raise RolloutGateBlocked("characterization_recovery_identity_invalid")
            recovery = store.claim_claude_auth_recovery(
                job_id=str(job_id),
                reserved_claude_uuid=str(reserved_uuid),
                operation_id=str(operation_id),
                evidence_digest=evidence_digest,
                prompt_digest=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                now=time.time(),
                lease_seconds=policy.lease_seconds,
                daily_limit=policy.daily_registration_limit,
                cost_limit=policy.emergency_daily_cost_usd,
                reserved_cost=policy.reserved_cost_per_attempt_usd,
                max_attempts=policy.max_attempts,
            )
            if recovery.get("status") != "claimed":
                raise RolloutGateBlocked("characterization_recovery_not_available")
            outcome = registrar.resume_auth_recovery(recovery, prompt)
            if outcome.status != "recovered":
                raise ProviderDegraded(
                    outcome.error_code or "characterization_recovery_failed"
                )
            return {
                **recovery,
                "status": "recovered",
                "job_id": outcome.job_id,
                "reserved_claude_uuid": outcome.reserved_claude_uuid,
            }

        def _complete_auth_recovery(
            recovery: Mapping[str, Any], transcript_digest: str
        ) -> None:
            store.commit_claude_auth_recovery(
                job_id=str(recovery["job_id"]),
                lease_digest=str(recovery["lease_digest"]),
                reserved_claude_uuid=str(recovery["reserved_claude_uuid"]),
                transcript_digest=transcript_digest,
                visible_at=time.time(),
            )

        def _reconcile_auth_recovery(
            operation: Mapping[str, Any],
            evidence_digest: str,
            prompt_digest: str,
            transcript_digest: str,
        ) -> None:
            store.reconcile_claude_auth_recovery(
                job_id=str(operation["job_id"]),
                reserved_claude_uuid=str(operation["reserved_claude_uuid"]),
                operation_id=str(operation["operation_id"]),
                evidence_digest=evidence_digest,
                prompt_digest=prompt_digest,
                transcript_digest=transcript_digest,
                visible_at=time.time(),
            )

        def _reject_local_relaunch(_projection: Any) -> Any:
            raise RolloutGateBlocked("characterization_relaunch_requires_provider")

        return characterize_claude_visibility(
            source_root=source_root,
            projects_root=_CLAUDE_PROJECTS_ROOT,
            reserve=(
                _reject_local_relaunch if local_exact_reconciliation else _reserve
            ),
            reconcile_existing=_reconcile_existing,
            registration_is_visible=_registration_is_visible,
            registrar=registrar,
            restarted_source=lambda: ClaudeSourceAdapter(
                _CLAUDE_PROJECTS_ROOT, marker_secret=marker_secret
            ),
            marker_secret=marker_secret,
            recover_auth_failure=_recover_auth_failure,
            complete_auth_recovery=_complete_auth_recovery,
            reconcile_auth_recovery=_reconcile_auth_recovery,
        )

    def _claude_visibility_runtime(self) -> ClaudeVisibilityCoordinator:
        try:
            claude_command = resolve_cli_executable("claude")
        except ConfigurationFailure:
            raise
        except Exception as exc:
            raise ProviderDegraded("claude_visibility_runtime_unavailable") from exc
        local = _claude_visibility_local_preflight_detail()
        if local.startup is None:
            raise ProviderDegraded(cast(str, local.failure_code))
        now = time.monotonic()
        command_identity = tuple(claude_command)
        cached_at = self._claude_visibility_preflight_at
        command_evidence_is_fresh = (
            self._claude_visibility_preflight_command == command_identity
            and cached_at is not None
            and now - cached_at < _CLAUDE_VISIBILITY_PREFLIGHT_TTL_SECONDS
        )
        if command_evidence_is_fresh:
            startup = cast(dict[str, str], local.startup)
        else:
            preflight = _claude_visibility_preflight_detail(claude_command)
            if preflight.startup is None:
                raise ProviderDegraded(cast(str, preflight.failure_code))
            startup = preflight.startup
            self._claude_visibility_preflight_command = command_identity
            self._claude_visibility_preflight_at = now
        startup_identity = (tuple(claude_command), startup["theme"])
        if (
            self._claude_visibility_coordinator is not None
            and self._claude_visibility_startup_identity == startup_identity
        ):
            return self._claude_visibility_coordinator
        try:
            marker_secret = resolve_marker_key()
            source = ClaudeSourceAdapter(
                _CLAUDE_PROJECTS_ROOT, marker_secret=marker_secret
            )
            store = self._require_store()
            registrar = ClaudeNativeRegistrar(
                store,
                source,
                marker_secret=marker_secret,
                startup_theme=startup["theme"],
                claude_command=claude_command,
                process_timeout=self.config.claude_visibility.process_timeout_seconds,
                discovery_timeout=self.config.claude_visibility.discovery_timeout_seconds,
            )

            coordinator = ClaudeVisibilityCoordinator(
                config=self.config,
                store=store,
                inventory=lambda after, **_kwargs: self._claude_visibility_inventory(
                    after,
                    marker_secret=marker_secret,
                    state_db_only=True,
                ),
                continuous_inventory=lambda after, **kwargs: self._claude_visibility_inventory(
                    after,
                    marker_secret=marker_secret,
                    state_db_only=True,
                    stop=kwargs.get("stop"),
                ),
                registrar=registrar,
                marker_secret=marker_secret,
                clock=time.time,
            )
        except ConfigurationFailure:
            raise
        except Exception as exc:
            raise ProviderDegraded("claude_visibility_runtime_unavailable") from exc
        self._claude_visibility_coordinator = coordinator
        self._claude_visibility_startup_identity = startup_identity
        return coordinator

    def _claude_visibility_inventory(
        self,
        after: float,
        *,
        marker_secret: bytes,
        state_db_only: bool = False,
        stop: Any = None,
    ) -> Sequence[SidebarSource]:
        def cancelled() -> None:
            if stop is not None and stop.is_set():
                raise _VisibilityCycleCancelled()

        cancelled()
        store = self._require_store()
        sources = list(store.list_claude_visibility_hermes_sources(after, None))
        cancelled()
        indexed_codex = store.list_claude_visibility_codex_sources(after, None)
        cancelled()
        indexed_by_native_id: dict[str, SidebarSource] = {}
        for source in indexed_codex:
            native_id = source.projection.native_id
            if (
                source.projection.provider is not Provider.CODEX
                or source.source_session_id
                != canonical_session_id(Provider.CODEX, native_id)
                or native_id in indexed_by_native_id
            ):
                raise ValueError("conflicting indexed Codex visibility source")
            indexed_by_native_id[native_id] = source
        known_visibility_source_ids = store.list_claude_visibility_source_ids()
        if self._codex_client is None:
            codex_command = resolve_cli_executable("codex")
            if len(codex_command) != 1:
                raise RuntimeError("codex_direct_runtime_required")
            self._codex_client = RecoveringCodexAppServerClient(
                lambda: CodexAppServerClient(codex_bin=codex_command[0]),
                cancel_event=self._claude_visibility_stop,
            )
        codex = self._claude_visibility_codex_adapter
        if codex is None:
            codex = CodexSourceAdapter(
                self._codex_client,
                marker_secret=marker_secret,
                trusted_origins=lambda: load_codex_characterization_origins(
                    marker_secret=marker_secret
                ),
            )
            self._claude_visibility_codex_adapter = codex
        try:
            page = codex.list_claude_visibility_sources(
                after=after,
                state_db_only=state_db_only,
                indexed_sources=indexed_by_native_id,
                known_visibility_source_ids=known_visibility_source_ids,
                discovery_timeout=(
                    self.config.claude_visibility.discovery_timeout_seconds
                ),
                stop=stop,
            )
        except _VisibilityInventoryCancelled:
            raise _VisibilityCycleCancelled() from None
        cancelled()
        existing = {
            (item.projection.provider, item.source_session_id) for item in sources
        }
        for source in page:
            key = (source.projection.provider, source.source_session_id)
            if key in existing:
                continue
            sources.append(source)
            existing.add(key)
        sources.sort(
            key=lambda item: (
                -float(item.projection.last_active),
                item.source_session_id,
                item.projection.provider.value,
            )
        )
        return tuple(sources)

    def characterize(self, *, provider: str) -> Mapping[str, Any]:
        selected = _CHARACTERIZATION_PROVIDER_SELECTIONS.get(provider)
        if selected is None:
            raise ConfigurationFailure("characterization_provider_invalid")
        try:
            marker_key = resolve_marker_key()
            report_path = run_live_characterization(
                claude_projects_root=_CLAUDE_PROJECTS_ROOT,
                provenance_secret=marker_key,
                live_tests_enabled=True,
                providers=selected,
            )
            gate = resolve_characterization_gate()
        except LiveCharacterizationError as exc:
            raise ProviderDegraded("characterization_failed") from exc
        except CharacterizationGateError as exc:
            raise ProviderDegraded(f"characterization_{exc.code}") from exc
        except Exception as exc:
            raise ProviderDegraded("characterization_failed") from exc
        return {
            "passed": True,
            "report": report_path.name,
            "characterization_id": gate.characterization_id,
            "codex_registration_turn_required": (gate.codex_registration_turn_required),
        }

    def characterization_status(self) -> str:
        try:
            resolve_characterization_gate()
        except CharacterizationGateError as exc:
            return exc.code
        except Exception:
            return "invalid"
        return "passed"

    def backfill_candidates(self, *, days: int) -> list[dict[str, Any]]:
        if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
            raise ConfigurationFailure("invalid_backfill_days")
        store = self._require_store()
        now = time.time()
        after = now - days * 24 * 60 * 60
        policy = replace(self._policy(), backfill_days=days)
        planned: list[dict[str, Any]] = []
        catalog = self._require_catalog()
        cursor: tuple[float, str] | None = None
        examined = 0
        try:
            while True:
                remaining = _MAX_PLANNED_SESSIONS - examined
                if remaining <= 0:
                    raise ProviderDegraded("backfill_plan_truncated")
                projections = store.list_native_projections(
                    after=after,
                    limit=min(_BACKFILL_PAGE_SIZE, remaining),
                    cursor=cursor,
                )
                source_ids = [
                    f"{projection.provider.value}:{projection.native_id}"
                    for projection in projections
                ]
                mappings = store.list_existing_target_mappings(source_ids)
                context = EligibilityContext(
                    now=now,
                    discovery_mode=DiscoveryMode.INITIAL_BACKFILL,
                    continuous_watermark=None,
                    existing_target_mappings=frozenset(mappings),
                    policy=policy,
                )
                for projection in projections:
                    eligibility = classify_mirror_eligibility(projection, context)
                    if not eligibility.eligible:
                        continue
                    canonical_id = f"{projection.provider.value}:{projection.native_id}"
                    preview = catalog.mirror_preview(
                        canonical_id, eligibility.target_provider.value
                    )
                    if preview.get("would_enqueue") is not True:
                        continue
                    planned.append({
                        "canonical_id": canonical_id,
                        "provider": projection.provider.value,
                        "target_provider": eligibility.target_provider.value,
                        "last_active": float(projection.last_active),
                        "eligible": True,
                        "reason": eligibility.reason,
                    })
                examined += len(projections)
                if not projections.has_more:
                    break
                if projections.next_cursor is None:
                    raise ProviderDegraded("backfill_plan_cursor_missing")
                if examined >= _MAX_PLANNED_SESSIONS:
                    raise ProviderDegraded("backfill_plan_truncated")
                cursor = projections.next_cursor
        except ProviderDegraded:
            raise
        except Exception as exc:
            raise ProviderDegraded("backfill_plan_failed") from exc
        return _ordered_candidates(planned)

    def apply_backfill(self, *, candidates: list[dict[str, Any]]) -> Mapping[str, Any]:
        self._mutation_preflight()
        store = self._require_store()
        policy = self._policy()
        self._require_open_breaker(store, policy)
        totals = {
            "authorized": 0,
            "claimed": 0,
            "succeeded": 0,
            "retried": 0,
            "manual_failure": 0,
        }
        for candidate in _ordered_candidates(candidates):
            try:
                job = enqueue_mirror_job(
                    store,
                    candidate["canonical_id"],
                    Provider(candidate["target_provider"]),
                    policy=policy,
                    manual_authorized=True,
                    require_unmapped=True,
                    rollout_limited=True,
                )
            except PermissionError as exc:
                if totals["authorized"]:
                    return {
                        **totals,
                        "degraded": False,
                        "halted": True,
                        "partial": True,
                        "gate": "backfill_authority_revoked",
                    }
                raise RolloutGateBlocked("backfill_authority_revoked") from exc
            except (KeyError, TypeError, ValueError) as exc:
                if totals["authorized"]:
                    return {
                        **totals,
                        "degraded": False,
                        "halted": True,
                        "partial": True,
                        "gate": "backfill_candidate_invalid",
                    }
                raise RolloutGateBlocked("backfill_candidate_invalid") from exc
            totals["authorized"] += 1
            try:
                coordinator = self._provider_runtime(
                    targets=True,
                    catalog_only=False,
                    providers=(Provider.CLAUDE, Provider.CODEX),
                )
            except Exception:
                return {
                    **totals,
                    "degraded": True,
                    "halted": False,
                    "provider_available": False,
                }
            summary = asyncio.run(
                coordinator.process_jobs_once(job_ids=(job["id"],), limit=1)
            )
            for key in ("claimed", "succeeded", "retried", "manual_failure"):
                totals[key] += int(getattr(summary, key))
            if summary.claimed == 0 or summary.retried or summary.manual_failure:
                break
        progress = store.get_mirror_breaker_progress()
        halted = should_halt_batch(
            BatchProgress(
                attempts=int(progress.get("attempts", 0)),
                errors=int(progress.get("errors", 0)),
            ),
            policy,
        )
        return {
            **totals,
            "degraded": bool(totals["retried"] or totals["manual_failure"]),
            "halted": halted,
        }

    def mirror_preview(self, *, session_id: str, target: str) -> Mapping[str, Any]:
        try:
            return self._require_catalog().mirror_preview(session_id, target)
        except (KeyError, TypeError, ValueError) as exc:
            raise RolloutGateBlocked("mirror_invalid") from exc

    def apply_mirror(self, *, session_id: str, target: str) -> Mapping[str, Any]:
        self._mutation_preflight()
        store = self._require_store()
        policy = self._policy()
        self._require_open_breaker(store, policy)
        try:
            job = enqueue_mirror_job(
                store,
                session_id,
                Provider(target),
                policy=policy,
                manual_authorized=True,
                require_unmapped=True,
                rollout_limited=True,
            )
        except PermissionError as exc:
            raise RolloutGateBlocked("mirror_authority_revoked") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise RolloutGateBlocked("mirror_invalid") from exc
        try:
            coordinator = self._provider_runtime(
                targets=True,
                catalog_only=False,
                providers=(Provider.CLAUDE, Provider.CODEX),
            )
        except Exception:
            return {
                "session_id": session_id,
                "target_provider": target,
                "state": job.get("state"),
                "claimed": 0,
                "succeeded": 0,
                "retried": 0,
                "manual_failure": 0,
                "degraded": True,
                "provider_available": False,
            }
        summary = asyncio.run(
            coordinator.process_jobs_once(job_ids=(job["id"],), limit=1)
        )
        durable = next(
            (
                row
                for row in store.list_mirror_jobs(list(MirrorJobState), limit=1000)
                if row.get("id") == job["id"]
            ),
            job,
        )
        return {
            "session_id": session_id,
            "target_provider": target,
            "state": durable.get("state", job.get("state")),
            "claimed": summary.claimed,
            "succeeded": summary.succeeded,
            "retried": summary.retried,
            "manual_failure": summary.manual_failure,
            "degraded": bool(summary.retried or summary.manual_failure),
        }

    def _require_catalog(self) -> UnifiedCatalog:
        if self._catalog is None:
            self._db = SessionDB(db_path=self._db_path)
            self._store = SessionBridgeStore(self._db)
            self._catalog = UnifiedCatalog(self._db, self._store)
        return self._catalog

    def _require_store(self) -> SessionBridgeStore:
        self._require_catalog()
        assert self._store is not None
        return self._store

    def _provider_runtime(
        self,
        *,
        targets: bool,
        catalog_only: bool,
        providers: Sequence[Provider],
    ) -> SessionBridgeCoordinator:
        if self._coordinator is not None:
            return self._coordinator
        selected = tuple(dict.fromkeys(Provider(provider) for provider in providers))
        if not selected or any(
            provider not in (Provider.CLAUDE, Provider.CODEX) for provider in selected
        ):
            raise ConfigurationFailure("provider_selection_invalid")
        try:
            try:
                marker_key = resolve_marker_key()
            except (OSError, PermissionError, RuntimeError, ValueError) as exc:
                raise ConfigurationFailure("marker_key_unavailable") from exc
            source_adapters: dict[Provider, object] = {}
            claude_source: ClaudeSourceAdapter | None = None
            codex_source: CodexSourceAdapter | None = None
            if Provider.CLAUDE in selected:
                claude_source = ClaudeSourceAdapter(
                    _CLAUDE_PROJECTS_ROOT, marker_secret=marker_key
                )
                source_adapters[Provider.CLAUDE] = claude_source
            if Provider.CODEX in selected:
                codex_command = resolve_cli_executable("codex")
                if len(codex_command) != 1:
                    raise RuntimeError("codex_direct_runtime_required")
                if self._codex_client is None:
                    self._codex_client = RecoveringCodexAppServerClient(
                        lambda: CodexAppServerClient(codex_bin=codex_command[0])
                    )
                codex_source = CodexSourceAdapter(
                    self._codex_client,
                    marker_secret=marker_key,
                    trusted_origins=lambda: load_codex_characterization_origins(
                        marker_secret=marker_key
                    ),
                )
                source_adapters[Provider.CODEX] = codex_source
            target_adapters: dict[Provider, object] = {}
            if targets:
                if claude_source is not None:
                    target_adapters[Provider.CLAUDE] = ClaudeTargetAdapter(
                        claude_source, marker_secret=marker_key
                    )
                if codex_source is not None and self._codex_client is not None:
                    target_adapters[Provider.CODEX] = CodexTargetAdapter(
                        self._codex_client,
                        source_adapter=codex_source,
                        marker_secret=marker_key,
                    )
            effective_config = self.config
            if catalog_only:
                effective_config = replace(
                    self.config,
                    mirrors=replace(
                        self.config.mirrors,
                        automatic_creation=False,
                    ),
                )
            catalog = self._require_catalog()
            sidebar_verifier = (
                SidebarThreadVerifier(
                    codex_source,
                    marker_secret=marker_key,
                    reconciliation_interval=effective_config.service.reconcile_seconds,
                )
                if codex_source is not None
                else None
            )
            sidebar_executor = None
            mirror_float = (
                ClaudeMirrorFloatWorker(
                    self._require_store(),
                    registry_roots=discover_ccd_registry_roots(),
                )
                if (
                    not catalog_only
                    and effective_config.claude_visibility.enabled
                    and effective_config.claude_visibility.float_activity
                )
                else None
            )
            idle_chip_archiver = (
                IdleChipArchiveWorker(
                    registry_roots=discover_ccd_registry_roots(),
                    idle_seconds=float(
                        effective_config.claude_visibility.idle_chip_archive_seconds
                    ),
                )
                if (
                    not catalog_only
                    and effective_config.claude_visibility.enabled
                    and effective_config.claude_visibility.archive_idle_chips
                )
                else None
            )
            self._coordinator = SessionBridgeCoordinator(
                config=effective_config,
                store=self._require_store(),
                adapters=source_adapters,
                target_adapters=target_adapters,
                context_builder=(
                    ContextPackBuilder(catalog.db, catalog.store) if targets else None
                ),
                claude_projects_root=(
                    _CLAUDE_PROJECTS_ROOT if Provider.CLAUDE in selected else None
                ),
                permission_preflight=_production_codex_permission_preflight,
                sidebar_verifier=sidebar_verifier,
                sidebar_executor=sidebar_executor,
                mirror_float=mirror_float,
                idle_chip_archiver=idle_chip_archiver,
            )
            return self._coordinator
        except Exception:
            self.close()
            raise

    def _require_sidebar_executor(self) -> SidebarExecutor:
        raise RolloutGateBlocked("desktop_broker_required")

    def _sidebar_registration_runtime_args(self, *, codex_bin: str) -> list[str]:
        """Resolve every configured MCP name before launching the lean runtime."""

        if self._sidebar_codex_client is None:
            self._sidebar_codex_client = CodexAppServerClient(codex_bin=codex_bin)
        client = self._sidebar_codex_client
        if not bool(getattr(client, "_initialized", False)):
            client.initialize(
                capabilities={"experimentalApi": True},
                timeout=30.0,
            )
        response = client.request(
            "config/read",
            {"cwd": str(Path.cwd()), "includeLayers": False},
            timeout=30.0,
        )
        return sidebar_registration_app_server_args(
            configured_mcp_server_names(response)
        )

    def _require_sidebar_hydration_executor(self) -> SidebarHydrationExecutor:
        if self._sidebar_hydration_executor is not None:
            return self._sidebar_hydration_executor
        try:
            marker_key = resolve_marker_key()
            store = self._require_store()
            coordinator = SessionBridgeCoordinator(
                config=self.config,
                store=store,
                adapters={},
                target_adapters={},
            )
            native = self._require_sidebar_terminal_delivery()

            def _claim_once():
                return asyncio.run(
                    coordinator.claim_sidebar_hydration_for_delivery(limit=1)
                )

            self._sidebar_hydration_executor = SidebarHydrationExecutor(
                claim_once=_claim_once,
                store=store,
                native=native,
                marker_secret=marker_key,
            )
            return self._sidebar_hydration_executor
        except ConfigurationFailure:
            raise
        except Exception as exc:
            self.close()
            raise ConfigurationFailure(
                "sidebar_hydration_executor_unavailable"
            ) from exc

    def _require_sidebar_terminal_delivery(self) -> CodexAppServerSidebarDelivery:
        """Build the narrow read/resume-only provider boundary for terminal proof."""

        try:
            if self._sidebar_codex_client is None:
                codex_command = resolve_cli_executable("codex")
                if len(codex_command) != 1:
                    raise RuntimeError("codex_direct_runtime_required")
                self._sidebar_codex_client = CodexAppServerClient(
                    codex_bin=codex_command[0]
                )
            return CodexAppServerSidebarDelivery(cast(Any, self._sidebar_codex_client))
        except ConfigurationFailure:
            raise
        except Exception as exc:
            raise ConfigurationFailure("sidebar_terminal_probe_unavailable") from exc

    def _require_sidebar_terminal_verifier(
        self,
        *,
        marker_secret: bytes,
    ) -> SidebarThreadVerifier:
        """Build a fresh read-only inventory verifier for precreate proof."""

        try:
            if type(marker_secret) is not bytes or not marker_secret:
                raise ValueError("marker key is unavailable")
            codex_command = resolve_cli_executable("codex")
            if len(codex_command) != 1:
                raise RuntimeError("codex_direct_runtime_required")
            if self._sidebar_codex_client is None:
                self._sidebar_codex_client = CodexAppServerClient(
                    codex_bin=codex_command[0]
                )
            source = CodexSourceAdapter(
                self._sidebar_codex_client,
                marker_secret=marker_secret,
            )
            return SidebarThreadVerifier(
                source,
                marker_secret=marker_secret,
                reconciliation_interval=0.0,
            )
        except ConfigurationFailure:
            raise
        except Exception as exc:
            raise ConfigurationFailure("sidebar_precreate_probe_unavailable") from exc

    def _release_provider_runtime(self) -> None:
        client, self._codex_client = self._codex_client, None
        self._coordinator = None
        if client is not None:
            client.close()

    def _policy(self) -> MirrorPolicy:
        mirrors = self.config.mirrors
        return MirrorPolicy(
            automatic_creation=mirrors.automatic_creation,
            backfill_days=mirrors.backfill_days,
            creates_per_minute=mirrors.creates_per_minute,
            max_attempts=mirrors.max_attempts,
            stop_after_attempts=mirrors.stop_after_attempts,
            stop_error_rate=mirrors.stop_error_rate,
        )

    def _mutation_preflight(self) -> None:
        try:
            resolve_marker_key()
            codex_command = resolve_cli_executable("codex")
            claude_command = resolve_cli_executable("claude")
        except (OSError, PermissionError, RuntimeError, ValueError) as exc:
            raise ConfigurationFailure("mutation_preflight_failed") from exc
        if len(codex_command) != 1 or not claude_command:
            raise ConfigurationFailure("mutation_preflight_failed")

    @staticmethod
    def _require_open_breaker(store: SessionBridgeStore, policy: MirrorPolicy) -> None:
        progress = store.get_mirror_breaker_progress()
        attempts = int(progress.get("attempts", 0))
        errors = int(progress.get("errors", 0))
        if errors > 0 and attempts > 0 and errors / attempts >= policy.stop_error_rate:
            raise RolloutGateBlocked("rollout_breaker_halted")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-session-bridge",
        description="Unified Claude Code and Codex session catalog control plane.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "install-sidebar-skill",
        help="install the personal Codex sidebar delivery skill",
    )
    commands.add_parser(
        "install-claude-skill",
        help="install the personal Claude unified catalog skill",
    )
    serve = commands.add_parser("serve", help="serve the authenticated loopback MCP")
    serve.add_argument("--config-home", type=Path, metavar="PATH")
    serve.add_argument("--state-db", type=Path, metavar="PATH")

    scan = commands.add_parser("scan", help="import provider history into the catalog")
    scan.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    scan.add_argument(
        "--all-history",
        action="store_true",
        default=True,
        help="force a catalog-only full-history scan (default)",
    )

    status = commands.add_parser("status", help="show sanitized local bridge status")
    status.add_argument("--json", action="store_true")

    sidebar_status = commands.add_parser(
        "sidebar-status",
        help="show sanitized native sidebar delivery status",
    )
    sidebar_status.add_argument("--json", action="store_true")

    sidebar_broker_configure = commands.add_parser(
        "sidebar-broker-configure",
        help="persist the exact Desktop sidebar broker identity",
    )
    sidebar_broker_configure.add_argument("--thread-id", required=True)
    sidebar_broker_configure.add_argument("--project-id", required=True)
    sidebar_broker_configure.add_argument("--cwd", required=True)
    sidebar_broker_configure.add_argument("--inbox-cwd", required=True)

    sidebar_backfill = commands.add_parser(
        "sidebar-backfill",
        help="preview or enqueue a bounded native sidebar batch",
    )
    sidebar_backfill_window = sidebar_backfill.add_mutually_exclusive_group(
        required=True
    )
    sidebar_backfill_window.add_argument("--days", type=_bounded_sidebar_days)
    sidebar_backfill_window.add_argument("--all-history", action="store_true")
    sidebar_backfill.add_argument("--limit", type=_bounded_sidebar_limit, default=10)
    sidebar_backfill_mode = sidebar_backfill.add_mutually_exclusive_group(required=True)
    sidebar_backfill_mode.add_argument("--dry-run", action="store_true")
    sidebar_backfill_mode.add_argument("--apply", action="store_true")

    sidebar_continuous = commands.add_parser(
        "sidebar-continuous",
        help="persist the native sidebar continuous registration mode",
    )
    sidebar_continuous_mode = sidebar_continuous.add_mutually_exclusive_group(
        required=True
    )
    sidebar_continuous_mode.add_argument("--enable", action="store_true")
    sidebar_continuous_mode.add_argument("--disable", action="store_true")

    sidebar_readable = commands.add_parser(
        "sidebar-readable-preview",
        help="persist readable previews for future sidebar registrations",
    )
    sidebar_readable_mode = sidebar_readable.add_mutually_exclusive_group(required=True)
    sidebar_readable_mode.add_argument("--enable", action="store_true")
    sidebar_readable_mode.add_argument("--disable", action="store_true")

    sidebar_hydration = commands.add_parser(
        "sidebar-hydration",
        help="persist the exact-task legacy hydration gate",
    )
    sidebar_hydration_mode = sidebar_hydration.add_mutually_exclusive_group(
        required=True
    )
    sidebar_hydration_mode.add_argument("--enable", action="store_true")
    sidebar_hydration_mode.add_argument("--disable", action="store_true")

    sidebar_hydration_seed = commands.add_parser(
        "sidebar-hydration-seed",
        help="seed one exact existing linked task for in-place hydration",
    )
    sidebar_hydration_seed.add_argument("--source-session-id", required=True)
    sidebar_hydration_seed.add_argument(
        "--codex-thread-id",
        type=_sidebar_terminal_thread_id,
        required=True,
    )
    sidebar_hydration_seed.add_argument(
        "--confirm",
        choices=("HYDRATE_EXACT_EXISTING_TASK",),
        required=True,
    )

    sidebar_hydration_seed_backfill = commands.add_parser(
        "sidebar-hydration-seed-backfill",
        help="inventory or seed exact legacy tasks for in-place hydration",
    )
    sidebar_hydration_seed_backfill_window = (
        sidebar_hydration_seed_backfill.add_mutually_exclusive_group(required=True)
    )
    sidebar_hydration_seed_backfill_window.add_argument(
        "--days",
        type=_bounded_sidebar_days,
    )
    sidebar_hydration_seed_backfill_window.add_argument(
        "--all-history",
        action="store_true",
    )
    sidebar_hydration_seed_backfill.add_argument(
        "--limit",
        type=_bounded_sidebar_hydration_limit,
        default=10,
    )
    sidebar_hydration_seed_backfill_mode = (
        sidebar_hydration_seed_backfill.add_mutually_exclusive_group()
    )
    sidebar_hydration_seed_backfill_mode.add_argument(
        "--dry-run",
        action="store_true",
    )
    sidebar_hydration_seed_backfill_mode.add_argument(
        "--apply",
        action="store_true",
    )
    sidebar_hydration_seed_backfill.add_argument(
        "--confirm",
        choices=("HYDRATE_ALL_EXACT_EXISTING_TASKS",),
    )

    sidebar_hydration_status = commands.add_parser(
        "sidebar-hydration-status",
        help="show sanitized in-place hydration status",
    )
    sidebar_hydration_status.add_argument("--json", action="store_true")

    commands.add_parser(
        "sidebar-run-once",
        help="diagnostic only; delivery is owned by the pinned Codex Desktop broker",
    )

    sidebar_retry_bound = commands.add_parser(
        "sidebar-retry-bound",
        help="requeue one exact failed bound sidebar task without replacement",
    )
    sidebar_retry_bound.add_argument(
        "--job-id", type=_sidebar_terminal_job_id, required=True
    )
    sidebar_retry_bound.add_argument("--source-session-id", required=True)
    sidebar_retry_bound.add_argument(
        "--codex-thread-id",
        type=_sidebar_terminal_thread_id,
        required=True,
    )
    sidebar_retry_bound.add_argument(
        "--expected-error-code",
        choices=(
            "native_task_not_indexed",
            "codex_thread_conflict",
            "native_create_ambiguous",
            "marker_conflict",
            "bridge_temporarily_unavailable",
            "source_identity_mismatch",
        ),
        required=True,
    )
    sidebar_retry_bound.add_argument(
        "--confirm",
        choices=(
            SIDEBAR_BOUND_RETRY_CONFIRMATION,
            SIDEBAR_SOURCE_CWD_REPAIR_CONFIRMATION,
        ),
        required=True,
    )

    sidebar_terminal = commands.add_parser(
        "sidebar-acknowledge-unrecoverable",
        help="acknowledge one audited unrecoverable bound Codex thread",
    )
    sidebar_terminal.add_argument(
        "--job-id", type=_sidebar_terminal_job_id, required=True
    )
    sidebar_terminal.add_argument(
        "--codex-thread-id",
        type=_sidebar_terminal_thread_id,
        required=True,
    )
    sidebar_terminal.add_argument(
        "--expected-error-code",
        choices=("native_create_ambiguous",),
        required=True,
    )
    sidebar_terminal.add_argument(
        "--confirm",
        choices=("native-thread-unrecoverable",),
        required=True,
    )

    sidebar_precreate_terminal = commands.add_parser(
        "sidebar-acknowledge-precreate-unrecoverable",
        help="acknowledge one audited pre-cutover create with no native task",
    )
    sidebar_precreate_terminal.add_argument(
        "--job-id", type=_sidebar_terminal_job_id, required=True
    )
    sidebar_precreate_terminal.add_argument(
        "--expected-error-code",
        choices=("native_create_ambiguous",),
        required=True,
    )
    sidebar_precreate_terminal.add_argument(
        "--confirm",
        choices=("precutover-create-unrecoverable",),
        required=True,
    )

    sidebar_unbound_terminal = commands.add_parser(
        "sidebar-acknowledge-unbound-unrecoverable",
        help="acknowledge one audited post-dispatch create with no native task",
    )
    sidebar_unbound_terminal.add_argument(
        "--job-id", type=_sidebar_terminal_job_id, required=True
    )
    sidebar_unbound_terminal.add_argument(
        "--expected-error-code",
        choices=("native_create_ambiguous",),
        required=True,
    )
    sidebar_unbound_terminal.add_argument(
        "--confirm",
        choices=("unbound-create-unrecoverable",),
        required=True,
    )

    claude_visibility_status = commands.add_parser(
        "claude-visibility-status",
        help="show sanitized Claude native visibility status",
    )
    claude_visibility_status.add_argument("--json", action="store_true")

    claude_visibility_backfill = commands.add_parser(
        "claude-visibility-backfill",
        help="preview or enqueue a reviewed Claude visibility batch",
    )
    claude_visibility_backfill.add_argument("--days", type=_positive_int, default=30)
    claude_visibility_backfill.add_argument(
        "--limit", type=_bounded_claude_visibility_limit, default=10
    )
    claude_visibility_mode = claude_visibility_backfill.add_mutually_exclusive_group()
    claude_visibility_mode.add_argument("--dry-run", action="store_true")
    claude_visibility_mode.add_argument("--apply", action="store_true")

    claude_lineage = commands.add_parser(
        "claude-visibility-reconcile-lineage",
        help="inspect or repair bounded historical Claude catalog lineage",
    )
    claude_lineage.add_argument(
        "--limit", type=_bounded_claude_lineage_limit, default=25
    )
    claude_lineage.add_argument(
        "--cursor",
        type=_claude_lineage_cursor_argument,
        help="continue from an exact cursor emitted by the preceding page",
    )
    claude_lineage_mode = claude_lineage.add_mutually_exclusive_group(required=True)
    claude_lineage_mode.add_argument("--dry-run", action="store_true")
    claude_lineage_mode.add_argument("--apply", action="store_true")
    claude_lineage.add_argument(
        "--confirm-historical-repair",
        action="store_true",
        help="confirm the existing-row-only repair when --apply is selected",
    )

    claude_visibility_continuous = commands.add_parser(
        "claude-visibility-continuous",
        help="persist the Claude visibility continuous discovery preference",
    )
    claude_continuous_mode = claude_visibility_continuous.add_mutually_exclusive_group(
        required=True
    )
    claude_continuous_mode.add_argument("--enable", action="store_true")
    claude_continuous_mode.add_argument("--disable", action="store_true")

    commands.add_parser(
        "claude-visibility-run-once",
        help="process at most one reviewed Claude visibility job",
    )

    characterize_claude_visibility_parser = commands.add_parser(
        "characterize-claude-visibility",
        help="register and verify one disposable native Claude mirror",
    )
    characterize_claude_visibility_parser.add_argument("--json", action="store_true")
    characterize_claude_visibility_parser.add_argument("--cleanup-token")

    abort_claude_characterization = commands.add_parser(
        "claude-visibility-abort-characterization",
        help="retire one disposable Claude probe after exact-UUID absence proof",
    )
    abort_claude_characterization.add_argument(
        "--confirm-exact-absence",
        action="store_true",
        help="confirm terminal abort without creating a replacement session",
    )
    abort_claude_characterization.add_argument("--job-id", required=True)
    abort_claude_characterization.add_argument("--reserved-claude-uuid", required=True)

    repair_failed_claude_visibility = commands.add_parser(
        "claude-visibility-repair-failed",
        help="reconcile one exact terminal Claude visibility failure",
    )
    repair_failed_claude_visibility.add_argument("--job-id", required=True)
    repair_failed_claude_visibility.add_argument(
        "--reserved-claude-uuid", required=True
    )
    repair_failed_claude_visibility.add_argument(
        "--error-code", required=True, choices=("bridge_conflict",)
    )
    repair_failed_claude_visibility_mode = (
        repair_failed_claude_visibility.add_mutually_exclusive_group(required=True)
    )
    repair_failed_claude_visibility_mode.add_argument("--dry-run", action="store_true")
    repair_failed_claude_visibility_mode.add_argument(
        "--apply", action="store_true"
    )
    repair_failed_claude_visibility.add_argument(
        "--confirm-exact-terminal-repair",
        action="store_true",
        help="confirm native-only reconciliation when --apply is selected",
    )

    dismiss_claude_visibility = commands.add_parser(
        "claude-visibility-dismiss",
        help="acknowledge one terminally failed job so discovery can resume",
    )
    dismiss_claude_visibility.add_argument("--job-id", required=True)
    dismiss_claude_visibility.add_argument(
        "--error-code",
        required=True,
        help="the failure recorded on the row, restated to prove intent",
    )
    dismiss_claude_visibility.add_argument(
        "--confirm-terminal-failure",
        action="store_true",
        help="confirm the job is not worth another paid attempt",
    )

    characterize = commands.add_parser(
        "characterize", help="run the disposable live provider gate"
    )
    characterize.add_argument(
        "--provider",
        choices=tuple(_CHARACTERIZATION_PROVIDER_SELECTIONS),
        default="all",
        help=(
            "which provider to re-prove; a scoped refresh creates one real "
            "session instead of two"
        ),
    )

    backfill = commands.add_parser(
        "backfill", help="plan or apply a bounded recent mirror batch"
    )
    backfill.add_argument("--days", type=_positive_int, default=30)
    backfill_mode = backfill.add_mutually_exclusive_group(required=True)
    backfill_mode.add_argument("--dry-run", action="store_true")
    backfill_mode.add_argument("--apply", action="store_true")
    backfill.add_argument(
        "--max-create", type=_bounded_create_count, default=_MAX_BACKFILL_CREATE
    )
    backfill.add_argument("--confirm-one-shot", action="store_true")

    mirror = commands.add_parser("mirror", help="plan or apply one native mirror")
    mirror.add_argument("session_id")
    mirror.add_argument("--target", choices=("claude", "codex"), required=True)
    mirror_mode = mirror.add_mutually_exclusive_group(required=True)
    mirror_mode.add_argument("--dry-run", action="store_true")
    mirror_mode.add_argument("--apply", action="store_true")
    mirror.add_argument("--confirm-one-shot", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    config_loader: Callable[[], BridgeConfig] = BridgeConfig.load,
    backend_factory: Callable[[BridgeConfig], _Backend] = ProductionBackend,
) -> int:
    args = build_parser().parse_args(argv)
    config_home = getattr(args, "config_home", None)
    if args.command != "serve" or config_home is None:
        return _main_unscoped(
            argv,
            config_loader=config_loader,
            backend_factory=backend_factory,
        )
    scope_token = set_hermes_home_override(config_home)
    try:
        return _main_unscoped(
            argv,
            config_loader=config_loader,
            backend_factory=backend_factory,
        )
    finally:
        reset_hermes_home_override(scope_token)


def _main_unscoped(
    argv: Sequence[str] | None = None,
    *,
    config_loader: Callable[[], BridgeConfig] = BridgeConfig.load,
    backend_factory: Callable[[BridgeConfig], _Backend] = ProductionBackend,
) -> int:
    args = build_parser().parse_args(argv)
    config_home = getattr(args, "config_home", None)
    if args.command == "install-sidebar-skill":
        try:
            installed = install_sidebar_skill()
        except Exception:
            _emit({"error": "configuration_error"})
            return EXIT_CONFIG
        _emit({"status": "installed", "path": str(installed)})
        return EXIT_OK
    if args.command == "install-claude-skill":
        try:
            installed = install_claude_skill()
        except Exception:
            _emit({"error": "configuration_error"})
            return EXIT_CONFIG
        _emit({"status": "installed", "path": str(installed)})
        return EXIT_OK
    if args.command == "sidebar-run-once":
        _emit({"error": "desktop_broker_required"})
        return EXIT_DEGRADED
    backend: _Backend | None = None
    try:
        config = config_loader()
        if not isinstance(config, BridgeConfig):
            raise TypeError("config loader did not return BridgeConfig")
        state_db = getattr(args, "state_db", None)
        if args.command == "serve" and state_db is None and config_home is not None:
            state_db = config_home / "state.db"
        if args.command == "serve" and state_db is not None and backend_factory is ProductionBackend:
            backend = ProductionBackend(config, db_path=state_db)
        else:
            backend = backend_factory(config)
    except Exception:
        _emit({"error": "configuration_error"})
        return EXIT_CONFIG

    assert backend is not None

    try:
        if args.command == "serve":
            backend.serve()
            _emit({"status": "stopped"})
            return EXIT_OK
        if args.command == "scan":
            payload = dict(
                backend.scan(
                    provider=args.provider,
                    all_history=bool(args.all_history),
                    newest_first=True,
                )
            )
            _emit(payload)
            return EXIT_DEGRADED if int(payload.get("failed", 0)) else EXIT_OK
        if args.command == "status":
            payload = dict(backend.status())
            _emit(payload)
            return EXIT_OK if payload.get("healthy", True) is True else EXIT_DEGRADED
        if args.command == "sidebar-status":
            payload = dict(backend.sidebar_status())
            _emit(payload)
            return EXIT_OK if payload.get("healthy") is True else EXIT_DEGRADED
        if args.command == "sidebar-broker-configure":
            payload = dict(
                backend.configure_sidebar_broker(
                    thread_id=args.thread_id,
                    project_id=args.project_id,
                    cwd=args.cwd,
                    inbox_cwd=args.inbox_cwd,
                )
            )
            _emit(payload)
            return EXIT_OK
        if args.command == "sidebar-backfill":
            payload = dict(
                backend.sidebar_backfill(
                    days=None if args.all_history else args.days,
                    limit=args.limit,
                    apply=bool(args.apply),
                )
            )
            _emit(payload)
            return EXIT_DEGRADED if int(payload.get("failed", 0)) else EXIT_OK
        if args.command == "sidebar-continuous":
            payload = dict(backend.set_sidebar_continuous(enabled=bool(args.enable)))
            _emit(payload)
            return EXIT_OK
        if args.command == "sidebar-readable-preview":
            payload = dict(
                backend.set_sidebar_readable_preview(enabled=bool(args.enable))
            )
            _emit(payload)
            return EXIT_OK
        if args.command == "sidebar-hydration":
            payload = dict(backend.set_sidebar_hydration(enabled=bool(args.enable)))
            _emit(payload)
            return EXIT_OK
        if args.command == "sidebar-hydration-seed":
            payload = dict(
                backend.sidebar_hydration_seed(
                    source_session_id=args.source_session_id,
                    codex_thread_id=args.codex_thread_id,
                    confirmation=args.confirm,
                )
            )
            _emit(payload)
            return EXIT_OK
        if args.command == "sidebar-hydration-seed-backfill":
            payload = dict(
                backend.sidebar_hydration_seed_backfill(
                    days=None if args.all_history else args.days,
                    limit=args.limit,
                    apply=bool(args.apply),
                    confirmation=args.confirm,
                )
            )
            _emit(payload)
            return (
                EXIT_ROLLOUT_GATE
                if args.apply and int(payload.get("blocked", 0)) > 0
                else EXIT_OK
            )
        if args.command == "sidebar-hydration-status":
            payload = dict(backend.sidebar_hydration_status())
            _emit(payload)
            return EXIT_OK
        if args.command == "sidebar-retry-bound":
            payload = _public_sidebar_bound_retry_result(
                backend.sidebar_retry_bound(
                    job_id=args.job_id,
                    source_session_id=args.source_session_id,
                    codex_thread_id=args.codex_thread_id,
                    expected_error_code=args.expected_error_code,
                    confirmation=args.confirm,
                ),
                expected_error_code=args.expected_error_code,
            )
            _emit(payload)
            return EXIT_OK
        if args.command == "sidebar-acknowledge-unrecoverable":
            payload = _public_sidebar_terminal_resolution_result(
                backend.sidebar_acknowledge_unrecoverable(
                    job_id=args.job_id,
                    codex_thread_id=args.codex_thread_id,
                    expected_error_code=args.expected_error_code,
                )
            )
            _emit(payload)
            return EXIT_OK
        if args.command == "sidebar-acknowledge-precreate-unrecoverable":
            payload = _public_sidebar_terminal_resolution_result(
                backend.sidebar_acknowledge_precreate_unrecoverable(
                    job_id=args.job_id,
                    expected_error_code=args.expected_error_code,
                )
            )
            _emit(payload)
            return EXIT_OK
        if args.command == "sidebar-acknowledge-unbound-unrecoverable":
            payload = _public_sidebar_terminal_resolution_result(
                backend.sidebar_acknowledge_unbound_unrecoverable(
                    job_id=args.job_id,
                    expected_error_code=args.expected_error_code,
                )
            )
            _emit(payload)
            return EXIT_OK
        if args.command == "claude-visibility-status":
            payload = dict(backend.claude_visibility_status())
            _emit(payload)
            return (
                EXIT_DEGRADED
                if payload.get("degraded_reasons") or payload.get("fatal_reasons")
                else EXIT_OK
            )
        if args.command == "claude-visibility-backfill":
            payload = dict(
                backend.claude_visibility_backfill(
                    days=args.days,
                    limit=args.limit,
                    apply=bool(args.apply),
                )
            )
            _emit(payload)
            blocked = (
                any(
                    payload.get(key)
                    for key in ("open_reasons", "fatal_reasons", "degraded_reasons")
                )
                or payload.get("degraded") is True
            )
            return (
                EXIT_ROLLOUT_GATE
                if args.apply and blocked
                else (EXIT_DEGRADED if blocked else EXIT_OK)
            )
        if args.command == "claude-visibility-reconcile-lineage":
            if args.apply and not args.confirm_historical_repair:
                raise RolloutGateBlocked(
                    "historical_lineage_repair_confirmation_required"
                )
            payload = dict(
                backend.reconcile_claude_visibility_lineage(
                    limit=args.limit,
                    apply=bool(args.apply),
                    cursor=args.cursor,
                )
            )
            _emit(payload)
            incomplete = (
                bool(payload.get("blocker_codes"))
                or bool(payload.get("has_more"))
                or int(payload.get("remaining", 0)) > 0
                or payload.get("complete") is not True
            )
            if args.apply and incomplete:
                return EXIT_ROLLOUT_GATE
            return EXIT_DEGRADED if incomplete else EXIT_OK
        if args.command == "claude-visibility-continuous":
            payload = dict(
                backend.set_claude_visibility_continuous(enabled=bool(args.enable))
            )
            _emit(payload)
            return EXIT_OK
        if args.command == "claude-visibility-run-once":
            payload = dict(backend.claude_visibility_run_once())
            _emit(payload)
            return (
                EXIT_DEGRADED
                if payload.get("degraded") is True or payload.get("fatal") is True
                else EXIT_OK
            )
        if args.command == "claude-visibility-abort-characterization":
            if not args.confirm_exact_absence:
                raise RolloutGateBlocked(
                    "characterization_exact_absence_confirmation_required"
                )
            payload = dict(
                backend.abort_claude_visibility_characterization(
                    expected_job_id=args.job_id,
                    expected_reserved_claude_uuid=args.reserved_claude_uuid,
                )
            )
            _emit(payload)
            return EXIT_OK
        if args.command == "claude-visibility-repair-failed":
            if args.apply and not args.confirm_exact_terminal_repair:
                raise RolloutGateBlocked("visibility_repair_confirmation_required")
            operation = (
                backend.repair_failed_claude_visibility_job
                if args.apply
                else backend.inspect_failed_claude_visibility_job
            )
            payload = dict(
                operation(
                    job_id=args.job_id,
                    reserved_claude_uuid=args.reserved_claude_uuid,
                    expected_error_code=args.error_code,
                )
            )
            _emit(payload)
            if args.apply:
                return (
                    EXIT_OK if payload.get("status") == "visible" else EXIT_ROLLOUT_GATE
                )
            return EXIT_OK if payload.get("status") == "repairable" else EXIT_ROLLOUT_GATE
        if args.command == "claude-visibility-dismiss":
            if not args.confirm_terminal_failure:
                raise RolloutGateBlocked("visibility_dismiss_confirmation_required")
            _emit(
                dict(
                    backend.dismiss_claude_visibility_job(
                        job_id=args.job_id,
                        expected_error_code=args.error_code,
                    )
                )
            )
            return EXIT_OK
        if args.command == "characterize-claude-visibility":
            if args.cleanup_token is None:
                payload = backend.characterize_claude_visibility()
            else:
                try:
                    cleanup_token = json.loads(args.cleanup_token)
                except json.JSONDecodeError as exc:
                    raise ConfigurationFailure(
                        "characterization_cleanup_token_invalid"
                    ) from exc
                payload = backend.characterize_claude_visibility(cleanup_token)
            _emit(dict(payload))
            return EXIT_OK
        if args.command == "characterize":
            _emit(dict(backend.characterize(provider=args.provider)))
            return EXIT_OK
        if args.command == "backfill":
            return _backfill_command(args, config=config, backend=backend)
        if args.command == "mirror":
            return _mirror_command(args, config=config, backend=backend)
        raise ConfigurationFailure("unknown_command")
    except RolloutGateBlocked as exc:
        _emit({"error": "rollout_gate_blocked", "gate": exc.gate})
        return EXIT_ROLLOUT_GATE
    except ConfigurationFailure:
        _emit({"error": "configuration_error"})
        return EXIT_CONFIG
    except ProviderDegraded:
        _emit({"error": "provider_degraded"})
        return EXIT_DEGRADED
    except (OSError, PermissionError, TypeError, ValueError):
        _emit({"error": "configuration_error"})
        return EXIT_CONFIG
    except Exception:
        _emit({"error": "provider_degraded"})
        return EXIT_DEGRADED
    finally:
        try:
            backend.close()
        except Exception:
            pass


def _backfill_command(
    args: argparse.Namespace,
    *,
    config: BridgeConfig,
    backend: _Backend,
) -> int:
    if args.confirm_one_shot and not args.apply:
        raise ConfigurationFailure("confirmation_requires_apply")
    if args.apply:
        _require_mutation_gate(
            config=config,
            backend=backend,
            confirmed=bool(args.confirm_one_shot),
        )
    candidates = _ordered_candidates(backend.backfill_candidates(days=args.days))
    if args.dry_run:
        _emit({
            "mode": "dry_run",
            "days": args.days,
            "count": len(candidates),
            "candidates": [_public_candidate(item) for item in candidates],
        })
        return EXIT_OK
    cap = min(
        int(args.max_create),
        config.mirrors.creates_per_minute,
        config.mirrors.stop_after_attempts,
    )
    payload = dict(backend.apply_backfill(candidates=candidates[:cap]))
    _emit(payload)
    if isinstance(payload.get("gate"), str):
        return EXIT_ROLLOUT_GATE
    if payload.get("degraded") is True:
        return EXIT_DEGRADED
    if payload.get("halted") is True and int(payload.get("claimed", 0)) == 0:
        return EXIT_ROLLOUT_GATE
    return EXIT_OK


def _mirror_command(
    args: argparse.Namespace,
    *,
    config: BridgeConfig,
    backend: _Backend,
) -> int:
    if args.confirm_one_shot and not args.apply:
        raise ConfigurationFailure("confirmation_requires_apply")
    preview = dict(
        backend.mirror_preview(session_id=args.session_id, target=args.target)
    )
    public_preview = _public_preview(preview)
    if args.dry_run:
        _emit({"mode": "dry_run", **public_preview})
        return EXIT_OK
    _require_mutation_gate(
        config=config,
        backend=backend,
        confirmed=bool(args.confirm_one_shot),
    )
    if preview.get("would_enqueue") is not True:
        reason = str(preview.get("reason") or "ineligible")
        raise RolloutGateBlocked(f"mirror_{reason}")
    payload = dict(backend.apply_mirror(session_id=args.session_id, target=args.target))
    _emit(payload)
    return EXIT_DEGRADED if payload.get("degraded") is True else EXIT_OK


def _require_mutation_gate(
    *, config: BridgeConfig, backend: _Backend, confirmed: bool
) -> None:
    if not config.catalog.enabled:
        raise RolloutGateBlocked("catalog_disabled")
    characterization = backend.characterization_status()
    if characterization != "passed":
        raise RolloutGateBlocked(f"characterization_{characterization}")
    if not config.mirrors.automatic_creation and not confirmed:
        raise RolloutGateBlocked("one_shot_confirmation_required")


def _ordered_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    normalized = [dict(candidate) for candidate in candidates]
    return sorted(
        normalized,
        key=lambda item: (
            -float(item.get("last_active", 0.0)),
            str(item.get("canonical_id", "")),
            str(item.get("target_provider", "")),
        ),
    )


def _public_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: candidate[key]
        for key in (
            "canonical_id",
            "provider",
            "target_provider",
            "last_active",
            "eligible",
            "reason",
        )
        if key in candidate
    }


def _public_preview(preview: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: preview[key]
        for key in (
            "session_id",
            "target_provider",
            "would_enqueue",
            "reason",
            "job_state",
        )
        if key in preview
    }


def _public_claude_candidate(item: Any) -> dict[str, Any]:
    candidate = item.candidate
    return {
        "source_session_id": candidate.source_session_id,
        "source_provider": candidate.source_provider.value,
        "native_name": candidate.native_name,
        "source_cwd": candidate.source_cwd,
        "git_root": candidate.git_root,
        "git_branch": candidate.git_branch,
        "git_head": candidate.git_head,
        "worktree_id": candidate.worktree_id,
        "activity": item.activity,
        "job_id": item.identity.job_id,
    }


def _public_claude_apply(result: Any, *, continuous: bool) -> dict[str, Any]:
    return {
        "enabled": result.enabled,
        "continuous": continuous,
        "mode": result.mode,
        "dry_run": result.mode == "dry_run",
        "applied": result.mode == "apply",
        "enqueued": result.applied,
        "duplicates": result.duplicates,
        "candidates": [_public_claude_candidate(item) for item in result.candidates],
        "exclusions": [asdict(item) for item in result.exclusions],
        "open_reasons": list(result.open_reasons),
        "fatal_reasons": list(result.fatal_reasons),
        "degraded_reasons": list(result.fatal_reasons) if result.degraded else [],
        "degraded": result.degraded,
    }


def _public_claude_run(
    result: ClaudeVisibilityRunResult, *, continuous: bool
) -> dict[str, Any]:
    return {
        "enabled": result.enabled,
        "status": result.status,
        "job_id": result.job_id,
        "error_code": result.error_code,
        "degraded": result.degraded,
        "fatal": result.fatal,
        "discovery": (
            _public_claude_apply(result.discovery, continuous=continuous)
            if result.discovery is not None
            else None
        ),
    }


def _claude_visibility_open_reasons(raw: Mapping[str, Any]) -> list[str]:
    counts = raw.get("counts")
    if not isinstance(counts, Mapping):
        return ["invalid_status"]
    reasons: list[str] = []
    if any(
        int(counts.get(state, 0)) > 0
        for state in (
            "claude_pending",
            "claude_leased",
            "claude_retry",
            "claude_failed",
        )
    ):
        reasons.append("open_visibility_work")
    try:
        lineage = _public_claude_visibility_lineage(raw)
    except ConfigurationFailure:
        return ["invalid_status"]
    if lineage["unlinked_visible"] > 0:
        reasons.append("unlinked_visible_lineage")
    return reasons


def _claude_characterization_open_work_allowed(
    raw: Mapping[str, Any], *, active_operation: bool, active_job_id: str | None = None
) -> bool:
    """Permit recovery only for the one durable characterization retry row."""

    if type(active_operation) is not bool or not active_operation:
        return False
    counts = raw.get("counts")
    if not isinstance(counts, Mapping):
        return False
    try:
        open_counts = {
            state: int(counts.get(state, 0))
            for state in (
                "claude_pending",
                "claude_leased",
                "claude_retry",
                "claude_failed",
            )
        }
    except (TypeError, ValueError):
        return False
    if open_counts["claude_failed"] != 0:
        return _claude_characterization_auth_recovery_allowed(
            raw,
            active_operation=active_operation,
            active_job_id=active_job_id,
        )
    owned_states = [state for state, count in open_counts.items() if count != 0]
    if len(owned_states) != 1 or open_counts[owned_states[0]] != 1:
        return False
    expected_state = owned_states[0]
    characterizations = raw.get("characterizations")
    return (
        isinstance(active_job_id, str)
        and bool(active_job_id)
        and isinstance(characterizations, list)
        and characterizations == [{"job_id": active_job_id, "state": expected_state}]
    )


def _claude_characterization_auth_recovery_allowed(
    raw: Mapping[str, Any], *, active_operation: bool, active_job_id: str | None
) -> bool:
    """Allow only the exact authenticated bridge-conflict recovery FSM."""

    if type(active_operation) is not bool or not active_operation:
        return False
    counts = raw.get("counts")
    failed_codes = raw.get("failed_codes")
    retry_codes = raw.get("retry_codes")
    characterizations = raw.get("characterizations")
    if (
        not isinstance(counts, Mapping)
        or not isinstance(failed_codes, Mapping)
        or not isinstance(retry_codes, Mapping)
        or not isinstance(characterizations, list)
        or not isinstance(active_job_id, str)
        or not active_job_id
    ):
        return False
    try:
        open_counts = {
            state: int(counts.get(state, 0))
            for state in (
                "claude_pending",
                "claude_leased",
                "claude_retry",
                "claude_failed",
            )
        }
        normalized_failed = {
            str(code): int(count) for code, count in failed_codes.items()
        }
        normalized_retry = {
            str(code): int(count) for code, count in retry_codes.items()
        }
    except (TypeError, ValueError):
        return False
    return (
        open_counts
        == {
            "claude_pending": 0,
            "claude_leased": 0,
            "claude_retry": 0,
            "claude_failed": 1,
        }
        and normalized_failed == {"bridge_conflict": 1}
        and not any(count > 0 for count in normalized_retry.values())
        and characterizations == [{"job_id": active_job_id, "state": "claude_failed"}]
    )


def _claude_visibility_repair_required(
    raw: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Name every job whose repair authority is dead and awaiting an operator."""

    shaped, _malformed = normalized_claude_visibility_repair_rows(
        raw.get("repair_required", [])
    )
    return shaped


def _claude_visibility_fatal_reasons(raw: Mapping[str, Any]) -> list[str]:
    retry_codes = raw.get("retry_codes")
    failed_codes = raw.get("failed_codes")
    if not isinstance(retry_codes, Mapping) or not isinstance(failed_codes, Mapping):
        return ["invalid_status"]
    reasons: list[str] = []
    fatal = raw.get("fatal", [])
    if not isinstance(fatal, list):
        return ["invalid_status"]
    for item in fatal:
        if (
            not isinstance(item, Mapping)
            or item.get("code") not in CLAUDE_VISIBILITY_STATUS_FATAL_CODES
        ):
            reasons.append("invalid_status")
        else:
            reasons.append(str(item["code"]))
    for code, count in retry_codes.items():
        if code not in CLAUDE_VISIBILITY_RETRY_CODES and int(count) > 0:
            reasons.append("unknown_retry_code")
    for code, count in failed_codes.items():
        if int(count) <= 0:
            continue
        reasons.append(
            str(code)
            if code in CLAUDE_VISIBILITY_FATAL_CODES
            else "unknown_failed_code"
        )
    try:
        lineage = _public_claude_visibility_lineage(raw)
    except ConfigurationFailure:
        reasons.append("invalid_status")
    else:
        for code, count in lineage["blocker_codes"].items():
            if count > 0 and code != "claude_lineage_target_missing":
                reasons.append(code)
    return sorted(set(reasons))


def _public_claude_visibility_lineage(raw: Mapping[str, Any]) -> dict[str, Any]:
    value = raw.get(
        "lineage",
        {
            "unlinked_visible": 0,
            "repairable": 0,
            "blocked": 0,
            "blocker_codes": {},
        },
    )
    if not isinstance(value, Mapping) or set(value) != {
        "unlinked_visible",
        "repairable",
        "blocked",
        "blocker_codes",
    }:
        raise ConfigurationFailure("invalid_claude_lineage_status")
    selected: dict[str, int] = {}
    for name in ("unlinked_visible", "repairable", "blocked"):
        item = value.get(name)
        if type(item) is not int or item < 0:
            raise ConfigurationFailure("invalid_claude_lineage_status")
        selected[name] = item
    blockers = value.get("blocker_codes")
    if not isinstance(blockers, Mapping):
        raise ConfigurationFailure("invalid_claude_lineage_status")
    blocker_codes: dict[str, int] = {}
    for code, count in blockers.items():
        if (
            type(code) is not str
            or _CLAUDE_LINEAGE_CODE.fullmatch(code) is None
            or type(count) is not int
            or count < 1
        ):
            raise ConfigurationFailure("invalid_claude_lineage_status")
        blocker_codes[code] = count
    if (
        selected["repairable"] + selected["blocked"] != selected["unlinked_visible"]
        or sum(blocker_codes.values()) != selected["blocked"]
    ):
        raise ConfigurationFailure("invalid_claude_lineage_status")
    return {**selected, "blocker_codes": dict(sorted(blocker_codes.items()))}


def _disabled_claude_visibility_payload(continuous: bool) -> dict[str, Any]:
    return {
        "enabled": False,
        "continuous": continuous,
        "counts": {
            "claude_pending": 0,
            "claude_leased": 0,
            "claude_retry": 0,
            "claude_visible": 0,
            "claude_failed": 0,
        },
        "retry_codes": {},
        "failed_codes": {},
        "fatal": [],
        "usage": {"local_day": None, "attempts": 0, "reserved_cost_usd": "0"},
        "lineage": {
            "unlinked_visible": 0,
            "repairable": 0,
            "blocked": 0,
            "blocker_codes": {},
        },
        "candidates": [],
        "exclusions": [],
        "open_reasons": [],
        "fatal_reasons": [],
        "degraded_reasons": [],
        "last_cycle": {"tracked": False, "value": None},
        "last_empty_cycle": {"tracked": False, "value": None},
        "last_registrar_result": {"tracked": False, "value": None},
    }


def _public_sidebar_status(
    raw: Mapping[str, Any],
    *,
    now: float,
    heartbeat_interval_seconds: int,
    heartbeat_grace_seconds: int,
    oldest_job_alert_seconds: int,
    broker_thread_id: str | None,
    broker_project_id: str | None,
    broker_cwd: str | None,
) -> dict[str, Any]:
    status_time = _finite_status_number(now)
    if (
        type(heartbeat_interval_seconds) is not int
        or heartbeat_interval_seconds < 0
        or type(heartbeat_grace_seconds) is not int
        or heartbeat_grace_seconds < 0
        or type(oldest_job_alert_seconds) is not int
        or oldest_job_alert_seconds < 0
    ):
        raise ConfigurationFailure("invalid_sidebar_heartbeat_grace")
    raw_counts = raw.get("counts")
    counts = raw_counts if isinstance(raw_counts, Mapping) else {}
    state_counts = {
        state.value: _status_count(
            counts.get(state.value, counts.get(state.name.casefold(), 0))
        )
        for state in SidebarJobState
    }
    state_counts["sidebar_excluded"] = _status_count(counts.get("sidebar_excluded", 0))
    for field in ("ambiguous", "needs_attention", "projectless_legacy_count"):
        state_counts[field] = _status_count(counts.get(field, 0))
    blocking_failed_count = _status_count(
        raw.get(
            "blocking_failed_count",
            state_counts[SidebarJobState.FAILED.value],
        )
    )
    state_counts["needs_attention"] = blocking_failed_count
    terminally_resolved_failed_count = _status_count(
        raw.get("terminally_resolved_failed_count", 0)
    )
    ineffective_terminal_resolution_count = _status_count(
        raw.get("ineffective_terminal_resolution_count", 0)
    )
    ledger_valid_value = raw.get("terminal_resolution_ledger_valid", True)
    if type(ledger_valid_value) is not bool:
        raise ConfigurationFailure("invalid_sidebar_status")
    terminal_resolution_ledger_valid = cast(bool, ledger_valid_value)
    raw_terminal_resolutions = raw.get("terminal_resolutions")
    if raw_terminal_resolutions is None:
        terminal_resolutions_source: Mapping[str, Any] = {}
    elif isinstance(raw_terminal_resolutions, Mapping):
        terminal_resolutions_source = raw_terminal_resolutions
    else:
        raise ConfigurationFailure("invalid_sidebar_status")
    terminal_total = _status_count(
        terminal_resolutions_source.get(
            "total",
            terminally_resolved_failed_count + ineffective_terminal_resolution_count,
        )
    )
    terminal_effective = _status_count(
        terminal_resolutions_source.get("effective", terminally_resolved_failed_count)
    )
    terminal_ineffective = _status_count(
        terminal_resolutions_source.get(
            "ineffective", ineffective_terminal_resolution_count
        )
    )
    raw_resolution_codes = terminal_resolutions_source.get("by_resolution_code")
    if raw_resolution_codes is None:
        resolution_codes_source: Mapping[str, Any] = {}
        terminal_code_default = terminally_resolved_failed_count
    elif isinstance(raw_resolution_codes, Mapping):
        resolution_codes_source = raw_resolution_codes
        terminal_code_default = 0
    else:
        raise ConfigurationFailure("invalid_sidebar_status")
    known_resolution_codes = {
        SIDEBAR_TERMINAL_RESOLUTION_CODE,
        SIDEBAR_PRECREATE_RESOLUTION_CODE,
        SIDEBAR_UNBOUND_RESOLUTION_CODE,
    }
    if any(code not in known_resolution_codes for code in resolution_codes_source):
        raise ConfigurationFailure("invalid_sidebar_status")
    terminal_code_count = _status_count(
        resolution_codes_source.get(
            SIDEBAR_TERMINAL_RESOLUTION_CODE,
            terminal_code_default,
        )
    )
    precreate_code_count = _status_count(
        resolution_codes_source.get(SIDEBAR_PRECREATE_RESOLUTION_CODE, 0)
    )
    unbound_code_count = _status_count(
        resolution_codes_source.get(SIDEBAR_UNBOUND_RESOLUTION_CODE, 0)
    )
    if (
        blocking_failed_count + terminally_resolved_failed_count
        != state_counts[SidebarJobState.FAILED.value]
        or terminal_total != terminal_effective + terminal_ineffective
        or terminal_effective != terminally_resolved_failed_count
        or terminal_ineffective != ineffective_terminal_resolution_count
        or terminal_code_count + precreate_code_count + unbound_code_count
        != terminal_effective
    ):
        raise ConfigurationFailure("invalid_sidebar_status")
    raw_execution_blockers = raw.get("execution_blockers")
    if raw_execution_blockers is None:
        execution_blockers = [
            code
            for code, active in (
                (
                    "sidebar_terminal_resolution_mismatch",
                    ineffective_terminal_resolution_count > 0,
                ),
                (
                    "sidebar_terminal_resolution_ledger_invalid",
                    not terminal_resolution_ledger_valid,
                ),
            )
            if active
        ]
    elif isinstance(raw_execution_blockers, (list, tuple)):
        execution_blockers = list(raw_execution_blockers)
    else:
        raise ConfigurationFailure("invalid_sidebar_status")
    if execution_blockers != [
        code for code in _SIDEBAR_EXECUTION_BLOCKER_ORDER if code in execution_blockers
    ]:
        raise ConfigurationFailure("invalid_sidebar_status")
    required_execution_blockers = {
        code
        for code, active in (
            (
                "sidebar_terminal_resolution_mismatch",
                ineffective_terminal_resolution_count > 0,
            ),
            (
                "sidebar_terminal_resolution_ledger_invalid",
                not terminal_resolution_ledger_valid,
            ),
        )
        if active
    }
    if not required_execution_blockers.issubset(execution_blockers):
        raise ConfigurationFailure("invalid_sidebar_status")
    raw_providers = raw.get("eligible_by_provider")
    providers = raw_providers if isinstance(raw_providers, Mapping) else {}
    eligible_by_provider = {
        Provider.CLAUDE.value: _status_count(providers.get(Provider.CLAUDE.value, 0)),
        Provider.HERMES.value: _status_count(providers.get(Provider.HERMES.value, 0)),
    }
    oldest_eligible_age = _optional_status_number(
        raw.get("oldest_eligible_age_seconds")
    )
    oldest_age = _optional_status_number(raw.get("oldest_pending_age_seconds"))
    heartbeat_at = _optional_status_number(raw.get("last_heartbeat_at"))
    heartbeat_age = (
        max(0.0, status_time - heartbeat_at) if heartbeat_at is not None else None
    )
    heartbeat_threshold = heartbeat_interval_seconds + heartbeat_grace_seconds
    work_pending = (
        sum(
            state_counts[state.value]
            for state in (
                SidebarJobState.PENDING,
                SidebarJobState.LEASED,
                SidebarJobState.RETRY,
            )
        )
        > 0
    )
    degraded_reasons: list[str] = []
    if blocking_failed_count > 0:
        degraded_reasons.append("sidebar_failed")
    heartbeat_stale = (
        heartbeat_age is not None and heartbeat_age > heartbeat_threshold
    )
    overdue_work = (
        work_pending
        and oldest_eligible_age is not None
        and oldest_eligible_age > oldest_job_alert_seconds
    )
    if heartbeat_stale:
        degraded_reasons.append("broker_heartbeat_stale")
    if overdue_work:
        degraded_reasons.append("oldest_pending_stale")
    degraded_reasons.extend(
        blocker for blocker in execution_blockers if blocker not in degraded_reasons
    )
    raw_codes = raw.get("recent_error_codes")
    allowed_codes = SIDEBAR_RETRYABLE_ERRORS | SIDEBAR_FATAL_ERRORS
    recent_codes = (
        [code for code in raw_codes if isinstance(code, str) and code in allowed_codes][
            :10
        ]
        if isinstance(raw_codes, list)
        else []
    )
    raw_latency = raw.get("delivery_latency_seconds")
    latency = raw_latency if isinstance(raw_latency, Mapping) else {}
    stage_names = (
        "source_to_index",
        "index_to_queue",
        "queue_to_visible",
        "source_to_visible",
    )
    raw_stage_latency = raw.get("stage_latency_seconds")
    if raw_stage_latency is None:
        stage_latency: Mapping[str, Any] = {}
    elif isinstance(raw_stage_latency, Mapping):
        stage_latency = raw_stage_latency
    else:
        raise ConfigurationFailure("invalid_sidebar_status")
    if any(stage not in stage_names for stage in stage_latency):
        raise ConfigurationFailure("invalid_sidebar_status")
    shaped_stage_latency: dict[str, dict[str, float | None]] = {}
    for stage in stage_names:
        raw_percentiles = stage_latency.get(stage)
        if raw_percentiles is None:
            percentiles: Mapping[str, Any] = {}
        elif isinstance(raw_percentiles, Mapping):
            percentiles = raw_percentiles
        else:
            raise ConfigurationFailure("invalid_sidebar_status")
        if any(key not in {"p50", "p95"} for key in percentiles):
            raise ConfigurationFailure("invalid_sidebar_status")
        shaped_stage_latency[stage] = {
            percentile: _optional_status_number(percentiles.get(percentile))
            for percentile in ("p50", "p95")
        }
    raw_scheduler = raw.get("scheduler")
    if raw_scheduler is None:
        scheduler: Mapping[str, Any] = {}
    elif isinstance(raw_scheduler, Mapping):
        scheduler = raw_scheduler
    else:
        raise ConfigurationFailure("invalid_sidebar_status")
    fresh_claims = scheduler.get("fresh_claims_since_oldest", 0)
    next_lane = scheduler.get("next_lane", "fresh")
    if (
        type(fresh_claims) is not int
        or not 0 <= fresh_claims <= 3
        or next_lane not in {"fresh", "oldest"}
        or next_lane != ("oldest" if fresh_claims == 3 else "fresh")
    ):
        raise ConfigurationFailure("invalid_sidebar_status")
    raw_recovery = raw.get("recovery")
    if raw_recovery is None:
        recovery: Mapping[str, Any] = {}
    elif isinstance(raw_recovery, Mapping):
        recovery = raw_recovery
    else:
        raise ConfigurationFailure("invalid_sidebar_status")
    recovery_lane = recovery.get("lane")
    recovery_status = recovery.get("status")
    recovery_at = _optional_status_number(recovery.get("last_cycle_at"))
    if (
        recovery_lane not in {None, "hydration", "registration"}
        or recovery_status
        not in {None, "idle", "visible", "retry", "failed", "unsettled"}
        or (recovery_lane is None) != (recovery_status is None)
        or (recovery_lane is None) != (recovery_at is None)
    ):
        raise ConfigurationFailure("invalid_sidebar_status")
    raw_reconciliation_counts = raw.get("reconciliation_counts")
    if raw_reconciliation_counts is None:
        reconciliation_counts_source: Mapping[str, Any] = {}
    elif isinstance(raw_reconciliation_counts, Mapping):
        reconciliation_counts_source = raw_reconciliation_counts
    else:
        raise ConfigurationFailure("invalid_sidebar_status")
    reconciliation_counts = {
        state: _status_count(reconciliation_counts_source.get(state, 0))
        for state in ("recovered", "absence_proven", "blocked")
    }
    raw_reconciliation_blocked_codes = raw.get("reconciliation_blocked_codes")
    if raw_reconciliation_blocked_codes is None:
        reconciliation_blocked_source: Mapping[str, Any] = {}
    elif isinstance(raw_reconciliation_blocked_codes, Mapping):
        reconciliation_blocked_source = raw_reconciliation_blocked_codes
    else:
        raise ConfigurationFailure("invalid_sidebar_status")
    reconciliation_blocked_codes = {
        code: _status_count(reconciliation_blocked_source.get(code, 0))
        for code in (
            "marker_conflict",
            "native_create_ambiguous",
            "bridge_temporarily_unavailable",
        )
    }
    oldest_reconciliation_wait_age = _optional_status_number(
        raw.get("oldest_reconciliation_wait_age_seconds")
    )
    reconciliation_scan_age = _optional_status_number(
        raw.get("reconciliation_scan_age_seconds")
    )
    recovered_existing_total = _status_count(raw.get("recovered_existing_total", 0))
    created_new_total = _status_count(raw.get("created_new_total", 0))
    task_id = raw.get("last_visible_task_id")
    placement = (
        _public_sidebar_placement_status(raw.get("placement"))
        if "placement" in raw
        else None
    )
    result = {
        "healthy": not degraded_reasons,
        "degraded_reasons": degraded_reasons,
        "eligible_by_provider": eligible_by_provider,
        "counts": state_counts,
        "blocking_failed_count": blocking_failed_count,
        "terminally_resolved_failed_count": terminally_resolved_failed_count,
        "ineffective_terminal_resolution_count": (
            ineffective_terminal_resolution_count
        ),
        "terminal_resolution_ledger_valid": terminal_resolution_ledger_valid,
        "terminal_resolutions": {
            "total": terminal_total,
            "effective": terminal_effective,
            "ineffective": terminal_ineffective,
            "by_resolution_code": {
                SIDEBAR_TERMINAL_RESOLUTION_CODE: terminal_code_count,
                SIDEBAR_PRECREATE_RESOLUTION_CODE: precreate_code_count,
                SIDEBAR_UNBOUND_RESOLUTION_CODE: unbound_code_count,
            },
        },
        "execution_blockers": execution_blockers,
        "oldest_eligible_age_seconds": oldest_eligible_age,
        "oldest_pending_age_seconds": oldest_age,
        "last_heartbeat_at": heartbeat_at,
        "last_successful_heartbeat_at": heartbeat_at,
        "heartbeat_age_seconds": heartbeat_age,
        "heartbeat_stale_seconds": heartbeat_threshold,
        "heartbeat_stale": heartbeat_stale,
        "oldest_job_overdue": overdue_work,
        "broker": {
            "thread_id": broker_thread_id,
            "project_id": broker_project_id,
            "cwd": broker_cwd,
        },
        "last_visible_task_id": redact_codex_thread_id(task_id),
        "recent_error_codes": recent_codes,
        "reconciliation_counts": reconciliation_counts,
        "reconciliation_blocked_codes": reconciliation_blocked_codes,
        "oldest_reconciliation_wait_age_seconds": oldest_reconciliation_wait_age,
        "reconciliation_scan_age_seconds": reconciliation_scan_age,
        "recovered_existing_total": recovered_existing_total,
        "created_new_total": created_new_total,
        "delivery_latency_seconds": {
            percentile: _optional_status_number(latency.get(percentile))
            for percentile in ("p50", "p95", "p99")
        },
        "stage_latency_seconds": shaped_stage_latency,
        "scheduler": {
            "fresh_claims_since_oldest": fresh_claims,
            "next_lane": next_lane,
        },
        "recovery": {
            "lane": recovery_lane,
            "status": recovery_status,
            "last_cycle_at": recovery_at,
        },
    }
    if placement is not None:
        result["placement"] = placement
    return result


def _public_sidebar_placement_status(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or set(value) != {
        "inbox_cwd",
        "generation",
        "verified_visible",
        "mismatch_count",
        "canary",
    }:
        raise ConfigurationFailure("invalid_sidebar_status")
    inbox_cwd = value.get("inbox_cwd")
    if not is_canonical_sidebar_string(inbox_cwd):
        return None
    generation = _status_count(value.get("generation"))
    if generation < 1:
        raise ConfigurationFailure("invalid_sidebar_status")
    canary = value.get("canary")
    if not isinstance(canary, Mapping) or set(canary) != {
        "status",
        "verified_at",
    }:
        raise ConfigurationFailure("invalid_sidebar_status")
    canary_status = canary.get("status")
    verified_at = _optional_status_number(canary.get("verified_at"))
    if canary_status not in {"not_run", "passed", "failed"} or (
        (canary_status == "not_run") != (verified_at is None)
    ):
        raise ConfigurationFailure("invalid_sidebar_status")
    return {
        "inbox_cwd": inbox_cwd,
        "generation": generation,
        "verified_visible": _status_count(value.get("verified_visible")),
        "mismatch_count": _status_count(value.get("mismatch_count")),
        "canary": {
            "status": canary_status,
            "verified_at": verified_at,
        },
    }


def _public_sidebar_execution_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    status = raw.get("status")
    if status not in {"idle", "visible", "retry", "failed", "unsettled"}:
        raise ProviderDegraded("invalid_sidebar_execution_result")
    result: dict[str, Any] = {"status": status}
    error_code = raw.get("error_code")
    if error_code is not None:
        if error_code not in SIDEBAR_RETRYABLE_ERRORS | SIDEBAR_FATAL_ERRORS:
            raise ProviderDegraded("invalid_sidebar_execution_result")
        result["error_code"] = error_code
    return result


def _public_sidebar_hydration_status(
    raw: Mapping[str, Any],
    *,
    enabled: bool,
) -> dict[str, Any]:
    raw_counts = raw.get("counts")
    counts = raw_counts if isinstance(raw_counts, Mapping) else {}
    raw_health_counts = raw.get("health_counts")
    health_counts = (
        raw_health_counts if isinstance(raw_health_counts, Mapping) else {}
    )
    raw_codes = raw.get("recent_error_codes")
    allowed_codes = HYDRATION_RETRYABLE_ERRORS | HYDRATION_FATAL_ERRORS
    return {
        "enabled": enabled is True,
        "counts": {
            "pending": _status_count(
                health_counts.get(
                    "pending", counts.get(SidebarHydrationState.PENDING.value, 0)
                )
            ),
            "leased": _status_count(
                health_counts.get(
                    "leased", counts.get(SidebarHydrationState.LEASED.value, 0)
                )
            ),
            "retry": _status_count(
                health_counts.get(
                    "retry", counts.get(SidebarHydrationState.RETRY.value, 0)
                )
            ),
            "committed": _status_count(
                health_counts.get(
                    "committed", counts.get(SidebarHydrationState.VISIBLE.value, 0)
                )
            ),
            "ambiguous": _status_count(health_counts.get("ambiguous", 0)),
            "failed": _status_count(
                health_counts.get(
                    "failed", counts.get(SidebarHydrationState.FAILED.value, 0)
                )
            ),
        },
        "oldest_pending_age_seconds": _optional_status_number(
            raw.get("oldest_pending_age_seconds")
        ),
        "active_lease": raw.get("active_lease") is True,
        "reserved_reconciliation": _status_count(
            raw.get("reserved_reconciliation", 0)
        ),
        "recent_error_codes": (
            [
                code
                for code in raw_codes
                if isinstance(code, str) and code in allowed_codes
            ][:10]
            if isinstance(raw_codes, list)
            else []
        ),
    }


def _public_sidebar_bound_retry_result(
    raw: Mapping[str, Any],
    *,
    expected_error_code: str,
) -> dict[str, Any]:
    status = raw.get("status")
    state = raw.get("state")
    job_id = raw.get("job_id")
    thread_id = raw.get("codex_thread_id")
    if (
        status != "requeued"
        or state != SidebarJobState.RETRY.value
        or expected_error_code
        not in {
            "native_task_not_indexed",
            "codex_thread_conflict",
            "native_create_ambiguous",
            "marker_conflict",
            "bridge_temporarily_unavailable",
            "source_identity_mismatch",
        }
        or raw.get("error_code") != expected_error_code
        or not isinstance(job_id, str)
        or re.fullmatch(r"sidebar-job:[0-9a-f]{64}", job_id) is None
        or not isinstance(thread_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,511}", thread_id) is None
    ):
        raise ProviderDegraded("invalid_sidebar_bound_retry_result")
    return {
        "status": "requeued",
        "job_id": job_id,
        "codex_thread_id": thread_id,
        "state": SidebarJobState.RETRY.value,
    }


def _public_sidebar_terminal_resolution_result(
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    status = raw.get("status")
    if status not in {"acknowledged", "already_acknowledged"}:
        raise ProviderDegraded("invalid_sidebar_terminal_resolution_result")
    if raw.get("error_code") != "native_create_ambiguous":
        raise ProviderDegraded("invalid_sidebar_terminal_resolution_result")
    resolution_code = raw.get("resolution_code")
    if resolution_code not in {
        SIDEBAR_TERMINAL_RESOLUTION_CODE,
        SIDEBAR_PRECREATE_RESOLUTION_CODE,
        SIDEBAR_UNBOUND_RESOLUTION_CODE,
    }:
        raise ProviderDegraded("invalid_sidebar_terminal_resolution_result")
    return {
        "status": status,
        "error_code": "native_create_ambiguous",
        "resolution_code": resolution_code,
    }


def _status_count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ConfigurationFailure("invalid_sidebar_status")
    return value


def _canonical_sidebar_broker_value(value: object) -> str:
    if not is_canonical_sidebar_string(value):
        raise ConfigurationFailure("invalid_sidebar_broker_identity")
    return cast(str, value)


def _persisted_sidebar_values(document: object) -> Mapping[str, Any]:
    if not isinstance(document, Mapping):
        raise ConfigurationFailure("invalid_persisted_sidebar_config")
    bridge = document.get("session_bridge")
    if not isinstance(bridge, Mapping):
        raise ConfigurationFailure("invalid_persisted_sidebar_config")
    sidebar = bridge.get("sidebar")
    if not isinstance(sidebar, Mapping):
        raise ConfigurationFailure("invalid_persisted_sidebar_config")
    return sidebar


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _finite_status_number(value: object) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ConfigurationFailure("invalid_sidebar_status")
    return float(value)


def _optional_status_number(value: object) -> float | None:
    if value is None:
        return None
    result = _finite_status_number(value)
    if result < 0:
        raise ConfigurationFailure("invalid_sidebar_status")
    return result


def _sanitize(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.casefold() == "cleanup_token":
        if (
            isinstance(value, Mapping)
            and set(value) == {"id", "capability"}
            and all(isinstance(item, str) for item in value.values())
        ):
            return {"id": value["id"], "capability": value["capability"]}
        return None
    if key is not None and any(
        fragment in key.casefold() for fragment in _SENSITIVE_KEY_FRAGMENTS
    ):
        return None
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitized
            for item_key, item_value in value.items()
            if (sanitized := _sanitize(item_value, key=str(item_key))) is not None
        }
    if isinstance(value, (list, tuple)):
        return [
            sanitized for item in value if (sanitized := _sanitize(item)) is not None
        ]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _emit(payload: Mapping[str, Any]) -> None:
    print(
        json.dumps(
            _sanitize(dict(payload)),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _bounded_create_count(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > _MAX_BACKFILL_CREATE:
        raise argparse.ArgumentTypeError(
            f"value must be at most {_MAX_BACKFILL_CREATE}"
        )
    return parsed


def _bounded_sidebar_days(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 30:
        raise argparse.ArgumentTypeError("value must be at most 30")
    return parsed


def _bounded_sidebar_limit(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 10:
        raise argparse.ArgumentTypeError("value must be at most 10")
    return parsed


def _bounded_sidebar_hydration_limit(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 500:
        raise argparse.ArgumentTypeError("value must be at most 500")
    return parsed


def _sidebar_terminal_job_id(value: str) -> str:
    if re.fullmatch(r"sidebar-job:[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError("invalid sidebar job ID")
    return value


def _sidebar_terminal_thread_id(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,511}", value) is None:
        raise argparse.ArgumentTypeError("invalid Codex thread ID")
    return value


def _bounded_claude_visibility_limit(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 10:
        raise argparse.ArgumentTypeError("value must be at most 10")
    return parsed


def _bounded_claude_lineage_limit(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 100:
        raise argparse.ArgumentTypeError("value must be at most 100")
    return parsed


def _claude_lineage_cursor_argument(value: str) -> Mapping[str, Any]:
    parsed = _strict_json_object(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(
            "cursor must be one strict JSON object emitted by the preceding page"
        )
    return parsed


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
