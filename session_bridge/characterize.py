from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from typing import Any
import uuid

from agent.transports.codex_app_server import CodexAppServerClient

from .claude_adapter import (
    CLAUDE_PLACEHOLDER_MAX_BUDGET_USD,
    ClaudeSourceAdapter,
    ClaudeTargetAdapter,
    PlaceholderCreationError,
    classify_claude_process_failure,
)
from .codex_adapter import CodexSourceAdapter, CodexTargetAdapter
from .models import OriginKind, SessionProjection


_REPORT_ROOT = Path.home() / ".hermes" / "session-bridge" / "characterization"
_CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
_SENSITIVE_REPORT_KEYS = frozenset({
    "context",
    "marker",
    "prompt",
    "secret",
    "stderr",
    "stdout",
    "token",
    "transcript",
})
_MARKER_PREFIX = "HERMES_SESSION_BRIDGE_V1:"
_SECRET_RE = re.compile(
    r"(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{12,}|"
    r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{12,}|"
    r"(?i:bearer\s+)[A-Za-z0-9._~+/-]{12,})"
)


class UnsafeCharacterizationCleanup(RuntimeError):
    pass


class LiveCharacterizationError(RuntimeError):
    def __init__(self, report_path: Path, failures: list[str]) -> None:
        self.report_path = report_path
        self.failures = tuple(failures)
        super().__init__(
            "live_characterization_failed:"
            + ",".join(self.failures)
            + f"; report={report_path}"
        )


def write_characterization_report(
    report: Mapping[str, Any],
    *,
    report_root: Path = _REPORT_ROOT,
    characterization_id: str,
) -> Path:
    record_id = _canonical_uuid(characterization_id)
    root = Path(report_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / f"{record_id}.json"
    temporary = root / f".{record_id}.tmp"
    sanitized = _sanitize_report_value(dict(report))
    payload = json.dumps(
        sanitized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    temporary.write_text(payload + "\n", encoding="utf-8")
    temporary.replace(report_path)
    return report_path


def quarantine_claude_transcript(
    source_adapter: ClaudeSourceAdapter,
    *,
    native_id: str,
    bridge_id: str,
    projects_root: Path = _CLAUDE_PROJECTS_ROOT,
    quarantine_root: Path | None = None,
) -> Path:
    expected_id = _canonical_uuid(native_id)
    if not isinstance(bridge_id, str) or not bridge_id.strip():
        raise UnsafeCharacterizationCleanup("bridge identity is missing")
    path = source_adapter.find_native_session(expected_id)
    if path is None:
        raise UnsafeCharacterizationCleanup("exact Claude transcript was not found")
    candidate = Path(path).resolve()
    allowed_root = Path(projects_root).expanduser().resolve()
    try:
        candidate.relative_to(allowed_root)
    except ValueError as exc:
        raise UnsafeCharacterizationCleanup(
            "Claude transcript is outside the projects root"
        ) from exc
    try:
        projection = source_adapter.parse(candidate).projection
    except Exception as exc:
        raise UnsafeCharacterizationCleanup(
            "Claude transcript could not be parsed safely"
        ) from exc
    if projection.native_id != expected_id:
        raise UnsafeCharacterizationCleanup("Claude transcript UUID mismatch")
    if (
        projection.origin_kind
        not in (OriginKind.BRIDGE_PLACEHOLDER, OriginKind.BRIDGE_CONTINUATION)
        or projection.origin_bridge_id != bridge_id.strip()
    ):
        raise UnsafeCharacterizationCleanup("Claude signed marker mismatch")

    destination_root = (
        Path(quarantine_root).expanduser()
        if quarantine_root is not None
        else _REPORT_ROOT / "quarantine"
    )
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / f"{expected_id}.jsonl"
    if destination.exists():
        raise UnsafeCharacterizationCleanup("Claude quarantine target already exists")
    shutil.move(str(candidate), str(destination))
    return destination


def run_live_characterization(
    *,
    report_root: Path = _REPORT_ROOT,
    claude_projects_root: Path = _CLAUDE_PROJECTS_ROOT,
    claude_executable: str = "claude",
    codex_executable: str = "codex",
    cwd: Path | None = None,
) -> Path:
    if os.environ.get("HERMES_SESSION_BRIDGE_LIVE_TESTS") != "1":
        raise RuntimeError("live_characterization_not_enabled")
    claude_executable = resolve_cli_executable(claude_executable)
    codex_executable = resolve_cli_executable(codex_executable)
    characterization_id = str(uuid.uuid4())
    title = f"[Hermes Bridge Characterization] {characterization_id}"
    marker_secret = secrets.token_bytes(32)
    working_directory = Path(cwd or Path.cwd()).resolve()
    report: dict[str, Any] = {
        "schema_version": 1,
        "characterization_id": characterization_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "automatic_mirroring_enabled": False,
        "versions": {
            "claude": _cli_version([claude_executable, "--version"]),
            "codex": _cli_version([codex_executable, "--version"]),
        },
        "providers": {
            "claude": _provider_report(),
            "codex": _provider_report(),
        },
    }
    failures: list[str] = []
    try:
        _characterize_claude(
            report["providers"]["claude"],
            characterization_id=characterization_id,
            title=title,
            marker_secret=marker_secret,
            projects_root=Path(claude_projects_root),
            report_root=Path(report_root),
            executable=claude_executable,
            cwd=working_directory,
        )
    except Exception as exc:
        code = _safe_error_code("claude", exc)
        report["providers"]["claude"]["error_code"] = code
        if isinstance(exc, PlaceholderCreationError):
            _record_claude_failure_diagnostics(
                report["providers"]["claude"], exc
            )
        failures.append(code)
    try:
        _characterize_codex(
            report["providers"]["codex"],
            characterization_id=characterization_id,
            title=title,
            marker_secret=marker_secret,
            executable=codex_executable,
            cwd=working_directory,
        )
    except Exception as exc:
        code = _safe_error_code("codex", exc)
        report["providers"]["codex"]["error_code"] = code
        failures.append(code)

    report_path = write_characterization_report(
        report,
        report_root=Path(report_root),
        characterization_id=characterization_id,
    )
    if failures:
        raise LiveCharacterizationError(report_path, failures)
    return report_path


def _characterize_claude(
    status: dict[str, Any],
    *,
    characterization_id: str,
    title: str,
    marker_secret: bytes,
    projects_root: Path,
    report_root: Path,
    executable: str,
    cwd: Path,
) -> None:
    native_id = str(uuid.uuid4())
    bridge_id = f"characterization-{characterization_id}-claude"
    status["native_id"] = native_id
    source = ClaudeSourceAdapter(projects_root, marker_secret=marker_secret)
    creation_processes: list[subprocess.CompletedProcess[str]] = []

    def _run_creation(
        args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(args, **kwargs)
        creation_processes.append(completed)
        return completed

    try:
        creation_started = time.monotonic()
        result = ClaudeTargetAdapter(
            source,
            marker_secret=marker_secret,
            claude_executable=executable,
            runner=_run_creation,
            process_timeout=180.0,
            discovery_timeout=30.0,
        ).create_placeholder(
            native_id=native_id,
            title=title,
            source_session_id=f"codex:characterization-{characterization_id}",
            bridge_id=bridge_id,
            policy_generation=1,
            cwd=cwd,
        )
        create_elapsed_ms = (time.monotonic() - creation_started) * 1000.0
        create_metrics = (
            _claude_result_metrics(creation_processes[-1])
            if creation_processes
            else {}
        )
        status["create_cost_usd"] = create_metrics.get("cost_usd")
        status["create_latency_ms"] = create_metrics.get(
            "duration_ms", create_elapsed_ms
        )
        status["create_num_turns"] = create_metrics.get("num_turns")
        status["create"] = result.native_id == native_id
        path = source.find_native_session(native_id)
        status["discover"] = path is not None
        if path is None:
            raise RuntimeError("claude_discovery_failed")
        projection = source.parse(path).projection
        status["read"] = (
            projection.native_id == native_id
            and projection.origin_bridge_id == bridge_id
        )
        if not status["read"]:
            raise RuntimeError("claude_read_verification_failed")

        resume_started = time.monotonic()
        resume = _resume_claude_characterization(
            source,
            baseline_projection=projection,
            native_id=native_id,
            bridge_id=bridge_id,
            resume_nonce=secrets.token_hex(16),
            executable=executable,
            cwd=cwd,
        )
        resume_elapsed_ms = (time.monotonic() - resume_started) * 1000.0
        resume_metrics = _claude_result_metrics(resume) if resume is not None else {}
        status["resume_cost_usd"] = resume_metrics.get("cost_usd")
        status["resume_latency_ms"] = resume_metrics.get(
            "duration_ms", resume_elapsed_ms
        )
        status["resume_num_turns"] = resume_metrics.get("num_turns")
        costs = [
            value
            for value in (
                status.get("create_cost_usd"),
                status.get("resume_cost_usd"),
            )
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        status["total_cost_usd"] = sum(costs) if costs else None
        status["total_latency_ms"] = float(status["create_latency_ms"]) + float(
            status["resume_latency_ms"]
        )
        turns = [
            value
            for value in (
                status.get("create_num_turns"),
                status.get("resume_num_turns"),
            )
            if isinstance(value, int) and not isinstance(value, bool)
        ]
        status["total_num_turns"] = sum(turns) if turns else None
        status["resume"] = True
    except PlaceholderCreationError as exc:
        _record_claude_failure_diagnostics(status, exc)
        raise
    finally:
        try:
            quarantine_claude_transcript(
                source,
                native_id=native_id,
                bridge_id=bridge_id,
                projects_root=projects_root,
                quarantine_root=report_root / "quarantine",
            )
            status["cleanup"] = "quarantined"
        except UnsafeCharacterizationCleanup:
            status["cleanup"] = "not_moved_safety_check"


def _resume_claude_characterization(
    source: ClaudeSourceAdapter,
    *,
    baseline_projection: SessionProjection,
    native_id: str,
    bridge_id: str,
    resume_nonce: str,
    executable: str,
    cwd: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    process_timeout: float = 180.0,
    verification_timeout: float = 30.0,
    verification_poll_interval: float = 0.1,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> subprocess.CompletedProcess[str] | None:
    if not re.fullmatch(r"[0-9a-f]{32}", resume_nonce):
        raise ValueError("Claude resume nonce must be 32 lowercase hex characters")
    if (
        baseline_projection.native_id != native_id
        or baseline_projection.origin_bridge_id != bridge_id
        or baseline_projection.origin_kind
        not in (OriginKind.BRIDGE_PLACEHOLDER, OriginKind.BRIDGE_CONTINUATION)
    ):
        raise PlaceholderCreationError("claude_resume_baseline_mismatch")
    baseline_cursor = baseline_projection.native_cursor
    baseline_hash = baseline_projection.native_hash
    baseline_messages = _projection_message_identities(baseline_projection)
    if not baseline_cursor or not baseline_hash or not baseline_messages:
        raise PlaceholderCreationError("claude_resume_baseline_incomplete")

    prompt = (
        "Hermes Bridge live characterization resume verification tag "
        f"{resume_nonce}. Reply READY."
    )
    args = [
        executable,
        "--print",
        "--resume",
        native_id,
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--max-budget-usd",
        CLAUDE_PLACEHOLDER_MAX_BUDGET_USD,
        "--output-format",
        "json",
        prompt,
    ]
    completed: subprocess.CompletedProcess[str] | None = None
    process_failure: PlaceholderCreationError | None = None
    metrics: dict[str, int | float] = {}
    try:
        completed = runner(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=process_timeout,
            stdin=subprocess.DEVNULL,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        process_failure = PlaceholderCreationError("claude_resume_timeout")
    except FileNotFoundError as exc:
        raise PlaceholderCreationError("claude_resume_executable_not_found") from exc
    except Exception as exc:
        raise PlaceholderCreationError("claude_resume_process_failed") from exc
    else:
        metrics = _claude_result_metrics(completed)
        if completed.returncode != 0:
            process_code = classify_claude_process_failure(completed)
            suffix = process_code.removeprefix("claude_process_")
            process_failure = _claude_resume_error(
                f"claude_resume_{suffix}", metrics
            )

    deadline = monotonic() + verification_timeout
    last_code = "claude_resume_target_not_found"
    while True:
        path = source.find_native_session(native_id)
        if path is not None:
            try:
                projection = source.parse(path).projection
            except Exception as exc:
                raise _claude_resume_error(
                    "claude_resume_target_unreadable", metrics
                ) from exc
            if projection.native_id != native_id:
                raise _claude_resume_error(
                    "claude_resume_identity_mismatch", metrics
                )
            if (
                projection.origin_bridge_id != bridge_id
                or projection.origin_kind
                not in (
                    OriginKind.BRIDGE_PLACEHOLDER,
                    OriginKind.BRIDGE_CONTINUATION,
                )
            ):
                raise _claude_resume_error(
                    "claude_resume_marker_mismatch", metrics
                )

            post_messages = _projection_message_identities(projection)
            post_fingerprint = (
                projection.native_cursor,
                projection.native_hash,
                post_messages,
            )
            baseline_fingerprint = (
                baseline_cursor,
                baseline_hash,
                baseline_messages,
            )
            new_messages = post_messages - baseline_messages
            advanced = (
                projection.origin_kind is OriginKind.BRIDGE_CONTINUATION
                and bool(projection.native_cursor)
                and bool(projection.native_hash)
                and projection.native_cursor != baseline_cursor
                and post_fingerprint != baseline_fingerprint
                and baseline_messages.issubset(post_messages)
                and bool(new_messages)
            )
            if advanced:
                nonce_found = any(
                    (message.native_event_id, message.ordinal) in new_messages
                    and message.role == "user"
                    and isinstance(message.content, str)
                    and resume_nonce in message.content
                    for message in projection.messages
                )
                if nonce_found:
                    return completed
                last_code = "claude_resume_nonce_mismatch"
            else:
                last_code = "claude_resume_not_advanced"
        if monotonic() >= deadline:
            if (
                last_code == "claude_resume_target_not_found"
                and process_failure is not None
            ):
                raise process_failure
            raise _claude_resume_error(last_code, metrics)
        sleep(verification_poll_interval)


def _projection_message_identities(
    projection: SessionProjection,
) -> frozenset[tuple[str, int]]:
    return frozenset(
        (message.native_event_id, message.ordinal)
        for message in projection.messages
        if isinstance(message.native_event_id, str)
        and message.native_event_id
        and isinstance(message.ordinal, int)
        and not isinstance(message.ordinal, bool)
        and message.ordinal >= 0
    )


def _claude_resume_error(
    code: str, metrics: Mapping[str, int | float]
) -> PlaceholderCreationError:
    cost = metrics.get("cost_usd")
    duration = metrics.get("duration_ms")
    turns = metrics.get("num_turns")
    return PlaceholderCreationError(
        code,
        observed_cost_usd=cost,
        duration_ms=duration,
        num_turns=turns if isinstance(turns, int) else None,
    )


def _characterize_codex(
    status: dict[str, Any],
    *,
    characterization_id: str,
    title: str,
    marker_secret: bytes,
    executable: str,
    cwd: Path,
) -> None:
    characterization_started = time.monotonic()
    client = CodexAppServerClient(codex_bin=executable)
    native_id: str | None = None
    try:
        source = CodexSourceAdapter(client, marker_secret=marker_secret)
        try:
            create_started = time.monotonic()
            result = CodexTargetAdapter(
                client,
                source_adapter=source,
                marker_secret=marker_secret,
                require_registration_turn=None,
                request_timeout=45.0,
            ).create_placeholder(
                title=title,
                source_session_id=f"claude:characterization-{characterization_id}",
                bridge_id=f"characterization-{characterization_id}-codex",
                policy_generation=1,
                cwd=cwd,
            )
            status["create_latency_ms"] = (
                time.monotonic() - create_started
            ) * 1000.0
        except PlaceholderCreationError as exc:
            native_id = exc.native_id
            if native_id is not None:
                status["native_id"] = native_id
            raise
        native_id = result.native_id
        status["native_id"] = native_id
        status["create"] = True
        status["used_registration_turn"] = result.used_registration_turn
        if result.used_registration_turn:
            _wait_for_turn_completion(client, expected_turn_id=None, timeout=180.0)
        summary = source.find_native_thread(
            native_id, source_kinds=("vscode", "appServer")
        )
        status["discover"] = summary is not None
        if summary is None:
            raise RuntimeError("codex_discovery_failed")
        projection = source.project_thread(summary)
        status["read"] = projection.native_id == native_id
        if not status["read"]:
            raise RuntimeError("codex_read_verification_failed")

        resume_started = time.monotonic()
        _resume_codex_characterization(
            client,
            native_id=native_id,
            resume_nonce=secrets.token_hex(16),
            request_timeout=45.0,
            verification_timeout=45.0,
            verification_poll_interval=0.25,
        )
        status["resume"] = True
        status["resume_latency_ms"] = (
            time.monotonic() - resume_started
        ) * 1000.0
    finally:
        status["total_latency_ms"] = (
            time.monotonic() - characterization_started
        ) * 1000.0
        if native_id is not None:
            if _codex_schema_advertises_archive(executable):
                try:
                    client.request(
                        "thread/archive", {"threadId": native_id}, timeout=30.0
                    )
                    status["cleanup"] = "archived"
                except Exception:
                    status["cleanup"] = "manual_archive_required"
            else:
                status["cleanup"] = "manual_archive_required"
        client.close()


def _resume_codex_characterization(
    client: Any,
    *,
    native_id: str,
    resume_nonce: str,
    request_timeout: float,
    verification_timeout: float,
    verification_poll_interval: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    completion_waiter: Callable[..., None] | None = None,
) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", resume_nonce):
        raise ValueError("Codex resume nonce must be 32 lowercase hex characters")
    prompt = (
        "Hermes Bridge live characterization resume verification tag "
        f"{resume_nonce}. Reply READY."
    )
    turn = client.request(
        "turn/start",
        {
            "threadId": native_id,
            "input": [{"type": "text", "text": prompt}],
        },
        timeout=request_timeout,
    )
    turn_id = _turn_id(turn)
    if completion_waiter is None:
        _wait_for_turn_completion(
            client, expected_turn_id=turn_id, timeout=180.0
        )
    else:
        completion_waiter(client, turn_id, 180.0)

    deadline = monotonic() + verification_timeout
    while True:
        read = client.request(
            "thread/read",
            {"threadId": native_id, "includeTurns": True},
            timeout=request_timeout,
        )
        thread = read.get("thread") if isinstance(read, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != native_id:
            raise RuntimeError("codex_resume_identity_mismatch")
        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise RuntimeError("codex_resume_read_malformed")
        if any(
            isinstance(observed, dict) and observed.get("id") == turn_id
            for observed in turns
        ):
            return turn_id
        if monotonic() >= deadline:
            raise RuntimeError("codex_resume_turn_not_found")
        sleep(verification_poll_interval)


def _provider_report() -> dict[str, Any]:
    return {
        "create": False,
        "discover": False,
        "read": False,
        "resume": False,
        "used_registration_turn": False,
        "cleanup": "not_started",
        "error_code": None,
    }


def _record_claude_failure_diagnostics(
    status: dict[str, Any], exc: PlaceholderCreationError
) -> None:
    for key in ("observed_cost_usd", "duration_ms"):
        value = getattr(exc, key, None)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= 0
        ):
            status[key] = float(value)
    num_turns = getattr(exc, "num_turns", None)
    if isinstance(num_turns, int) and not isinstance(num_turns, bool) and num_turns >= 0:
        status["num_turns"] = num_turns


def _claude_result_metrics(
    completed: subprocess.CompletedProcess[str],
) -> dict[str, int | float]:
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    metrics: dict[str, int | float] = {}
    for source_key, target_key in (
        ("total_cost_usd", "cost_usd"),
        ("duration_ms", "duration_ms"),
    ):
        value = payload.get(source_key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= 0
        ):
            metrics[target_key] = float(value)
    num_turns = payload.get("num_turns")
    if (
        isinstance(num_turns, int)
        and not isinstance(num_turns, bool)
        and num_turns >= 0
    ):
        metrics["num_turns"] = num_turns
    return metrics


def resolve_cli_executable(
    executable: str,
    *,
    which=shutil.which,
) -> str:
    if not isinstance(executable, str) or not executable.strip():
        raise ValueError("CLI executable must not be empty")
    normalized = executable.strip()
    resolved = which(normalized)
    return str(resolved) if resolved else normalized


def _wait_for_turn_completion(
    client: CodexAppServerClient,
    *,
    expected_turn_id: str | None,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        notification = client.take_notification(timeout=0.25)
        if not isinstance(notification, dict):
            continue
        if notification.get("method") != "turn/completed":
            continue
        params = notification.get("params")
        turn = params.get("turn") if isinstance(params, dict) else None
        observed = turn.get("id") if isinstance(turn, dict) else None
        if expected_turn_id is None or observed == expected_turn_id:
            return
    raise TimeoutError("codex_turn_completion_timeout")


def _turn_id(response: Any) -> str:
    if not isinstance(response, dict):
        raise RuntimeError("codex_turn_start_malformed")
    turn = response.get("turn")
    native_id = turn.get("id") if isinstance(turn, dict) else None
    if not isinstance(native_id, str) or not native_id:
        raise RuntimeError("codex_turn_start_missing_id")
    return native_id


def _codex_schema_advertises_archive(executable: str) -> bool:
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-codex-schema-") as directory:
            completed = subprocess.run(
                [
                    executable,
                    "app-server",
                    "generate-json-schema",
                    "--out",
                    directory,
                ],
                capture_output=True,
                text=True,
                timeout=60.0,
                stdin=subprocess.DEVNULL,
                shell=False,
                check=False,
            )
            if completed.returncode != 0:
                return False
            schema_path = Path(directory) / "ClientRequest.json"
            if not schema_path.is_file():
                return False
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            return "thread/archive" in _all_schema_strings(schema)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def _all_schema_strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        strings: set[str] = set()
        for key, item in value.items():
            strings.add(str(key))
            strings.update(_all_schema_strings(item))
        return strings
    if isinstance(value, list):
        strings: set[str] = set()
        for item in value:
            strings.update(_all_schema_strings(item))
        return strings
    return set()


def _cli_version(args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=15.0,
            stdin=subprocess.DEVNULL,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    match = re.search(r"\b\d+\.\d+\.\d+\b", completed.stdout)
    return match.group(0) if match else None


def _safe_error_code(provider: str, exc: Exception) -> str:
    if isinstance(exc, PlaceholderCreationError):
        return exc.code
    message = str(exc)
    if re.fullmatch(r"[a-z0-9_:-]{1,100}", message):
        return f"{provider}_{message}"[:120]
    return f"{provider}_{type(exc).__name__.lower()}"


def _sanitize_report_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.lower() in _SENSITIVE_REPORT_KEYS:
        return None
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_report_value(item, key=str(item_key))
            for item_key, item in value.items()
            if str(item_key).lower() not in _SENSITIVE_REPORT_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_report_value(item) for item in value]
    if isinstance(value, str):
        sanitized = value.replace(_MARKER_PREFIX, "[REDACTED_MARKER]:")
        return _SECRET_RE.sub("[REDACTED]", sanitized)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return type(value).__name__


def _canonical_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("characterization ID must be a UUID") from exc
    canonical = str(parsed)
    if not isinstance(value, str) or canonical != value.lower():
        raise ValueError("characterization ID must use canonical UUID syntax")
    return canonical
