from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any
import uuid

from agent.transports.codex_app_server import CodexAppServerClient
from hermes_constants import get_hermes_home

from .claude_adapter import (
    CLAUDE_PLACEHOLDER_MAX_BUDGET_USD,
    ClaudeMarkerSource,
    ClaudeReadableSource,
    ClaudeSourceAdapter,
    ClaudeTargetAdapter,
    PlaceholderCreationError,
    classify_claude_process_failure,
    resolve_claude_command,
)
from .codex_adapter import CodexSourceAdapter, CodexTargetAdapter
from .models import BridgeMarkerPayload, OriginKind, Provider, SessionProjection
from .claude_visibility import ClaudeVisibilityClaim


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
_MAX_CLI_VERSION_BYTES = 4096
_PROVIDER_REQUIRED_FIELDS = frozenset({
    "create",
    "discover",
    "read",
    "resume",
    "used_registration_turn",
    "cleanup",
    "error_code",
})
_PROVIDER_ALLOWED_FIELDS = {
    "claude": _PROVIDER_REQUIRED_FIELDS
    | {
        "native_id",
        "create_cost_usd",
        "create_latency_ms",
        "create_num_turns",
        "resume_cost_usd",
        "resume_latency_ms",
        "resume_num_turns",
        "total_cost_usd",
        "total_latency_ms",
        "total_num_turns",
        "observed_cost_usd",
        "duration_ms",
        "num_turns",
    },
    "codex": _PROVIDER_REQUIRED_FIELDS
    | {
        "native_id",
        "create_latency_ms",
        "resume_latency_ms",
        "total_latency_ms",
    },
}


def characterize_claude_visibility(
    *,
    source_root: Path,
    projects_root: Path,
    reserve: Callable[[SessionProjection], ClaudeVisibilityClaim],
    registrar: Any,
    restarted_source: Callable[[], ClaudeReadableSource],
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Register and safely remove one disposable native Claude mirror.

    The caller owns the durable reservation transaction and registrar.  This
    function deliberately calls each exactly once, then constructs a fresh
    source adapter to prove restart-safe, exact-ID discovery before deleting
    only the transcript whose complete identity has been verified.
    """

    root = Path(source_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    project_root = Path(projects_root).resolve(strict=True)
    if root.is_symlink() or project_root.is_symlink():
        raise RuntimeError("unsafe_characterization_root")
    disposable = Path(
        tempfile.mkdtemp(prefix="claude-visibility-", dir=str(root))
    ).resolve(strict=True)
    characterization_id = str(uuid.uuid4())
    timestamp = float(now())
    projection = SessionProjection(
        provider=Provider.CODEX,
        native_id=characterization_id,
        title="Claude native visibility characterization",
        cwd=str(disposable),
        started_at=timestamp,
        last_active=timestamp,
        messages=[
            # A real meaningful request keeps the disposable source on the
            # exact same eligibility path as production mirrors.
            # IDs are local characterization metadata, never provider state.
            _characterization_message(timestamp)
        ],
        native_path=str(disposable / "source.json"),
        native_hash="0" * 64,
        origin_kind=OriginKind.NATIVE,
    )
    claim = reserve(projection)
    if (
        not isinstance(claim, ClaudeVisibilityClaim)
        or not claim.claimed
        or claim.lease_kind != "launch"
        or claim.source_cwd != str(disposable)
        or claim.source_provider is not Provider.CODEX
        or not claim.reserved_claude_uuid
        or not claim.signed_marker
        or not claim.native_name
    ):
        raise RuntimeError("characterization_reservation_invalid")
    outcome = registrar.process(claim)
    if (
        getattr(outcome, "status", None) != "visible"
        or getattr(outcome, "reserved_claude_uuid", None) != claim.reserved_claude_uuid
    ):
        raise RuntimeError("characterization_registration_failed")

    restarted = restarted_source()
    finder = getattr(restarted, "find_native_sessions", None)
    paths = (
        list(finder(claim.reserved_claude_uuid))
        if callable(finder)
        else [
            found
            for found in [restarted.find_native_session(claim.reserved_claude_uuid)]
            if found is not None
        ]
    )
    if len(paths) != 1:
        raise RuntimeError("characterization_identity_mismatch:exact_uuid")
    transcript = Path(paths[0])
    try:
        resolved_transcript = transcript.resolve(strict=True)
        relative = resolved_transcript.relative_to(project_root)
    except (OSError, ValueError):
        raise RuntimeError("characterization_identity_mismatch:path") from None
    if (
        transcript.is_symlink()
        or not resolved_transcript.is_file()
        or relative.name != f"{claim.reserved_claude_uuid}.jsonl"
    ):
        raise RuntimeError("characterization_identity_mismatch:path")
    parsed = restarted.parse(transcript)
    native = parsed.projection
    try:
        projected_path = Path(native.native_path or "").resolve(strict=True)
    except OSError:
        raise RuntimeError("characterization_identity_mismatch:path") from None
    marker_check = getattr(restarted, "projection_has_exact_marker", None)
    exact_marker = callable(marker_check) and marker_check(native, claim.signed_marker)
    if (
        native.provider is not Provider.CLAUDE
        or native.native_id != claim.reserved_claude_uuid
        or native.title != claim.native_name
        or native.cwd != claim.source_cwd
        or projected_path != resolved_transcript
        or not exact_marker
    ):
        raise RuntimeError("characterization_identity_mismatch:metadata")

    # Recheck the directory entry immediately before unlinking.  Identity
    # changes fail closed and leave provider state untouched for diagnosis.
    if (
        transcript.resolve(strict=True) != resolved_transcript
        or transcript.is_symlink()
    ):
        raise RuntimeError("characterization_identity_mismatch:path_changed")
    transcript.unlink()
    shutil.rmtree(disposable)
    return {
        "passed": True,
        "source_provider": Provider.CODEX.value,
        "source_cwd": str(disposable),
        "reserved_claude_uuid": claim.reserved_claude_uuid,
        "native_name": claim.native_name,
        "restart_exact_id_verified": True,
        "operator_checks": [
            "Run /resume in Claude Code and select the deterministic characterization name.",
            "Press Ctrl+A in /resume to verify the exact session across all projects.",
            f"Resume the exact ID with: claude --resume {claim.reserved_claude_uuid}",
        ],
        "cleanup": "removed_exact_characterization",
    }


def _characterization_message(timestamp: float) -> Any:
    from .models import ProjectedMessage

    return ProjectedMessage(
        "characterization-request",
        0,
        "user",
        "Verify native Claude session visibility and exact-ID resume metadata.",
        timestamp,
    )


_PROVIDER_NUMBER_FIELDS = frozenset({
    "create_cost_usd",
    "create_latency_ms",
    "resume_cost_usd",
    "resume_latency_ms",
    "total_cost_usd",
    "total_latency_ms",
    "observed_cost_usd",
    "duration_ms",
})
_PROVIDER_INTEGER_FIELDS = frozenset({
    "create_num_turns",
    "resume_num_turns",
    "total_num_turns",
    "num_turns",
})
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


@dataclass(frozen=True)
class CharacterizationGate:
    """A passing newest characterization report for the installed CLIs."""

    report_path: Path
    characterization_id: str
    codex_registration_turn_required: bool


@dataclass(frozen=True)
class _ValidatedGateReport:
    path: Path
    report: dict[str, Any]
    created_at: datetime
    characterization_id: str
    codex_registration_turn_required: bool
    passed: bool


class CharacterizationGateError(RuntimeError):
    """Stable failure from the fail-closed live-characterization gate."""

    _CODES = frozenset({"missing", "invalid", "failed", "version_drift"})

    def __init__(self, code: str, detail: str) -> None:
        if code not in self._CODES:
            raise ValueError("invalid characterization gate error code")
        self.code = code
        super().__init__(detail)


def resolve_characterization_gate(
    *,
    report_root: Path | None = None,
    current_versions: Mapping[str, str] | None = None,
) -> CharacterizationGate:
    """Require the newest report to pass exactly for the installed CLI versions."""

    root = (
        Path(
            report_root
            if report_root is not None
            else get_hermes_home() / "session-bridge" / "characterization"
        )
        .expanduser()
        .absolute()
    )
    _require_safe_report_root(root)
    latest = max(
        _read_validated_gate_reports(root),
        key=lambda candidate: (candidate.created_at, candidate.characterization_id),
    )
    if not latest.passed:
        raise CharacterizationGateError("failed", "characterization_report_failed")
    observed_versions = (
        _current_cli_versions()
        if current_versions is None
        else _validated_version_mapping(current_versions, source="current")
    )
    report_versions = _validated_version_mapping(
        latest.report["versions"], source="report"
    )
    if report_versions != observed_versions:
        raise CharacterizationGateError(
            "version_drift", "characterization_version_mismatch"
        )
    return CharacterizationGate(
        report_path=latest.path,
        characterization_id=latest.characterization_id,
        codex_registration_turn_required=latest.codex_registration_turn_required,
    )


def _require_safe_report_root(root: Path) -> None:
    for candidate in (root, *root.parents):
        if _path_is_redirect(candidate):
            raise CharacterizationGateError("invalid", "characterization_report_unsafe")
    try:
        metadata = os.lstat(root)
    except FileNotFoundError:
        raise CharacterizationGateError(
            "missing", "characterization_report_missing"
        ) from None
    except OSError:
        raise CharacterizationGateError(
            "invalid", "characterization_report_unsafe"
        ) from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise CharacterizationGateError("invalid", "characterization_report_unsafe")


def _read_validated_gate_reports(root: Path) -> list[_ValidatedGateReport]:
    try:
        paths = sorted(
            (path for path in root.iterdir() if path.suffix == ".json"),
            key=lambda path: path.name,
        )
    except OSError:
        raise CharacterizationGateError(
            "invalid", "characterization_report_unsafe"
        ) from None
    if not paths:
        raise CharacterizationGateError("missing", "characterization_report_missing")
    return [
        _validate_gate_report(_read_report_safely(path), report_path=path)
        for path in paths
    ]


def _read_report_safely(path: Path) -> dict[str, Any]:
    try:
        before = os.lstat(path)
    except OSError:
        raise CharacterizationGateError(
            "invalid", "characterization_report_unsafe"
        ) from None
    if _path_is_redirect(path) or not stat.S_ISREG(before.st_mode):
        raise CharacterizationGateError("invalid", "characterization_report_unsafe")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size > 1_048_576
        ):
            raise CharacterizationGateError("invalid", "characterization_report_unsafe")
        payload = os.read(descriptor, opened.st_size + 1)
        after = os.fstat(descriptor)
        current = os.lstat(path)
        current_attributes = getattr(current, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(current.st_mode)
            or bool(current_attributes & reparse_flag)
            or not stat.S_ISREG(current.st_mode)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
            or current.st_size != opened.st_size
            or current.st_mtime_ns != opened.st_mtime_ns
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or len(payload) != opened.st_size
        ):
            raise CharacterizationGateError("invalid", "characterization_report_unsafe")
    except CharacterizationGateError:
        raise
    except OSError:
        raise CharacterizationGateError(
            "invalid", "characterization_report_unsafe"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        report = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_json_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey):
        raise CharacterizationGateError(
            "invalid", "characterization_report_malformed"
        ) from None
    if not isinstance(report, dict):
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    return report


class _DuplicateJsonKey(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(key)
        value[key] = item
    return value


def _validate_gate_report(
    report: dict[str, Any], *, report_path: Path
) -> _ValidatedGateReport:
    expected_keys = {
        "schema_version",
        "characterization_id",
        "created_at",
        "automatic_mirroring_enabled",
        "versions",
        "providers",
    }
    if set(report) != expected_keys or type(report["schema_version"]) is not int:
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    if (
        report["schema_version"] != 1
        or report["automatic_mirroring_enabled"] is not False
    ):
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    try:
        characterization_id = _canonical_uuid(report["characterization_id"])
    except ValueError:
        raise CharacterizationGateError(
            "invalid", "characterization_report_malformed"
        ) from None
    if report_path.stem != characterization_id:
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    created_at = report["created_at"]
    try:
        created = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        raise CharacterizationGateError(
            "invalid", "characterization_report_malformed"
        ) from None
    if created.tzinfo is None or created.utcoffset() is None:
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    _validated_version_mapping(report["versions"], source="report")
    providers = report["providers"]
    if not isinstance(providers, dict) or set(providers) != {"claude", "codex"}:
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    claude_registration, claude_passed = _validate_provider_status(
        providers["claude"], provider="claude"
    )
    codex_registration, codex_passed = _validate_provider_status(
        providers["codex"], provider="codex"
    )
    if claude_registration:
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    return _ValidatedGateReport(
        path=report_path,
        report=report,
        created_at=created.astimezone(timezone.utc),
        characterization_id=characterization_id,
        codex_registration_turn_required=codex_registration,
        passed=claude_passed and codex_passed,
    )


def _validate_provider_status(value: Any, *, provider: str) -> tuple[bool, bool]:
    allowed = _PROVIDER_ALLOWED_FIELDS[provider]
    if (
        not isinstance(value, dict)
        or not _PROVIDER_REQUIRED_FIELDS.issubset(value)
        or not set(value).issubset(allowed)
    ):
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    if type(value["used_registration_turn"]) is not bool:
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    if (
        not isinstance(value["cleanup"], str)
        or not value["cleanup"]
        or value["cleanup"] != value["cleanup"].strip()
        or "\n" in value["cleanup"]
        or "\r" in value["cleanup"]
    ):
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    for stage in ("create", "discover", "read", "resume"):
        if type(value[stage]) is not bool:
            raise CharacterizationGateError(
                "invalid", "characterization_report_malformed"
            )
    error_code = value["error_code"]
    if error_code is not None and (
        not isinstance(error_code, str)
        or not error_code
        or error_code != error_code.strip()
        or "\n" in error_code
        or "\r" in error_code
    ):
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    native_id = value.get("native_id")
    if native_id is not None:
        try:
            _canonical_uuid(native_id)
        except ValueError:
            raise CharacterizationGateError(
                "invalid", "characterization_report_malformed"
            ) from None
    for field in _PROVIDER_NUMBER_FIELDS & value.keys():
        field_value = value[field]
        if field_value is not None and (
            not isinstance(field_value, (int, float))
            or isinstance(field_value, bool)
            or not math.isfinite(float(field_value))
            or float(field_value) < 0
        ):
            raise CharacterizationGateError(
                "invalid", "characterization_report_malformed"
            )
    for field in _PROVIDER_INTEGER_FIELDS & value.keys():
        field_value = value[field]
        if field_value is not None and (
            not isinstance(field_value, int)
            or isinstance(field_value, bool)
            or field_value < 0
        ):
            raise CharacterizationGateError(
                "invalid", "characterization_report_malformed"
            )
    passed = (
        all(value[stage] is True for stage in ("create", "discover", "read", "resume"))
        and error_code is None
    )
    return value["used_registration_turn"], passed


def _validated_version_mapping(value: Any, *, source: str) -> dict[str, str]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"claude", "codex"}
        or not all(isinstance(value[key], str) and value[key] for key in value)
    ):
        code = "invalid" if source == "report" else "version_drift"
        detail = (
            "characterization_report_malformed"
            if source == "report"
            else "characterization_version_unavailable"
        )
        raise CharacterizationGateError(code, detail)
    return {key: value[key] for key in ("claude", "codex")}


def _current_cli_versions() -> dict[str, str]:
    try:
        claude = _cli_version([*resolve_cli_executable("claude"), "--version"])
        codex = _cli_version([*resolve_cli_executable("codex"), "--version"])
    except (RuntimeError, ValueError):
        claude = codex = None
    if claude is None or codex is None:
        raise CharacterizationGateError(
            "version_drift", "characterization_version_unavailable"
        )
    return {"claude": claude, "codex": codex}


def write_characterization_report(
    report: Mapping[str, Any],
    *,
    report_root: Path | None = None,
    characterization_id: str,
) -> Path:
    record_id = _canonical_uuid(characterization_id)
    resolved_report_root = (
        Path(report_root)
        if report_root is not None
        else get_hermes_home() / "session-bridge" / "characterization"
    )
    root = _safe_directory_root(
        resolved_report_root.expanduser(),
        error_code="unsafe_characterization_report",
    )
    report_path = root / f"{record_id}.json"
    if report_path.exists() or _path_is_redirect(report_path):
        raise RuntimeError("unsafe_characterization_report:final_exists")
    sanitized = _sanitize_report_value(dict(report))
    payload = json.dumps(
        sanitized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{record_id}.", suffix=".tmp", dir=root
        )
        temporary = Path(temporary_name)
        if temporary.parent.resolve() != root.resolve() or _path_is_redirect(temporary):
            raise RuntimeError("unsafe_characterization_report:temp_redirect")
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if report_path.exists() or _path_is_redirect(report_path):
            raise RuntimeError("unsafe_characterization_report:final_exists")
        os.link(temporary, report_path, follow_symlinks=False)
        temporary.unlink()
    except RuntimeError:
        raise
    except (OSError, ValueError) as exc:
        raise RuntimeError("unsafe_characterization_report:write_failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)
    return report_path


def _safe_directory_root(path: Path, *, error_code: str) -> Path:
    root = path.absolute()
    for candidate in (root, *root.parents):
        if _path_is_redirect(candidate):
            raise RuntimeError(f"{error_code}:redirect_parent")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise RuntimeError(f"{error_code}:root_unavailable") from None
    if _path_is_redirect(root) or not root.is_dir():
        raise RuntimeError(f"{error_code}:unsafe_root")
    return root


def _path_is_redirect(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def quarantine_claude_transcript(
    source_adapter: ClaudeMarkerSource,
    *,
    native_id: str,
    bridge_id: str,
    source_session_id: str,
    policy_generation: int,
    projects_root: Path = _CLAUDE_PROJECTS_ROOT,
    quarantine_root: Path | None = None,
) -> Path:
    expected_id = _canonical_uuid(native_id)
    if not isinstance(bridge_id, str) or not bridge_id.strip():
        raise UnsafeCharacterizationCleanup("bridge identity is missing")
    expected_payload = BridgeMarkerPayload(
        bridge_id=bridge_id.strip(),
        source_session_id=source_session_id,
        target_provider=Provider.CLAUDE,
        policy_generation=policy_generation,
    )
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
        or not source_adapter.projection_has_marker_payload(
            projection, expected_payload
        )
    ):
        raise UnsafeCharacterizationCleanup("Claude signed marker mismatch")

    destination_root = (
        Path(quarantine_root).expanduser()
        if quarantine_root is not None
        else get_hermes_home() / "session-bridge" / "characterization" / "quarantine"
    )
    try:
        destination_root = _safe_directory_root(
            destination_root, error_code="unsafe_claude_quarantine"
        )
    except RuntimeError:
        raise UnsafeCharacterizationCleanup(
            "Claude quarantine parent is a symlink or unsafe"
        ) from None
    destination = destination_root / f"{expected_id}.jsonl"
    if destination.exists() or _path_is_redirect(destination):
        raise UnsafeCharacterizationCleanup("Claude quarantine target already exists")
    descriptor = -1
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with candidate.open("rb") as source, os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        candidate.unlink()
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        destination.unlink(missing_ok=True)
        raise UnsafeCharacterizationCleanup(
            "Claude quarantine move failed safely"
        ) from None
    return destination


def run_live_characterization(
    *,
    report_root: Path | None = None,
    claude_projects_root: Path = _CLAUDE_PROJECTS_ROOT,
    claude_executable: str = "claude",
    codex_executable: str = "codex",
    cwd: Path | None = None,
) -> Path:
    if os.environ.get("HERMES_SESSION_BRIDGE_LIVE_TESTS") != "1":
        raise RuntimeError("live_characterization_not_enabled")
    claude_command = resolve_cli_executable(claude_executable)
    codex_command = resolve_cli_executable(codex_executable)
    claude_version = _cli_version([*claude_command, "--version"])
    codex_version = _cli_version([*codex_command, "--version"])
    if claude_version is None:
        raise RuntimeError("claude_cli_preflight_failed")
    if codex_version is None:
        raise RuntimeError("codex_cli_preflight_failed")
    characterization_id = str(uuid.uuid4())
    resolved_report_root = (
        Path(report_root)
        if report_root is not None
        else get_hermes_home() / "session-bridge" / "characterization"
    )
    title = f"[Hermes Bridge Characterization] {characterization_id}"
    marker_secret = secrets.token_bytes(32)
    working_directory = Path(cwd or Path.cwd()).resolve()
    report: dict[str, Any] = {
        "schema_version": 1,
        "characterization_id": characterization_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "automatic_mirroring_enabled": False,
        "versions": {
            "claude": claude_version,
            "codex": codex_version,
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
            report_root=resolved_report_root,
            executable=claude_command,
            cwd=working_directory,
        )
    except Exception as exc:
        code = _safe_error_code("claude", exc)
        report["providers"]["claude"]["error_code"] = code
        if isinstance(exc, PlaceholderCreationError):
            _record_claude_failure_diagnostics(report["providers"]["claude"], exc)
        failures.append(code)
    try:
        _characterize_codex(
            report["providers"]["codex"],
            characterization_id=characterization_id,
            title=title,
            marker_secret=marker_secret,
            executable=codex_command,
            cwd=working_directory,
        )
    except Exception as exc:
        code = _safe_error_code("codex", exc)
        report["providers"]["codex"]["error_code"] = code
        failures.append(code)

    report_path = write_characterization_report(
        report,
        report_root=resolved_report_root,
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
    executable: str | Sequence[str],
    cwd: Path,
) -> None:
    native_id = str(uuid.uuid4())
    bridge_id = f"characterization-{characterization_id}-claude"
    source_session_id = f"codex:characterization-{characterization_id}"
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
            source_session_id=source_session_id,
            bridge_id=bridge_id,
            policy_generation=1,
            cwd=cwd,
        )
        create_elapsed_ms = (time.monotonic() - creation_started) * 1000.0
        create_metrics = (
            _claude_result_metrics(creation_processes[-1]) if creation_processes else {}
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
                source_session_id=source_session_id,
                policy_generation=1,
                projects_root=projects_root,
                quarantine_root=report_root / "quarantine",
            )
            status["cleanup"] = "quarantined"
        except UnsafeCharacterizationCleanup:
            status["cleanup"] = "not_moved_safety_check"


def _resume_claude_characterization(
    source: ClaudeReadableSource,
    *,
    baseline_projection: SessionProjection,
    native_id: str,
    bridge_id: str,
    resume_nonce: str,
    executable: str | Sequence[str],
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
        *_immutable_argv_prefix(executable, label="Claude executable"),
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
    runner_failure: PlaceholderCreationError | None = None
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
    except FileNotFoundError:
        runner_failure = PlaceholderCreationError("claude_resume_executable_not_found")
    except Exception:
        runner_failure = PlaceholderCreationError("claude_resume_process_failed")
    else:
        metrics = _claude_result_metrics(completed)
        if completed.returncode != 0:
            process_code = classify_claude_process_failure(completed)
            suffix = process_code.removeprefix("claude_process_")
            process_failure = _claude_resume_error(f"claude_resume_{suffix}", metrics)
    if runner_failure is not None:
        raise runner_failure

    deadline = monotonic() + verification_timeout
    last_code = "claude_resume_target_not_found"
    while True:
        path = source.find_native_session(native_id)
        if path is not None:
            parse_failure: PlaceholderCreationError | None = None
            try:
                projection = source.parse(path).projection
            except Exception:
                projection = None
                parse_failure = _claude_resume_error(
                    "claude_resume_target_unreadable", metrics
                )
            if parse_failure is not None:
                raise parse_failure
            assert projection is not None
            if projection.native_id != native_id:
                raise _claude_resume_error("claude_resume_identity_mismatch", metrics)
            if (
                projection.origin_bridge_id != bridge_id
                or projection.origin_kind
                not in (
                    OriginKind.BRIDGE_PLACEHOLDER,
                    OriginKind.BRIDGE_CONTINUATION,
                )
            ):
                raise _claude_resume_error("claude_resume_marker_mismatch", metrics)

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
    executable: Sequence[str],
    cwd: Path,
) -> None:
    characterization_started = time.monotonic()
    codex_bin = _single_native_executable(executable, label="Codex")
    client = CodexAppServerClient(codex_bin=codex_bin)
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
            status["create_latency_ms"] = (time.monotonic() - create_started) * 1000.0
        except PlaceholderCreationError as exc:
            native_id = exc.native_id
            if native_id is not None:
                status["native_id"] = native_id
            raise
        native_id = result.native_id
        status["native_id"] = native_id
        status["create"] = True
        status["used_registration_turn"] = result.used_registration_turn
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
        status["resume_latency_ms"] = (time.monotonic() - resume_started) * 1000.0
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
    baseline_turns = _read_codex_turns(
        client,
        native_id=native_id,
        request_timeout=request_timeout,
        stage="baseline",
    )
    baseline_ids = {
        turn_id
        for turn in baseline_turns
        if (turn_id := _nonempty_mapping_text(turn, "id")) is not None
    }
    prompt = (
        "Hermes Bridge live characterization resume verification tag "
        f"{resume_nonce}. Reply READY."
    )
    start_failure: RuntimeError | None = None
    try:
        turn = client.request(
            "turn/start",
            {
                "threadId": native_id,
                "input": [{"type": "text", "text": prompt}],
            },
            timeout=request_timeout,
        )
    except Exception:
        turn = None
        start_failure = RuntimeError("codex_resume_turn_start_failed")
    if start_failure is not None:
        raise start_failure
    turn_id = _turn_identity(turn)
    if turn_id in baseline_ids:
        raise RuntimeError("codex_resume_turn_preexisting")
    if completion_waiter is None:
        _wait_for_turn_completion(
            client,
            expected_thread_id=native_id,
            expected_turn_id=turn_id,
            timeout=180.0,
        )
    else:
        completion_waiter(client, native_id, turn_id, 180.0)

    deadline = monotonic() + verification_timeout
    while True:
        turns = _read_codex_turns(
            client,
            native_id=native_id,
            request_timeout=request_timeout,
            stage="post_resume",
        )
        exact_turns = [
            observed
            for observed in turns
            if _nonempty_mapping_text(observed, "id") == turn_id
        ]
        if len(exact_turns) > 1:
            raise RuntimeError("codex_resume_turn_conflict")
        if exact_turns:
            durable_status = _nonempty_mapping_text(exact_turns[0], "status")
            if durable_status not in {"completed", "inProgress"}:
                raise RuntimeError("codex_resume_turn_not_completed")
            if durable_status == "completed":
                if not _codex_turn_user_input_has_nonce(
                    exact_turns[0], nonce=resume_nonce
                ):
                    raise RuntimeError("codex_resume_nonce_mismatch")
                return turn_id
        if monotonic() >= deadline:
            raise RuntimeError("codex_resume_turn_not_found")
        sleep(verification_poll_interval)


def _read_codex_turns(
    client: Any,
    *,
    native_id: str,
    request_timeout: float,
    stage: str,
) -> list[dict[str, Any]]:
    read_failure: RuntimeError | None = None
    try:
        read = client.request(
            "thread/read",
            {"threadId": native_id, "includeTurns": True},
            timeout=request_timeout,
        )
    except Exception:
        read = None
        read_failure = RuntimeError(f"codex_resume_{stage}_read_failed")
    if read_failure is not None:
        raise read_failure
    thread = read.get("thread") if isinstance(read, dict) else None
    if not isinstance(thread, dict) or thread.get("id") != native_id:
        raise RuntimeError("codex_resume_identity_mismatch")
    turns = thread.get("turns")
    if not isinstance(turns, list) or not all(isinstance(turn, dict) for turn in turns):
        raise RuntimeError("codex_resume_read_malformed")
    return turns


def _nonempty_mapping_text(value: Any, key: str) -> str | None:
    if not isinstance(value, dict):
        return None
    observed = value.get(key)
    return observed if isinstance(observed, str) and observed else None


def _codex_turn_user_input_has_nonce(turn: dict[str, Any], *, nonce: str) -> bool:
    items = turn.get("items")
    if not isinstance(items, list):
        return False
    occurrences = 0
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "userMessage":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str):
                occurrences += text.count(nonce)
    return occurrences == 1


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
    if (
        isinstance(num_turns, int)
        and not isinstance(num_turns, bool)
        and num_turns >= 0
    ):
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
) -> tuple[str, ...]:
    if not isinstance(executable, str) or not executable.strip():
        raise ValueError("CLI executable must not be empty")
    normalized = executable.strip()
    resolved = str(which(normalized) or normalized)
    candidate = Path(resolved).expanduser()
    suffix = candidate.suffix.casefold()
    if candidate.stem.casefold() == "claude":
        return resolve_claude_command(normalized, which=which)
    if suffix not in {".cmd", ".ps1", ".bat"}:
        return (str(candidate.resolve()) if candidate.exists() else resolved,)

    command_name = candidate.stem.casefold()
    if command_name == "codex":
        if suffix == ".cmd" and candidate.is_file():
            return (str(candidate.resolve()),)
        raise RuntimeError("unsupported_shell_shim")
    raise RuntimeError("unsupported_shell_shim")


def _immutable_argv_prefix(
    command: str | Sequence[str], *, label: str
) -> tuple[str, ...]:
    entries: Sequence[str] = (command,) if isinstance(command, str) else command
    if not entries:
        raise ValueError(f"{label} must not be empty")
    normalized: list[str] = []
    for entry in entries:
        if (
            not isinstance(entry, str)
            or not entry.strip()
            or "\r" in entry
            or "\n" in entry
        ):
            raise ValueError(f"{label} entries must be non-empty and single-line")
        normalized.append(entry.strip())
    return tuple(normalized)


def _wait_for_turn_completion(
    client: CodexAppServerClient,
    *,
    expected_thread_id: str,
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


def _turn_identity(response: Any) -> str:
    if not isinstance(response, dict):
        raise RuntimeError("codex_turn_start_malformed")
    turn = response.get("turn")
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        raise RuntimeError("codex_turn_start_missing_id")
    return turn_id


def _single_native_executable(command: Sequence[str], *, label: str) -> str:
    if len(command) != 1:
        raise RuntimeError(f"{label.casefold()}_direct_runtime_required")
    executable = command[0]
    if not isinstance(executable, str) or not executable.strip():
        raise RuntimeError(f"{label.casefold()}_executable_invalid")
    return executable


def _codex_schema_advertises_archive(executable: Sequence[str]) -> bool:
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-codex-schema-") as directory:
            completed = subprocess.run(
                [
                    *executable,
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
    stdout = completed.stdout
    if not isinstance(stdout, str):
        return None
    normalized = stdout.replace("\r\n", "\n").replace("\r", "\n").strip()
    if (
        not normalized
        or "\x00" in normalized
        or len(normalized.encode("utf-8")) > _MAX_CLI_VERSION_BYTES
    ):
        return None
    return normalized


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
