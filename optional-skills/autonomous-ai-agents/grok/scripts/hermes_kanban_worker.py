#!/usr/bin/env python3
"""Deterministic, opt-in Hermes Kanban adapter for the Grok CLI."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import re
import shlex
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple, Sequence


PROFILE = "worker-grok-cli"
RIGHTCODE_GROK_BASE_URL = "https://rightapi.ai/grok/v1"
# Match agent-client-protocol 0.9.0's bounded stdio default. Its lower-level
# spawn helper otherwise falls back to asyncio's 64 KiB StreamReader limit.
ACP_STREAM_LIMIT_BYTES = 50 * 1024 * 1024
ACP_RUN_ENV_KEY = "HERMES_GROK_ACP_RUN_ID"
ADAPTER_RUN_ENV_KEY = "HERMES_GROK_WORKER_RUN_ID"
CLAIM_READY_TIMEOUT_SECONDS = 5.0
CLAIM_READY_POLL_SECONDS = 0.05
CLAIM_READY_STABLE_POLLS = 4
VERIFICATION_MUTATION_PATH_LIMIT = 100
WORKER_RULES = """This session is a delegated implementation-worker run.
The upstream Foreman already owns and has authorized the task lifecycle. Editing
files inside the supplied cwd and running the card's tests are explicitly
authorized. Repository instructions about claiming a GitHub Issue, creating or
owning a branch or Pull Request, posting lifecycle comments, committing, pushing,
or operating Hermes Kanban are delegated to the Foreman and must not be treated as
blockers to the requested in-place implementation. Follow every other applicable
repository instruction, especially scope, code-quality, security, and testing
requirements. Do not write to GitHub, commit, push, or invoke Hermes Kanban. Leave
the requested implementation as an uncommitted diff. Do not block merely because
the delegated lifecycle actions are unavailable to you."""
REVIEW_RULES = """This session is a delegated read-only security reviewer run.
Review only the implementation SHA pinned in the Kanban card context. Do not edit,
format, generate, delete, or otherwise mutate repository files. Do not commit, push,
open or modify a Pull Request, merge, or invoke Hermes Kanban; the deterministic
adapter owns terminal state. Read-only applies to repository and lifecycle mutations;
running tests, static analysis, catalog queries, and disposable database probes is
explicitly authorized and is the purpose of the review. Do not block merely because
the review is read-only. Treat a missing or unverifiable pinned SHA as a blocked
dependency, never as PASS. Base every verdict on concrete evidence, and verify the
workspace has no changes caused by this review before returning the report. Follow
all repository scope, security, and testing instructions that are compatible with
this read-only boundary."""
REPORT_SCHEMA: dict[str, Any] = {
    # Grok CLI 1.0.4 returns no structuredOutput when this object gains a
    # top-level conditional anyOf. Keep cross-field terminal-state invariants
    # explicit in the report prompt and authoritative in validate_report().
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "summary",
        "changed_files",
        "tests",
        "risks",
        "evidence",
        "block_reason",
        "block_kind",
    ],
    "properties": {
        "status": {
            "type": "string",
            "enum": ["completed", "blocked"],
        },
        "summary": {"type": "string", "minLength": 1},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["command", "outcome", "details"],
                "properties": {
                    "command": {"type": "string"},
                    "outcome": {
                        "type": "string",
                        "enum": ["passed", "failed", "not_run"],
                    },
                    "details": {"type": "string"},
                },
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "block_reason": {"type": "string"},
        "block_kind": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "string",
                    "enum": ["dependency", "needs_input", "transient"],
                },
            ],
        },
    },
}


class AdapterError(RuntimeError):
    """A safe-to-report adapter failure."""


class ReportContractError(AdapterError):
    """A model-authored report may be corrected within the live ACP session."""


class CapabilityError(AdapterError):
    """A required local or provider capability is unavailable."""


class GrokTimeout(AdapterError):
    pass


class GrokNoProgress(AdapterError):
    """The live agent made no observable workspace progress before its deadline."""


class GrokCapabilityError(CapabilityError):
    pass


class GrokProviderError(AdapterError):
    """A bounded, retryable upstream provider failure."""

    retryable = True

    def __init__(self, evidence: Mapping[str, Any]) -> None:
        self.evidence = dict(evidence)
        encoded = json.dumps(self.evidence, ensure_ascii=False, separators=(",", ":"))
        super().__init__(f"Retryable Grok provider failure; stderr_evidence={encoded}")


class WorkspaceReadinessError(AdapterError):
    """A labeled Git-probe failure during workspace readiness."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        self.retryable = retryable
        super().__init__(message)


class AcceptanceContractError(AdapterError):
    """The adapter-owned acceptance contract is absent or malformed."""


class ProjectContextCapabilityError(CapabilityError):
    pass


class TerminalStateConflict(AdapterError):
    """A concurrent actor already chose a different terminal state."""


STDERR_PREVIEW_CHARS = 4_096


def _redact_stderr(text: str) -> str:
    """Remove common credential forms from process diagnostics."""
    text = re.sub(
        r"(?i)\b(authorization\s*:\s*bearer)\s+[^\s,;]+",
        r"\1 [REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD)"
        r"[A-Z0-9_]*)\s*([=:])\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)",
        r"\1\2[REDACTED]",
        text,
    )
    return re.sub(
        r"(?i)\b(?:sk|xai)-[A-Z0-9_-]{8,}",
        "[REDACTED]",
        text,
    )


def _stderr_evidence(stderr: str) -> dict[str, Any]:
    raw = stderr.encode("utf-8", errors="replace")
    sanitized = _redact_stderr(stderr)
    truncated = len(sanitized) > STDERR_PREVIEW_CHARS
    if truncated:
        marker = "\n...[sanitized stderr truncated]...\n"
        remaining = STDERR_PREVIEW_CHARS - len(marker)
        head_chars = remaining // 2
        tail_chars = remaining - head_chars
        sanitized = sanitized[:head_chars] + marker + sanitized[-tail_chars:]
    return {
        "bytes": len(raw),
        "chars": len(stderr),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "truncated": truncated,
        "preview": sanitized,
    }


def _report_contract_evidence(report: Mapping[str, Any]) -> dict[str, Any]:
    """Describe an invalid report without retaining model-authored text."""
    status = report.get("status")
    safe_status = (
        status
        if isinstance(status, str) and status in {"completed", "blocked"}
        else f"<{type(status).__name__}>"
    )
    block_kind = report.get("block_kind")
    safe_block_kind = (
        block_kind
        if block_kind is None
        or (
            isinstance(block_kind, str)
            and block_kind in {"dependency", "needs_input", "transient"}
        )
        else f"<{type(block_kind).__name__}>"
    )
    block_reason = report.get("block_reason")
    evidence: dict[str, Any] = {
        "status": safe_status,
        "block_kind": safe_block_kind,
    }
    if isinstance(block_reason, str):
        raw = block_reason.encode("utf-8", errors="replace")
        evidence.update({
            "block_reason_bytes": len(raw),
            "block_reason_chars": len(block_reason),
            "block_reason_sha256": hashlib.sha256(raw).hexdigest(),
        })
    else:
        evidence["block_reason_type"] = type(block_reason).__name__
    tests = report.get("tests")
    if isinstance(tests, list):
        outcomes = {"passed": 0, "failed": 0, "not_run": 0, "invalid": 0}
        for test in tests:
            outcome = test.get("outcome") if isinstance(test, Mapping) else None
            key = outcome if outcome in {"passed", "failed", "not_run"} else "invalid"
            outcomes[key] += 1
        evidence["tests_count"] = len(tests)
        evidence["test_outcomes"] = outcomes
    else:
        evidence["tests_type"] = type(tests).__name__
    return evidence


class GrokProcessExit(AdapterError):
    """A non-auth process exit with bounded, sanitized diagnostic evidence."""

    def __init__(
        self,
        returncode: int,
        evidence: Mapping[str, Any],
        *,
        phase: str | None = None,
    ) -> None:
        self.returncode = returncode
        self.evidence = dict(evidence)
        self.phase = phase
        phase_text = f"; phase={phase}" if phase else ""
        encoded = json.dumps(
            self.evidence,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        super().__init__(
            f"Grok CLI exited with code {returncode}{phase_text}; "
            f"stderr_evidence={encoded}"
        )

    def with_phase(self, phase: str) -> GrokProcessExit:
        return GrokProcessExit(self.returncode, self.evidence, phase=phase)


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
GrokRunner = Callable[..., str]
WorkspaceSnapshot = dict[str, str]


class AcpWorkResult(NamedTuple):
    """Bounded facts returned by the official ACP work session."""

    session_id: str
    stop_reason: str
    update_count: int
    tool_call_count: int
    max_update_json_bytes: int = 0
    update_sequence: tuple[int, ...] = ()
    report: dict[str, Any] | None = None
    acceptance: dict[str, Any] | None = None


class AcceptanceCommand(NamedTuple):
    label: str
    argv: tuple[str, ...]
    timeout: float


class ProviderRoute(NamedTuple):
    name: str
    endpoint_env: str
    key_env: str


def grok_exit_error(
    returncode: int,
    stdout: str,
    stderr: str,
    *,
    phase: str | None = None,
) -> AdapterError:
    """Classify a failed Grok process without forwarding provider details."""
    details = f"{stdout}\n{stderr}".casefold()
    auth_markers = (
        "not signed in",
        "authentication failed",
        "authentication required",
        "not authenticated",
        "unauthorized (401)",
        "invalid api key",
        "无效的api key",
        "session expired",
        "token_expired",
        "no login method available",
        "no api key is configured",
    )
    auth_status = re.search(
        r"\b(?:http(?:/\d(?:\.\d)?)?|status(?:[_ -]?code)?)"
        r"\s*(?:[:=]\s*)?401\b",
        details,
    )
    if auth_status or any(marker in details for marker in auth_markers):
        return GrokCapabilityError("Grok authentication capability is unavailable")
    provider_markers = (
        "no responsecompleted or responseincomplete event received from responses api",
        "internal server error",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
    )
    provider_status = re.search(
        r"\b(?:http(?:/\d(?:\.\d)?)?|status(?:[_ -]?code)?)"
        r"\s*(?:[:=]\s*)?5\d\d\b",
        details,
    )
    if provider_status or any(marker in details for marker in provider_markers):
        diagnostic = "\n".join(part for part in (stdout, stderr) if part)
        return GrokProviderError(_stderr_evidence(diagnostic))
    return GrokProcessExit(returncode, _stderr_evidence(stderr), phase=phase)


def run_command(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        env=dict(env),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _verification_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Remove provider and credential-shaped variables from verify children."""
    secret_name = re.compile(
        r"(?i)(?:api[_-]?key|token|secret|password|private[_-]?key)"
    )
    return {
        key: value
        for key, value in source.items()
        if key not in {"RIGHTCODE_API_KEY", "RIGHTCODE_GROK_API_KEY", "XAI_API_KEY"}
        and not secret_name.search(key)
    }


def _text_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def parse_acceptance_commands(
    raw_commands: Sequence[str],
) -> tuple[AcceptanceCommand, ...]:
    """Parse repeated JSON descriptors without accepting credential values or shells."""
    if not raw_commands:
        raise AcceptanceContractError(
            "At least one structured adapter-owned acceptance command is required"
        )
    parsed: list[AcceptanceCommand] = []
    for index, raw in enumerate(raw_commands):
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise AcceptanceContractError(
                f"Acceptance command {index} is not valid JSON"
            ) from exc
        if not isinstance(value, dict) or set(value) != {"label", "argv", "timeout"}:
            raise AcceptanceContractError(
                f"Acceptance command {index} must contain label, argv, and timeout"
            )
        label = value["label"]
        argv = value["argv"]
        timeout = value["timeout"]
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise AcceptanceContractError(
                f"Acceptance command {index} has an invalid label"
            )
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > 128
            or not all(
                isinstance(item, str) and item and len(item) <= 4096 for item in argv
            )
        ):
            raise AcceptanceContractError(
                f"Acceptance command {index} has invalid argv"
            )
        if any(re.fullmatch(r"[();<>|&]+", token) for token in argv):
            raise AcceptanceContractError(
                f"Acceptance command {index} contains shell operators"
            )
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise AcceptanceContractError(
                f"Acceptance command {index} has invalid timeout"
            )
        bounded_timeout = float(timeout)
        if not 0.1 <= bounded_timeout <= 3600:
            raise AcceptanceContractError(
                f"Acceptance command {index} timeout must be between 0.1 and 3600 seconds"
            )
        parsed.append(AcceptanceCommand(label.strip(), tuple(argv), bounded_timeout))
    return tuple(parsed)


def provider_route_environments(
    base_env: Mapping[str, str], raw_routes: Sequence[str]
) -> tuple[tuple[str, dict[str, str]], ...]:
    """Resolve route descriptors exclusively through named environment variables."""
    if not raw_routes:
        return (("default", dict(base_env)),)
    if len(raw_routes) > 4:
        raise AcceptanceContractError("At most four provider routes are allowed")
    routes: list[tuple[str, dict[str, str]]] = []
    env_name = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
    for index, raw in enumerate(raw_routes):
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise AcceptanceContractError(
                f"Provider route {index} is not valid JSON"
            ) from exc
        if not isinstance(value, dict) or set(value) != {
            "name",
            "endpoint_env",
            "key_env",
        }:
            raise AcceptanceContractError(
                f"Provider route {index} must name endpoint_env and key_env"
            )
        name, endpoint_name, key_name = (
            value["name"],
            value["endpoint_env"],
            value["key_env"],
        )
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise AcceptanceContractError(f"Provider route {index} has an invalid name")
        if not all(
            isinstance(item, str) and env_name.fullmatch(item)
            for item in (endpoint_name, key_name)
        ):
            raise AcceptanceContractError(
                f"Provider route {index} must use environment variable names"
            )
        endpoint = base_env.get(endpoint_name)
        key = base_env.get(key_name)
        if not endpoint or not key:
            raise GrokCapabilityError(
                f"Provider route {name.strip()} capability is unavailable"
            )
        route_env = dict(base_env)
        route_env["GROK_MODELS_BASE_URL"] = endpoint
        route_env["XAI_API_KEY"] = key
        route_env["RIGHTCODE_GROK_API_KEY"] = key
        routes.append((name.strip(), route_env))
    return tuple(routes)


def run_with_provider_failover(
    operation: Callable[[Mapping[str, str]], Any],
    routes: Sequence[tuple[str, dict[str, str]]],
    *,
    workspace: Path,
    pristine_snapshot: Mapping[str, str],
) -> tuple[Any, str]:
    """Retry only retryable provider failures before any workspace mutation."""
    for index, (name, route_env) in enumerate(routes):
        try:
            return operation(route_env), name
        except GrokProviderError as exc:
            if workspace_delta(pristine_snapshot, workspace_snapshot(workspace)):
                encoded = json.dumps(
                    exc.evidence,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                raise AdapterError(
                    "Retryable Grok provider failure occurred after workspace mutation; "
                    "partial work was preserved and failover was suppressed; "
                    f"stderr_evidence={encoded}"
                ) from exc
            if index + 1 >= len(routes):
                raise
    raise AdapterError("No Grok provider route was available")


def _run_verification_command(
    command: AcceptanceCommand,
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float,
) -> dict[str, Any]:
    """Run one detector-owned command with bounded, sanitized evidence."""
    run_id = str(uuid.uuid4())
    verification_env = _verification_environment(env)
    verification_env[ACP_RUN_ENV_KEY] = run_id
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    started = time.monotonic()
    argv = list(command.argv)
    effective_timeout = min(timeout, command.timeout)
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=verification_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **options,
    )
    timed_out = False
    try:
        try:
            stdout, stderr = process.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if process.poll() is None:
                    process.kill()
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
    finally:
        _reap_linux_run_processes_sync(run_id)
    duration_ms = int((time.monotonic() - started) * 1000)
    encoded_argv = json.dumps(argv, ensure_ascii=False, separators=(",", ":"))
    raw_command = encoded_argv.encode("utf-8", errors="replace")
    return {
        "label": command.label,
        "argv": [_redact_stderr(item) for item in argv],
        "command": _redact_stderr(shlex.join(argv)),
        "command_sha256": hashlib.sha256(raw_command).hexdigest(),
        "returncode": None if timed_out else process.returncode,
        "timed_out": timed_out,
        "timeout_seconds": effective_timeout if timed_out else None,
        "configured_timeout_seconds": command.timeout,
        "duration_ms": duration_ms,
        "execution": "argv",
        "process_cleanup": "passed" if sys.platform == "linux" else "not_supported",
        "stdout": _stderr_evidence(_text_output(stdout)),
        "stderr": _stderr_evidence(_text_output(stderr)),
    }


def _materialize_verification_workspace(
    source: Path,
    destination: Path,
    *,
    timeout: float,
) -> None:
    """Create an independent Git copy containing the source working-tree state."""
    clone = subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-hardlinks",
            "--no-checkout",
            str(source),
            str(destination),
        ],
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if clone.returncode:
        raise AdapterError("Adapter-owned verification sandbox could not be created")
    head = workspace_head(source)
    checkout = subprocess.run(
        ["git", "-C", str(destination), "checkout", "--quiet", "--detach", head],
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if checkout.returncode:
        raise AdapterError("Adapter-owned verification sandbox checkout failed")
    diff = subprocess.run(
        ["git", "-C", str(source), "diff", "--binary", "HEAD", "--"],
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if diff.returncode:
        raise AdapterError("Adapter-owned verification sandbox diff failed")
    if diff.stdout:
        applied = subprocess.run(
            ["git", "-C", str(destination), "apply", "--binary", "--"],
            input=diff.stdout,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        if applied.returncode:
            raise AdapterError(
                "Adapter-owned verification sandbox diff could not apply"
            )
    untracked = subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if untracked.returncode:
        raise AdapterError("Adapter-owned verification sandbox inventory failed")
    destination_root = destination.resolve()
    for raw_path in untracked.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative_text = raw_path.decode("utf-8", errors="surrogateescape")
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise AdapterError("Adapter-owned verification sandbox path is unsafe")
        source_path = source / relative
        destination_path = destination / relative
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if not destination_path.parent.resolve().is_relative_to(destination_root):
            raise AdapterError("Adapter-owned verification sandbox path escaped")
        metadata = source_path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            destination_path.symlink_to(os.readlink(source_path))
        elif stat.S_ISREG(metadata.st_mode):
            shutil.copy2(source_path, destination_path, follow_symlinks=False)
        else:
            raise AdapterError(
                "Adapter-owned verification sandbox found an unsupported artifact"
            )
    for candidate in destination.rglob("*"):
        if candidate.is_symlink() and not candidate.resolve(
            strict=False
        ).is_relative_to(destination_root):
            raise AdapterError(
                "Adapter-owned verification sandbox contains an unsafe symlink"
            )


def run_adapter_verification(
    commands: Sequence[AcceptanceCommand],
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float,
) -> dict[str, Any]:
    """Run the preflight-pinned verification commands outside the model session."""
    fixed_commands = tuple(commands)
    if not fixed_commands:
        raise AcceptanceContractError(
            "At least one structured adapter-owned acceptance command is required"
        )
    with tempfile.TemporaryDirectory(prefix="hermes-grok-verification-") as temp_root:
        isolated_workspace = Path(temp_root) / "workspace"
        _materialize_verification_workspace(
            cwd,
            isolated_workspace,
            timeout=timeout,
        )
        snapshot_before = workspace_snapshot(isolated_workspace)
        head_before = workspace_head(isolated_workspace)
        results = [
            _run_verification_command(
                command,
                env=env,
                cwd=isolated_workspace,
                timeout=timeout,
            )
            for command in fixed_commands
        ]
        snapshot_after = workspace_snapshot(isolated_workspace)
        head_after = workspace_head(isolated_workspace)
        mutations = sorted(workspace_delta(snapshot_before, snapshot_after))
        passed = (
            all(
                result["returncode"] == 0 and not result["timed_out"]
                for result in results
            )
            and not mutations
            and head_before == head_after
        )
        return {
            "status": "passed" if passed else "failed",
            "commands": results,
            "command_count": len(results),
            "execution_root": "isolated",
            "workspace_mutation_count": len(mutations),
            "workspace_mutations": mutations[:VERIFICATION_MUTATION_PATH_LIMIT],
            "workspace_mutations_truncated": (
                len(mutations) > VERIFICATION_MUTATION_PATH_LIMIT
            ),
            "head_before": head_before,
            "head_after": head_after,
            "head_changed": head_before != head_after,
        }


def run_grok(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float,
) -> str:
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(
        list(argv),
        cwd=str(cwd),
        env=dict(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **options,
    )
    try:
        stdout, _stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                text=True,
                capture_output=True,
                check=False,
            )
            if process.poll() is None:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.communicate()
        raise GrokTimeout(f"Grok CLI timed out after {timeout:g} seconds") from exc
    if process.returncode:
        raise grok_exit_error(process.returncode, stdout, _stderr)
    return stdout


class _AcpClient:
    """Minimal ACP client surface for Grok's own repository tools."""

    def __init__(self) -> None:
        self.update_count = 0
        self.tool_call_count = 0
        self.max_update_json_bytes = 0
        self.update_sequence: list[int] = []

    async def session_update(
        self, session_id: str, update: Any, **_kwargs: Any
    ) -> None:
        del session_id
        self.update_count += 1
        dump_json = getattr(update, "model_dump_json", None)
        if callable(dump_json):
            encoded = dump_json(by_alias=True, exclude_none=True).encode()
            self.max_update_json_bytes = max(self.max_update_json_bytes, len(encoded))
        field_meta = getattr(update, "field_meta", None)
        sequence = field_meta.get("sequence") if isinstance(field_meta, dict) else None
        if isinstance(sequence, int) and len(self.update_sequence) < 64:
            self.update_sequence.append(sequence)
        if getattr(update, "session_update", None) == "tool_call":
            self.tool_call_count += 1

    async def request_permission(
        self,
        options: list[Any],
        session_id: str,
        tool_call: Any,
        **_kwargs: Any,
    ) -> Any:
        del session_id, tool_call
        if not options:
            raise AdapterError("Grok ACP requested permission without any option")
        # The Grok process is also started with --always-approve. This handler is
        # a protocol-safe fallback for versions that still emit a permission RPC.
        from acp.schema import AllowedOutcome, RequestPermissionResponse

        return RequestPermissionResponse(
            outcome=AllowedOutcome(option_id=options[0].option_id, outcome="selected")
        )


def _stop_reason(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def _linux_run_processes(
    run_id: str,
    *,
    env_key: str = ACP_RUN_ENV_KEY,
) -> set[int]:
    """Find live Linux processes that inherited a private run marker."""
    if sys.platform != "linux":
        return set()
    marker = f"{env_key}={run_id}".encode()
    matches: set[int] = set()
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError:
        return matches
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            environment = (entry / "environ").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        if marker in environment:
            matches.add(pid)
    return matches


def _reap_linux_run_processes_sync(
    run_id: str,
    *,
    env_key: str = ACP_RUN_ENV_KEY,
    owner: str = "Adapter-owned verification",
) -> None:
    """Synchronously reap marked verification descendants on Linux."""
    if sys.platform != "linux":
        return
    for sent_signal, grace in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 1.0)):
        remaining = _linux_run_processes(run_id, env_key=env_key)
        if not remaining:
            return
        for pid in sorted(remaining, reverse=True):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, sent_signal)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            time.sleep(0.05)
            if not _linux_run_processes(run_id, env_key=env_key):
                return
    if _linux_run_processes(run_id, env_key=env_key):
        raise AdapterError(f"{owner} residual process cleanup failed")


async def _reap_linux_run_processes(run_id: str) -> None:
    """Boundedly terminate tool descendants that escaped Grok's process group."""
    if sys.platform != "linux":
        return
    loop = asyncio.get_running_loop()
    for sent_signal, grace in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 1.0)):
        remaining = _linux_run_processes(run_id)
        if not remaining:
            return
        for pid in sorted(remaining, reverse=True):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, sent_signal)
        deadline = loop.time() + grace
        while loop.time() < deadline:
            await asyncio.sleep(0.05)
            if not _linux_run_processes(run_id):
                return
    if _linux_run_processes(run_id):
        raise AdapterError("Grok ACP residual process cleanup failed")


async def _acp_process_error(
    process: Any,
    *,
    phase: str = "ACP work",
) -> AdapterError:
    stderr = ""
    stream = getattr(process, "stderr", None)
    if stream is not None:
        with contextlib.suppress(Exception):
            raw = await asyncio.wait_for(stream.read(65_536), timeout=1.0)
            stderr = (
                raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            )
    return grok_exit_error(
        int(getattr(process, "returncode", 1) or 1),
        "",
        stderr,
        phase=phase,
    )


async def _await_acp_request(
    process: Any,
    request: Any,
    timeout: float,
    *,
    phase: str = "ACP work",
) -> Any:
    """Race an ACP request against early child exit to preserve error meaning."""
    wait_method = getattr(process, "wait", None)
    if not callable(wait_method):
        return await asyncio.wait_for(request, timeout=timeout)
    request_task = asyncio.ensure_future(request)
    process_task = asyncio.create_task(wait_method())
    try:
        done, _pending = await asyncio.wait(
            {request_task, process_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if request_task in done:
            return await request_task
        if process_task in done:
            request_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await request_task
            raise await _acp_process_error(process, phase=phase)
        request_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await request_task
        raise asyncio.TimeoutError
    finally:
        if not process_task.done():
            process_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await process_task


async def _run_grok_acp_async(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    prompt: str,
    timeout: float,
    no_progress_timeout: float,
    progress_probe: Callable[[], bool],
    poll_interval: float,
    report_prompt_factory: Callable[[str, AdapterError | None], str] | None,
    report_schema: Mapping[str, Any] | None,
    report_validator: Callable[[Mapping[str, Any]], None] | None,
    report_timeout: float,
    acceptance_probe: Callable[[], dict[str, Any]] | None,
) -> AcpWorkResult:
    try:
        import acp
    except ImportError as exc:
        raise GrokCapabilityError("Grok ACP client capability is unavailable") from exc

    if not argv:
        raise AdapterError("Grok ACP command is empty")
    client = _AcpClient()
    loop = asyncio.get_running_loop()
    overall_deadline = loop.time() + timeout
    progress_deadline = (
        loop.time() + no_progress_timeout if no_progress_timeout > 0 else None
    )
    session_id: str | None = None
    prompt_task: asyncio.Task[Any] | None = None
    process_task: asyncio.Task[Any] | None = None
    preserved_process_error: AdapterError | None = None
    run_id = str(uuid.uuid4())
    acp_env = dict(env)
    acp_env[ACP_RUN_ENV_KEY] = run_id

    try:
        async with acp.spawn_agent_process(
            client,
            argv[0],
            *argv[1:],
            env=acp_env,
            cwd=cwd,
            transport_kwargs={"limit": ACP_STREAM_LIMIT_BYTES},
        ) as (connection, process):
            await _await_acp_request(
                process,
                connection.initialize(protocol_version=acp.PROTOCOL_VERSION),
                timeout=max(0.001, overall_deadline - loop.time()),
            )
            session = await _await_acp_request(
                process,
                connection.new_session(cwd=str(cwd), mcp_servers=[]),
                timeout=max(0.001, overall_deadline - loop.time()),
            )
            session_id = session.session_id
            prompt_task = asyncio.create_task(
                connection.prompt(
                    prompt=[acp.text_block(prompt)],
                    session_id=session_id,
                )
            )
            process_task = (
                asyncio.create_task(process.wait())
                if callable(getattr(process, "wait", None))
                else None
            )
            while not prompt_task.done():
                if process_task is not None and process_task.done():
                    raise await _acp_process_error(process)
                now = loop.time()
                if now >= overall_deadline:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            connection.cancel(session_id=session_id), timeout=5.0
                        )
                    raise GrokTimeout(
                        f"Grok ACP work phase timed out after {timeout:g} seconds"
                    )
                if progress_deadline is not None:
                    if progress_probe():
                        progress_deadline = None
                    elif now >= progress_deadline:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(
                                connection.cancel(session_id=session_id), timeout=5.0
                            )
                        raise GrokNoProgress(
                            "Grok ACP work phase produced no workspace changes "
                            f"after {no_progress_timeout:g} seconds"
                        )
                await asyncio.wait(
                    {prompt_task}
                    if process_task is None
                    else {prompt_task, process_task},
                    timeout=min(poll_interval, max(0.001, overall_deadline - now)),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            response = await prompt_task
            result = AcpWorkResult(
                session_id=session_id,
                stop_reason=_stop_reason(response.stop_reason),
                update_count=client.update_count,
                tool_call_count=client.tool_call_count,
                max_update_json_bytes=client.max_update_json_bytes,
                update_sequence=tuple(client.update_sequence),
            )
            if result.stop_reason != "end_turn":
                raise AdapterError(
                    "Grok ACP work phase ended without a terminal turn: "
                    f"{result.stop_reason}"
                )
            acceptance: dict[str, Any] | None = None
            if acceptance_probe is not None:
                acceptance = acceptance_probe()
                failed_commands = [
                    item
                    for item in acceptance.get("commands", [])
                    if item.get("timed_out") or item.get("returncode") != 0
                ]
                safe_to_continue = (
                    acceptance.get("status") == "failed"
                    and bool(failed_commands)
                    and not acceptance.get("workspace_mutation_count")
                    and not acceptance.get("head_changed")
                )
                if safe_to_continue and loop.time() < overall_deadline:
                    assert session_id is not None
                    residual = json.dumps(
                        failed_commands,
                        ensure_ascii=True,
                        separators=(",", ":"),
                    )
                    residual = residual[:12_000]
                    try:
                        continuation = await _await_acp_request(
                            process,
                            connection.prompt(
                                prompt=[
                                    acp.text_block(
                                        "Adapter-owned acceptance still fails. Fix only "
                                        "these exact residual checks in this same session, "
                                        "then end the turn: " + residual
                                    )
                                ],
                                session_id=session_id,
                            ),
                            timeout=max(0.001, overall_deadline - loop.time()),
                            phase="acceptance continuation",
                        )
                    except asyncio.TimeoutError as exc:
                        raise GrokTimeout(
                            "Grok ACP acceptance continuation timed out"
                        ) from exc
                    if _stop_reason(continuation.stop_reason) != "end_turn":
                        raise AdapterError(
                            "Grok ACP acceptance continuation ended without a terminal turn"
                        )
                    acceptance = acceptance_probe()
                result = result._replace(acceptance=acceptance)
            if report_prompt_factory is None:
                return result
            if report_schema is None or report_validator is None:
                raise AdapterError(
                    "Grok ACP structured reporting requires a schema and validator"
                )

            async def request_report(
                validation_error: AdapterError | None,
            ) -> dict[str, Any]:
                nonlocal preserved_process_error
                phase = (
                    "report correction"
                    if validation_error is not None
                    else "terminal report"
                )
                try:
                    report_response = await _await_acp_request(
                        process,
                        connection.prompt(
                            prompt=[
                                acp.text_block(
                                    report_prompt_factory(
                                        result.stop_reason,
                                        validation_error,
                                    )
                                )
                            ],
                            session_id=session_id,
                            outputSchema=dict(report_schema),
                        ),
                        timeout=report_timeout,
                        phase=phase,
                    )
                except asyncio.TimeoutError as exc:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            connection.cancel(session_id=session_id),
                            timeout=5.0,
                        )
                    timeout_error = GrokTimeout(
                        f"Grok ACP {phase} phase timed out after "
                        f"{report_timeout:g} seconds"
                    )
                    preserved_process_error = timeout_error
                    raise timeout_error from exc
                except AdapterError as exc:
                    preserved_process_error = exc
                    raise
                field_meta = getattr(report_response, "field_meta", None)
                structured = (
                    field_meta.get("structuredOutput")
                    if isinstance(field_meta, dict)
                    else None
                )
                if not isinstance(structured, dict):
                    raise ReportContractError(
                        "Grok ACP terminal report did not return structuredOutput"
                    )
                return dict(structured)

            def validate_structured_report(report: Mapping[str, Any]) -> None:
                try:
                    report_validator(report)
                except ReportContractError:
                    raise
                except AdapterError as exc:
                    raise ReportContractError(str(exc)) from exc

            try:
                report = await request_report(None)
                validate_structured_report(report)
            except ReportContractError as validation_error:
                report = await request_report(validation_error)
                try:
                    validate_structured_report(report)
                except ReportContractError as final_error:
                    evidence = json.dumps(
                        _report_contract_evidence(report),
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    raise ReportContractError(
                        f"{final_error}; report_evidence={evidence}"
                    ) from final_error
            return result._replace(report=dict(report))
    except (GrokTimeout, GrokNoProgress, CapabilityError, AdapterError):
        raise
    except asyncio.TimeoutError as exc:
        raise GrokTimeout(
            f"Grok ACP work phase timed out after {timeout:g} seconds"
        ) from exc
    except OSError as exc:
        if preserved_process_error is not None:
            raise preserved_process_error from exc
        raise AdapterError(f"Grok ACP launch failed: {type(exc).__name__}") from exc
    except Exception as exc:
        if isinstance(exc, ValueError) and any(
            marker in str(exc)
            for marker in (
                "chunk is longer than limit",
                "chunk exceed the limit",
            )
        ):
            raise GrokCapabilityError(
                "Grok ACP frame exceeded the bounded "
                f"{ACP_STREAM_LIMIT_BYTES}-byte receive limit"
            ) from exc
        if getattr(exc, "code", None) == -32000:
            raise GrokCapabilityError(
                "Grok authentication capability is unavailable"
            ) from exc
        classified = grok_exit_error(
            1,
            "",
            f"{exc} {getattr(exc, 'data', '')}",
        )
        if isinstance(classified, GrokCapabilityError):
            raise classified from exc
        if isinstance(classified, GrokProviderError):
            raise classified from exc
        raise AdapterError(f"Grok ACP session failed: {type(exc).__name__}") from exc
    finally:
        if prompt_task is not None:
            if not prompt_task.done():
                prompt_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await prompt_task
        if process_task is not None and not process_task.done():
            process_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await process_task
        await _reap_linux_run_processes(run_id)


def run_grok_acp(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    prompt: str,
    timeout: float,
    no_progress_timeout: float,
    progress_probe: Callable[[], bool],
    poll_interval: float = 1.0,
    report_prompt_factory: Callable[[str, AdapterError | None], str] | None = None,
    report_schema: Mapping[str, Any] | None = None,
    report_validator: Callable[[Mapping[str, Any]], None] | None = None,
    report_timeout: float = 120.0,
    acceptance_probe: Callable[[], dict[str, Any]] | None = None,
) -> AcpWorkResult:
    """Run one official ACP session with durable cancellation boundaries."""
    return asyncio.run(
        _run_grok_acp_async(
            argv,
            env=env,
            cwd=cwd,
            prompt=prompt,
            timeout=timeout,
            no_progress_timeout=no_progress_timeout,
            progress_probe=progress_probe,
            poll_interval=poll_interval,
            report_prompt_factory=report_prompt_factory,
            report_schema=report_schema,
            report_validator=report_validator,
            report_timeout=report_timeout,
            acceptance_probe=acceptance_probe,
        )
    )


def child_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Build the child environment without persisting or printing credentials."""
    result = dict(source)
    if not result.get("RIGHTCODE_GROK_API_KEY") and result.get("RIGHTCODE_API_KEY"):
        result["RIGHTCODE_GROK_API_KEY"] = result["RIGHTCODE_API_KEY"]
    rightcode_key = result.get("RIGHTCODE_GROK_API_KEY")
    if rightcode_key:
        # Grok CLI 1.0.4 uses XAI_API_KEY to authorize model discovery even
        # when a custom model has its own env_key. Pin the discovery endpoint
        # with the compatibility alias so a RightCode key cannot fall through
        # to the first-party xAI endpoint.
        if not result.get("XAI_API_KEY"):
            result["XAI_API_KEY"] = rightcode_key
        if not result.get("GROK_MODELS_BASE_URL"):
            result["GROK_MODELS_BASE_URL"] = RIGHTCODE_GROK_BASE_URL
    configured_grok_home = result.get("GROK_HOME")
    source_home = result.get("HOME")
    grok_home = (
        Path(configured_grok_home)
        if configured_grok_home
        else Path(source_home) / ".grok"
        if source_home
        else None
    )
    if grok_home is not None and grok_home.is_symlink():
        try:
            # Grok's Linux write-deny sandbox deliberately rejects a symlinked
            # GROK_HOME. Resolve it only for this child process; do not rewrite
            # the user's home layout or authentication state.
            result["GROK_HOME"] = str(grok_home.resolve(strict=True))
        except OSError as exc:
            raise GrokCapabilityError(
                "Grok home capability is unavailable for sandboxed execution"
            ) from exc
    result["HERMES_PROFILE"] = PROFILE
    return result


def inspect_grok_environment(
    args: argparse.Namespace,
    env: Mapping[str, str],
    runner: CommandRunner,
) -> dict[str, Any]:
    """Return a bounded summary of Grok's native project discovery surfaces."""
    try:
        result = runner(
            [args.grok_bin, "inspect", "--json"],
            env=env,
            cwd=args.workspace,
            timeout=args.command_timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "unavailable", "reason": "inspect_launch_failed"}
    if result.returncode:
        return {"status": "unavailable", "reason": "inspect_command_failed"}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"status": "unavailable", "reason": "inspect_invalid_json"}
    if not isinstance(payload, dict):
        return {"status": "unavailable", "reason": "inspect_invalid_shape"}

    def count(name: str) -> int:
        value = payload.get(name)
        return len(value) if isinstance(value, list) else 0

    return {
        "status": "ok",
        "version": str(payload.get("grokVersion") or "unknown"),
        "project_trusted": payload.get("projectTrusted") is True,
        "project_instructions": count("projectInstructions"),
        "skills": count("skills"),
        "plugins": count("plugins"),
        "mcp_servers": count("mcpServers"),
        "lsp_servers": count("lspServers"),
    }


def _is_untracked_test_artifact(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    cache_directories = {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".hypothesis",
    }
    name = parts[-1]
    return (
        any(part in cache_directories for part in parts)
        or name.endswith((".pyc", ".pyo"))
        or name == ".coverage"
        or name.startswith(".coverage.")
    )


_GIT_PROBES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("diff", ("diff", "--name-only", "-z", "HEAD", "--")),
    ("untracked", ("ls-files", "--others", "--exclude-standard", "-z")),
    ("head", ("rev-parse", "--verify", "HEAD")),
)


def _git_probe(workspace: Path, label: str, arguments: Sequence[str]) -> bytes:
    command = ["git", "-C", str(workspace), *arguments]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceReadinessError(
            f"Git readiness probe={label} exception={type(exc).__name__}",
            retryable=isinstance(exc, (FileNotFoundError, subprocess.TimeoutExpired)),
        ) from exc
    if result.returncode:
        stderr = _text_output(result.stderr)
        evidence = _stderr_evidence(stderr)
        lowered = stderr.casefold()
        materializing = any(
            marker in lowered
            for marker in (
                "not a git repository",
                "unknown revision or path not in the working tree",
                "ambiguous argument 'head'",
                "needed a single revision",
                "bad revision 'head'",
            )
        )
        raise WorkspaceReadinessError(
            f"Git readiness probe={label} returncode={result.returncode}; "
            f"stderr_evidence={json.dumps(evidence, ensure_ascii=False, separators=(',', ':'))}",
            retryable=materializing,
        )
    return bytes(result.stdout)


def workspace_changes(workspace: Path) -> set[str]:
    """Return paths whose final state differs from HEAD in the Git worktree."""
    commands = ((_GIT_PROBES[0], False), (_GIT_PROBES[1], True))
    changed: set[str] = set()
    for (label, arguments), filter_artifacts in commands:
        output = _git_probe(workspace, label, arguments)
        paths = (
            path.decode("utf-8", errors="surrogateescape")
            for path in output.split(b"\0")
            if path
        )
        changed.update(
            path
            for path in paths
            if not filter_artifacts or not _is_untracked_test_artifact(path)
        )
    return changed


def _workspace_path_fingerprint(workspace: Path, path: str) -> str:
    """Fingerprint content and Git-relevant type/mode without following symlinks."""
    target = workspace / path
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        raise AdapterError("Claimed workspace could not be snapshotted") from exc

    file_type = stat.S_IFMT(metadata.st_mode)
    executable = bool(metadata.st_mode & stat.S_IXUSR)
    digest = hashlib.sha256()
    digest.update(f"{file_type}:{int(executable)}\0".encode())
    try:
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(os.readlink(target).encode("utf-8", errors="surrogateescape"))
        elif stat.S_ISREG(metadata.st_mode):
            with target.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        elif stat.S_ISDIR(metadata.st_mode):
            # A directory path in a parent Git diff is normally a submodule.
            # Its commit and dirty status are the stable observable state.
            head = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "HEAD"],
                capture_output=True,
                check=False,
            )
            status_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                ],
                capture_output=True,
                check=False,
            )
            if head.returncode or status_result.returncode:
                raise AdapterError("Claimed workspace contains an unreadable submodule")
            digest.update(head.stdout)
            digest.update(b"\0")
            digest.update(status_result.stdout)
        else:
            digest.update(str(metadata.st_size).encode())
    except OSError as exc:
        raise AdapterError("Claimed workspace could not be snapshotted") from exc
    return digest.hexdigest()


def workspace_snapshot(workspace: Path) -> WorkspaceSnapshot:
    """Capture the observable state of every current HEAD-relative change."""
    return {
        path: _workspace_path_fingerprint(workspace, path)
        for path in workspace_changes(workspace)
    }


def workspace_head(workspace: Path) -> str:
    """Return the claimed worktree's current commit identity."""
    output = _git_probe(workspace, *_GIT_PROBES[2])
    return output.decode("ascii", errors="replace").strip()


def wait_for_claimed_workspace(
    workspace: Path,
    *,
    timeout: float,
) -> WorkspaceSnapshot:
    """Wait boundedly for a claimed path to become a readable Git worktree."""
    deadline = time.monotonic() + max(0.0, timeout)
    previous_state: tuple[str, WorkspaceSnapshot] | None = None
    stable_polls = 0
    while True:
        if workspace.is_dir():
            try:
                snapshot = workspace_snapshot(workspace)
                state = (workspace_head(workspace), snapshot)
                if state == previous_state:
                    stable_polls += 1
                else:
                    previous_state = state
                    stable_polls = 1
                if stable_polls >= CLAIM_READY_STABLE_POLLS:
                    return snapshot
            except WorkspaceReadinessError as exc:
                if not exc.retryable:
                    raise
                previous_state = None
                stable_polls = 0
        if time.monotonic() >= deadline:
            if not workspace.is_dir():
                raise AdapterError("Claimed workspace does not exist")
            try:
                workspace_snapshot(workspace)
                workspace_head(workspace)
            except WorkspaceReadinessError as exc:
                raise WorkspaceReadinessError(str(exc), retryable=False) from exc
            raise WorkspaceReadinessError(
                "Claimed workspace readiness did not stabilize before the deadline",
                retryable=False,
            )
        time.sleep(CLAIM_READY_POLL_SECONDS)


def workspace_delta(before: Mapping[str, str], after: Mapping[str, str]) -> set[str]:
    """Return paths whose observable state changed between two snapshots."""
    return {
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    }


def build_project_context_pack(workspace: Path) -> dict[str, Any]:
    """Reuse Hermes' coding-context detector for Grok worker orientation."""
    try:
        from agent.coding_context import (
            build_coding_workspace_block,
            project_facts_for,
        )
    except ImportError as exc:
        raise ProjectContextCapabilityError(
            "Hermes project-context capability is unavailable"
        ) from exc

    try:
        workspace_snapshot = build_coding_workspace_block(workspace).strip()
        facts = project_facts_for(workspace) or {}
    except Exception as exc:
        raise ProjectContextCapabilityError(
            "Hermes project-context capability failed"
        ) from exc
    if not workspace_snapshot:
        raise ProjectContextCapabilityError(
            "Claimed workspace has no detectable Hermes project context"
        )

    verify_commands = facts.get("verifyCommands")
    context_files = facts.get("contextFiles")
    manifests = facts.get("manifests")
    return {
        "workspace_snapshot": workspace_snapshot,
        "root": str(facts.get("root") or workspace),
        "manifests": [str(item) for item in manifests]
        if isinstance(manifests, list)
        else [],
        "verify_commands": [str(item) for item in verify_commands]
        if isinstance(verify_commands, list)
        else [],
        "context_files": [str(item) for item in context_files]
        if isinstance(context_files, list)
        else [],
    }


def _json_object(value: Any, depth: int = 0) -> dict[str, Any] | None:
    if depth > 8:
        return None
    if isinstance(value, dict) and "status" in value:
        return value
    if isinstance(value, dict):
        preferred = (
            "structured_output",
            "result",
            "output",
            "output_text",
            "content",
            "message",
            "response",
            "text",
        )
        ordered_keys = [key for key in preferred if key in value]
        ordered_keys.extend(key for key in value if key not in ordered_keys)
        for key in ordered_keys:
            found = _json_object(value[key], depth + 1)
            if found is not None:
                return found
    if isinstance(value, list):
        # Headless agent envelopes may retain several chronological assistant
        # messages. The last structured report is the terminal candidate.
        for candidate in reversed(value):
            found = _json_object(candidate, depth + 1)
            if found is not None:
                return found
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return _json_object(decoded, depth + 1)
    return None


def parse_report(stdout: str, *, task_mode: str = "implementation") -> dict[str, Any]:
    try:
        envelope = json.loads(stdout.strip())
    except json.JSONDecodeError as exc:
        raise AdapterError("Grok CLI did not return a JSON envelope") from exc
    report = _json_object(envelope)
    if report is None:
        raise AdapterError("Grok CLI JSON envelope did not contain a worker report")
    validate_report(report, task_mode=task_mode)
    return report


def validate_report(
    report: Mapping[str, Any], *, task_mode: str = "implementation"
) -> None:
    required = set(REPORT_SCHEMA["required"])
    if set(report) != required:
        raise AdapterError("Worker report fields do not match the required schema")
    if not isinstance(report["status"], str) or report["status"] not in {
        "completed",
        "blocked",
    }:
        raise AdapterError("Worker report has an invalid status")
    if not isinstance(report["summary"], str) or not report["summary"].strip():
        raise AdapterError("Worker report summary is empty")
    if report["status"] == "completed":
        normalized_summary = report["summary"].strip().casefold()
        if normalized_summary in {
            "placeholder",
            "inspecting",
            "in progress",
            "working",
        }:
            raise AdapterError(
                "A completed worker report cannot be a progress or placeholder summary"
            )
        if task_mode == "implementation" and (
            not isinstance(report["changed_files"], list) or not report["changed_files"]
        ):
            raise AdapterError(
                "A completed implementation report requires at least one changed file"
            )
        if task_mode == "review" and not normalized_summary.startswith((
            "pass:",
            "changes_requested:",
        )):
            raise AdapterError(
                "A completed review summary must start with PASS: or CHANGES_REQUESTED:"
            )
        if not isinstance(report["tests"], list) or not report["tests"]:
            raise AdapterError(
                "A completed worker report requires at least one test record"
            )
        if any(
            isinstance(test, dict) and test.get("outcome") == "not_run"
            for test in report["tests"]
        ):
            raise AdapterError("A completed worker report cannot contain not_run tests")
        if task_mode == "review" and (
            not isinstance(report["evidence"], list) or not report["evidence"]
        ):
            raise AdapterError("A completed review report requires evidence")
    for name in ("changed_files", "risks", "evidence"):
        if not isinstance(report[name], list) or not all(
            isinstance(item, str) for item in report[name]
        ):
            raise AdapterError(f"Worker report {name} must be a string array")
    if not isinstance(report["tests"], list):
        raise AdapterError("Worker report tests must be an array")
    for test in report["tests"]:
        if not isinstance(test, dict) or set(test) != {"command", "outcome", "details"}:
            raise AdapterError("Worker report contains an invalid test record")
        if not all(isinstance(test[key], str) for key in test):
            raise AdapterError("Worker report test fields must be strings")
        if test["outcome"] not in {"passed", "failed", "not_run"}:
            raise AdapterError("Worker report contains an invalid test outcome")
        if "&&" in test["command"] or "||" in test["command"]:
            raise AdapterError(
                "Worker report test records must describe one command at a time"
            )
    block_kind = report["block_kind"]
    if block_kind is not None and (
        not isinstance(block_kind, str)
        or block_kind not in {"capability", "dependency", "needs_input", "transient"}
    ):
        raise AdapterError("Worker report has an invalid block kind")
    if not isinstance(report["block_reason"], str):
        raise AdapterError("Worker report block_reason must be a string")
    if report["status"] == "completed" and report["block_reason"].strip():
        raise AdapterError("A completed worker report must have an empty block_reason")
    if report["status"] == "completed" and block_kind is not None:
        raise AdapterError("A completed worker report must use a null block_kind")
    if report["status"] == "blocked" and not report["block_reason"].strip():
        raise AdapterError("A blocked worker report requires block_reason")
    if report["status"] == "blocked" and block_kind == "capability":
        raise AdapterError(
            "The adapter owns capability classification; a model-authored blocked "
            "report cannot use capability"
        )
    if report["status"] == "blocked" and block_kind is None:
        raise AdapterError("A blocked worker report requires a block kind")
    if report["status"] == "blocked" and (
        not report["evidence"] or not any(item.strip() for item in report["evidence"])
    ):
        raise AdapterError("A blocked worker report requires evidence")
    if report["status"] == "blocked" and not report["tests"]:
        raise AdapterError("A blocked worker report requires a test record")
    if report["status"] == "blocked" and report[
        "summary"
    ].strip().casefold().startswith("pass:"):
        raise AdapterError("A blocked review cannot claim PASS")
    if (
        task_mode == "review"
        and report["status"] == "blocked"
        and "read-only" in report["block_reason"].casefold()
    ):
        raise AdapterError("Read-only review mode is not itself a blocker")


def _hermes(
    args: argparse.Namespace,
    subcommand: Sequence[str],
    env: Mapping[str, str],
    runner: CommandRunner,
) -> subprocess.CompletedProcess[str]:
    command = [args.hermes_bin, "kanban", "--board", args.board, *subcommand]
    hermes_env = {
        key: value
        for key, value in env.items()
        if key not in {"RIGHTCODE_API_KEY", "RIGHTCODE_GROK_API_KEY", "XAI_API_KEY"}
    }
    result = runner(command, env=hermes_env, timeout=args.command_timeout)
    if result.returncode:
        raise AdapterError(f"Hermes Kanban command failed: {subcommand[0]}")
    return result


def _project_context_prompt(
    project_context: Mapping[str, Any] | None,
    grok_discovery: Mapping[str, Any] | None,
) -> str:
    if not project_context:
        return ""
    verify_commands = project_context.get("verify_commands") or []
    context_files = project_context.get("context_files") or []
    discovery = grok_discovery or {"status": "not_run"}
    if discovery.get("status") == "ok":
        discovery_line = (
            f"project_instructions={discovery.get('project_instructions', 0)}, "
            f"skills={discovery.get('skills', 0)}, "
            f"plugins={discovery.get('plugins', 0)}, "
            f"mcp_servers={discovery.get('mcp_servers', 0)}, "
            f"lsp_servers={discovery.get('lsp_servers', 0)}"
        )
    else:
        discovery_line = f"unavailable ({discovery.get('reason', 'not_run')})"
    verify_text = (
        "\n".join(f"- {command}" for command in verify_commands)
        if verify_commands
        else "- No generic verification command was detected; use card-specific commands."
    )
    context_files_text = ", ".join(context_files) if context_files else "none"
    return f"""
HERMES PROJECT CONTEXT PACK
---------------------------
This is a deterministic orientation snapshot produced by the same Hermes coding-context
detector used for Sol and Luna workers. Re-check live Git state before acting. The Kanban
card remains the authority for scope, allowlists, pinned SHAs, and required verification;
this context is evidence, not additional permission.

{project_context["workspace_snapshot"]}

Hermes-detected verification commands:
{verify_text}

Hermes-detected project instruction files: {context_files_text}
Grok native discovery: {discovery_line}
"""


def _prompt(
    context: str,
    task_mode: str = "implementation",
    project_context: Mapping[str, Any] | None = None,
    grok_discovery: Mapping[str, Any] | None = None,
) -> str:
    project_context_text = _project_context_prompt(project_context, grok_discovery)
    if task_mode == "review":
        return f"""You are the read-only security reviewer for exactly one Hermes Kanban card.

Review only the implementation SHA explicitly pinned in the card context. Do not modify the working tree in any way, and do not commit, push, open or update a PR, merge, or call Hermes Kanban. Running tests, static analysis, read-only catalog queries, and disposable database probes is explicitly authorized; read-only is not itself a blocker. Confirm the pinned SHA exists and record concrete review evidence. Verify before returning that this run caused no workspace changes. Do not stop after inspection or planning. Return exactly one terminal report after the review. If the SHA is missing or cannot be verified, return a blocked report; never manufacture PASS. A completed summary must begin with PASS: or CHANGES_REQUESTED:. A blocked summary must not claim PASS. The deterministic adapter owns the card's terminal transition.

KANBAN CARD CONTEXT
-------------------
{context}
{project_context_text}
"""
    return f"""You are the implementation worker for exactly one Hermes Kanban card.

Work only in the supplied cwd. Editing files there and running tests are explicitly authorized and are the purpose of this run. Inspect the repository, implement the card, and run proportionate tests. The upstream Foreman owns repository lifecycle actions. Do not call Hermes Kanban and do not claim, complete, or block the card; the deterministic adapter owns terminal state. Do not write to GitHub, create or manage a Pull Request, commit, or push. Leave the implementation as an uncommitted diff. Repository lifecycle requirements delegated to the Foreman are not a reason to avoid editing or to block; continue to obey all repository scope, quality, security, and testing requirements.

Do not stop after inspection, planning, or describing the next step. At the end, return only one terminal structured worker report. Use completed only when the requested work is done and verified. For completed reports, use an empty block_reason and null block_kind. If work cannot safely finish because of a concrete unavailable dependency or required user decision, use blocked with a concise non-empty block_reason, concrete evidence, and the most accurate block_kind from dependency, needs_input, or transient. The adapter, not the model, classifies unavailable local/provider capabilities.

KANBAN CARD CONTEXT
-------------------
{context}
{project_context_text}
"""


def _work_prompt(
    context: str,
    task_mode: str,
    project_context: Mapping[str, Any],
    grok_discovery: Mapping[str, Any],
) -> str:
    """Prompt the stateful ACP agent for work, not for a report checkpoint."""
    project_context_text = _project_context_prompt(project_context, grok_discovery)
    if task_mode == "review":
        mission = """Perform the complete read-only security review now. Verify the pinned
implementation SHA, inspect the relevant code, and run proportionate read-only checks.
Do not modify any repository file. Do not manufacture PASS. Finish the substantive review
before ending this turn; the adapter will collect a terminal report separately."""
    else:
        mission = """Implement the complete card now in the supplied working tree. Begin with
the narrowest relevant files and failing tests, make the requested edits, and run
proportionate verification. Do not spend the turn merely surveying the repository or
describing a plan. Leave the finished implementation as an uncommitted diff; the adapter
will collect a terminal report separately."""
    return f"""You are the delegated Grok worker for exactly one Hermes Kanban card.

{mission}

The upstream Foreman owns all Kanban and GitHub lifecycle actions. Do not call Hermes
Kanban, commit, push, open or modify a PR, or merge. Follow repository scope, security,
quality, and test instructions. Lifecycle actions delegated to the Foreman are not blockers.
Run each verification command separately; never join pytest, Ruff, or other checks with
shell && or || because the terminal report must attribute every result precisely.

KANBAN CARD CONTEXT
-------------------
{context}
{project_context_text}
"""


def parse_claimed_workspace(stdout: str) -> Path:
    for line in reversed(stdout.splitlines()):
        label, separator, value = line.strip().partition(":")
        if separator and label.strip().lower() == "workspace" and value.strip():
            return Path(value.strip()).expanduser().resolve()
    raise AdapterError("Hermes claim did not return a Workspace path")


def _rules(args: argparse.Namespace) -> str:
    return REVIEW_RULES if args.task_mode == "review" else WORKER_RULES


def _grok_command(args: argparse.Namespace, session_id: str, prompt: str) -> list[str]:
    return [
        args.grok_bin,
        "--agent",
        args.agent,
        "--no-auto-update",
        "--always-approve",
        "--disable-web-search",
        "--no-subagents",
        "--no-memory",
        "--no-plan",
        "--rules",
        _rules(args),
        "--cwd",
        str(args.workspace),
        "--model",
        args.model,
        "--max-turns",
        str(args.max_turns),
        "--session-id",
        session_id,
        "--json-schema",
        json.dumps(REPORT_SCHEMA, separators=(",", ":")),
        "-p",
        prompt,
    ]


def _acp_command(args: argparse.Namespace) -> list[str]:
    """Start the official stateful Grok ACP server for substantive work."""
    return [
        args.grok_bin,
        "agent",
        "--model",
        args.model,
        "--reasoning-effort",
        args.reasoning_effort,
        "--always-approve",
        "--no-leader",
        "stdio",
    ]


def _terminal_report_prompt(
    args: argparse.Namespace,
    observed_run_changes: Sequence[str],
    *,
    stop_reason: str,
    validation_error: AdapterError | None = None,
) -> str:
    del args
    correction = ""
    if validation_error is not None:
        correction = (
            "The previous terminal report was rejected by the deterministic adapter: "
            f"{validation_error}. Correct only the report fields using the existing "
            "session evidence. "
        )
    return (
        "The substantive ACP work phase has ended. This is a read-only reporting phase: "
        "do not edit files or run more tools. "
        f"ACP stop reason: {stop_reason}. "
        "The adapter observed these paths change during this invocation: "
        f"{json.dumps(sorted(observed_run_changes), ensure_ascii=True)}. "
        f"{correction}"
        "Return exactly one terminal JSON report matching the supplied schema. Only "
        "completed and blocked are legal statuses. If status is completed, block_reason "
        "MUST be exactly an empty string and block_kind MUST be null. Do not describe a "
        "blocker in a completed report. If status is completed, every listed test "
        "outcome MUST be passed or failed; omit optional or out-of-scope checks that "
        "were not run. If a required check could not run, status MUST be blocked with "
        "block_kind dependency. Never mark an unrun check as passed. A completed "
        "implementation must list exactly the observed task paths and real tests already "
        "run. If status is blocked, "
        "block_reason MUST be non-empty and block_kind MUST be dependency, needs_input, "
        "or transient. A blocked report requires concrete evidence and must not use "
        "capability; only the adapter classifies provider or local capability failures. "
        "Do not emit a working or checkpoint report."
    )


def _resume_command(
    args: argparse.Namespace,
    session_id: str,
    validation_error: AdapterError,
    observed_run_changes: Sequence[str] = (),
) -> list[str]:
    error_message = str(validation_error)
    observed_note = ""
    if observed_run_changes:
        observed_note = (
            " The adapter's content snapshots observed these candidate paths change "
            "during this invocation: "
            f"{json.dumps(sorted(observed_run_changes), ensure_ascii=True)}. "
            "This observation is evidence, not authorization: inspect and confirm the "
            "current workspace, and make completed changed_files exactly match all and "
            "only task changes made during this invocation."
        )
    # The resumed session retains its agent profile; passing --agent again makes
    # Grok 1.0.4 reject the correction command before it can emit a report.
    return [
        args.grok_bin,
        "--no-auto-update",
        "--always-approve",
        "--disable-web-search",
        "--no-subagents",
        "--no-memory",
        "--no-plan",
        "--rules",
        _rules(args),
        "--cwd",
        str(args.workspace),
        "--model",
        args.model,
        "--max-turns",
        str(args.max_turns),
        "--resume",
        session_id,
        "--json-schema",
        json.dumps(REPORT_SCHEMA, separators=(",", ":")),
        "-p",
        (
            f"Your previous terminal review report was rejected by the deterministic "
            f"adapter: {error_message}. Continue from the current session state; do not "
            "restart the review or repeat repository inspection merely to repair report "
            "fields. If substantive review work remains, perform only that remaining "
            "work. Then return one terminal, "
            "evidenced PASS: or CHANGES_REQUESTED: report; block only for a concrete "
            "unavailable dependency or capability."
            if args.task_mode == "review"
            else f"Your previous terminal worker report was rejected by the deterministic "
            f"adapter: {error_message}. Continue from the current session and workspace "
            f"state; do not start over.{observed_note} Do not repeat repository inspection merely to "
            "repair report fields. If the implementation is unfinished and there is no "
            "concrete blocker, continue editing and testing. Then return one terminal "
            "JSON report with "
            "status=completed or status=blocked. A blocked report requires a concrete, "
            "non-empty block_reason."
        ),
    ]


def _run_grok_phase(
    grok_runner: GrokRunner,
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float,
    phase: str,
) -> str:
    try:
        return grok_runner(argv, env=env, cwd=cwd, timeout=timeout)
    except GrokProcessExit as exc:
        raise exc.with_phase(phase) from exc
    except GrokTimeout as exc:
        raise GrokTimeout(
            f"Grok CLI {phase} phase timed out after {timeout:g} seconds"
        ) from exc


def _validate_workspace_report(
    report: Mapping[str, Any],
    *,
    task_mode: str,
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> set[str]:
    """Bind a terminal report to changes made during this adapter invocation."""
    run_changes = workspace_delta(before, after)
    if task_mode == "review":
        if report["changed_files"]:
            raise AdapterError("A read-only review report must have no changed_files")
        if run_changes:
            raise AdapterError("Read-only review modified the workspace")
    elif report["status"] == "completed":
        reported_changes = set(report["changed_files"])
        if not run_changes:
            raise AdapterError(
                "A completed implementation has no workspace changes from this adapter run"
            )
        if reported_changes != run_changes:
            raise AdapterError(
                "Reported changed_files do not match changes made during this adapter run"
            )
    return run_changes


def _terminal_metadata(report: Mapping[str, Any], session_id: str) -> str:
    metadata = {
        "adapter": PROFILE,
        "task_mode": report.get("task_mode", "implementation"),
        "grok_session_id": session_id,
        "changed_files": report["changed_files"],
        "tests": report["tests"],
        "risks": report["risks"],
        "evidence": report["evidence"],
        "observed_workspace_changes": report.get("observed_workspace_changes", []),
        "preexisting_workspace_changes": report.get(
            "preexisting_workspace_changes", []
        ),
        "observed_run_changes": report.get("observed_run_changes", []),
        "project_context": report.get("project_context", {}),
        "grok_discovery": report.get("grok_discovery", {}),
        "provider_route": report.get("provider_route", "default"),
        "transport": report.get("transport", "headless"),
        "acp": report.get("acp", {}),
        "verification": report.get("verification", {}),
        "adapter_execution": report.get("adapter_execution", {}),
    }
    return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))


def _failure_classification(exc: BaseException, message: str, phase: str | None) -> str:
    if isinstance(exc, GrokNoProgress):
        return "no_progress"
    if isinstance(exc, GrokTimeout):
        return "timeout"
    if isinstance(exc, CapabilityError):
        return "capability"
    if isinstance(exc, ReportContractError):
        return "report_contract"
    if isinstance(exc, GrokProcessExit):
        return "process_exit"
    if isinstance(exc, GrokProviderError):
        return "provider_retryable"
    if isinstance(exc, TerminalStateConflict):
        return "terminal_conflict"
    if "no workspace changes" in message:
        return "no_workspace_change"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "hermes_timeout"
    if isinstance(exc, OSError):
        return "launch_failure"
    if phase == "verification":
        return "verification_error"
    return "transient"


def execute(
    args: argparse.Namespace,
    *,
    command_runner: CommandRunner | None = None,
    grok_runner: GrokRunner | None = None,
    inspect_runner: CommandRunner | None = None,
    acp_runner: Callable[..., AcpWorkResult] | None = None,
) -> int:
    use_runtime_inspector = command_runner is None
    command_runner = command_runner or run_command
    grok_runner = grok_runner or run_grok
    acp_runner = acp_runner or run_grok_acp
    if inspect_runner is None and use_runtime_inspector:
        inspect_runner = run_command
    claimed = False
    session_id = str(uuid.uuid4())
    acp_facts: dict[str, Any] = {}
    adapter_run_id = str(uuid.uuid4())
    run_cleanup_status = "pending"
    terminal_observed_status: str | None = None
    phase_records: list[dict[str, Any]] = []
    current_phase: str | None = None
    phase_started = time.monotonic()

    def enter_phase(name: str) -> None:
        nonlocal current_phase, phase_started
        now = time.monotonic()
        if current_phase is not None:
            phase_records.append({
                "name": current_phase,
                "duration_ms": max(0, int((now - phase_started) * 1000)),
            })
        current_phase = name
        phase_started = now

    def execution_evidence(
        classification: str,
        terminal_action: str,
    ) -> dict[str, Any]:
        phases = list(phase_records)
        if current_phase is not None:
            phases.append({
                "name": current_phase,
                "duration_ms": max(0, int((time.monotonic() - phase_started) * 1000)),
            })
        return {
            "run_id": adapter_run_id,
            "phase": current_phase,
            "classification": classification,
            "terminal_action": terminal_action,
            "terminal_observed_status": terminal_observed_status,
            "process_cleanup": run_cleanup_status,
            "phases": phases,
        }

    def cleanup_adapter_processes() -> None:
        nonlocal run_cleanup_status
        _reap_linux_run_processes_sync(
            adapter_run_id,
            env_key=ADAPTER_RUN_ENV_KEY,
            owner="Grok worker run",
        )
        run_cleanup_status = "passed" if sys.platform == "linux" else "not_supported"

    try:
        enter_phase("policy")
        if args.transport == "headless" and not getattr(
            args, "allow_experimental_headless", False
        ):
            raise CapabilityError(
                "Headless transport requires explicit experimental opt-in"
            )
        acceptance_commands = parse_acceptance_commands(args.acceptance_command)
        env = child_environment(os.environ)
        env[ADAPTER_RUN_ENV_KEY] = adapter_run_id
        provider_routes = provider_route_environments(env, args.provider_route)
        selected_provider_route = "default"
        selected_provider_env: Mapping[str, str] = env
        minimum_ttl = (
            args.timeout * len(provider_routes)
            + args.correction_timeout * (2 if args.transport == "acp" else 1)
            + 3 * args.command_timeout
            + 30
        )
        if args.claim_ttl < minimum_ttl:
            raise AdapterError(
                f"claim TTL must be at least {minimum_ttl:g} seconds for configured timeouts"
            )
        enter_phase("task_lookup")
        task = _hermes(
            args,
            ["show", args.task_id, "--json"],
            env,
            command_runner,
        )
        try:
            task_record = json.loads(task.stdout)
        except json.JSONDecodeError as exc:
            raise AdapterError("Hermes show did not return task JSON") from exc
        task_details = task_record.get("task", task_record)
        if (
            not isinstance(task_details, dict)
            or task_details.get("assignee") != PROFILE
        ):
            raise AdapterError(f"Task must be assigned to {PROFILE} before invocation")
        enter_phase("claim")
        claim = _hermes(
            args,
            ["claim", args.task_id, "--ttl", str(args.claim_ttl)],
            env,
            command_runner,
        )
        claimed = True
        claimed_workspace = parse_claimed_workspace(claim.stdout)
        if args.workspace is None:
            args.workspace = claimed_workspace
        else:
            args.workspace = args.workspace.expanduser().resolve()
            if args.workspace != claimed_workspace:
                raise AdapterError(
                    "Supplied workspace does not match claimed workspace"
                )
        enter_phase("workspace_readiness")
        preflight_snapshot = wait_for_claimed_workspace(
            args.workspace,
            timeout=max(
                min(args.command_timeout, CLAIM_READY_TIMEOUT_SECONDS),
                CLAIM_READY_POLL_SECONDS * (CLAIM_READY_STABLE_POLLS + 2),
            ),
        )
        claimed_head = workspace_head(args.workspace)
        enter_phase("project_context")
        project_context = build_project_context_pack(args.workspace)
        grok_discovery = (
            inspect_grok_environment(args, env, inspect_runner)
            if inspect_runner is not None
            else {"status": "not_run"}
        )
        snapshot_before = workspace_snapshot(args.workspace)
        if workspace_delta(preflight_snapshot, snapshot_before):
            raise AdapterError("Project-context preflight modified the workspace")
        context = _hermes(args, ["context", args.task_id], env, command_runner).stdout
        enter_phase("work_report")
        if args.transport == "acp":
            snapshot_after_work: WorkspaceSnapshot | None = None
            run_changes: set[str] = set()

            def acp_report_prompt(
                stop_reason: str,
                validation_error: AdapterError | None,
            ) -> str:
                nonlocal snapshot_after_work, run_changes
                current_snapshot = workspace_snapshot(args.workspace)
                if snapshot_after_work is None:
                    snapshot_after_work = current_snapshot
                    run_changes = workspace_delta(snapshot_before, snapshot_after_work)
                    if args.task_mode == "review" and run_changes:
                        raise AdapterError("Read-only review modified the workspace")
                elif workspace_delta(snapshot_after_work, current_snapshot):
                    raise AdapterError(
                        "Read-only terminal report modified the workspace"
                    )
                return _terminal_report_prompt(
                    args,
                    sorted(run_changes),
                    stop_reason=stop_reason,
                    validation_error=validation_error,
                )

            def validate_acp_report(candidate: Mapping[str, Any]) -> None:
                nonlocal run_changes
                if snapshot_after_work is None:
                    raise AdapterError(
                        "Grok ACP report phase started before work ended"
                    )
                if workspace_delta(
                    snapshot_after_work,
                    workspace_snapshot(args.workspace),
                ):
                    raise AdapterError(
                        "Read-only terminal report modified the workspace"
                    )
                validate_report(candidate, task_mode=args.task_mode)
                run_changes = _validate_workspace_report(
                    candidate,
                    task_mode=args.task_mode,
                    before=snapshot_before,
                    after=snapshot_after_work,
                )

            def invoke_acp(route_env: Mapping[str, str]) -> AcpWorkResult:
                effective_env = dict(route_env)
                if args.task_mode == "review":
                    effective_env["GROK_SANDBOX"] = "read-only"
                acp_kwargs: dict[str, Any] = {
                    "env": effective_env,
                    "cwd": args.workspace,
                    "prompt": _work_prompt(
                        context,
                        args.task_mode,
                        project_context,
                        grok_discovery,
                    ),
                    "timeout": args.timeout,
                    "no_progress_timeout": (
                        0.0 if args.task_mode == "review" else args.no_progress_timeout
                    ),
                    "progress_probe": lambda: bool(
                        workspace_delta(
                            snapshot_before,
                            workspace_snapshot(args.workspace),
                        )
                    ),
                    "report_prompt_factory": acp_report_prompt,
                    "report_schema": REPORT_SCHEMA,
                    "report_validator": validate_acp_report,
                    "report_timeout": args.correction_timeout,
                }
                runner_parameters = inspect.signature(acp_runner).parameters.values()
                if any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    or parameter.name == "acceptance_probe"
                    for parameter in runner_parameters
                ):
                    acp_kwargs["acceptance_probe"] = lambda: run_adapter_verification(
                        acceptance_commands,
                        env=effective_env,
                        cwd=args.workspace,
                        timeout=args.command_timeout,
                    )
                return acp_runner(_acp_command(args), **acp_kwargs)

            work_result, selected_provider_route = run_with_provider_failover(
                invoke_acp,
                provider_routes,
                workspace=args.workspace,
                pristine_snapshot=snapshot_before,
            )
            if work_result.stop_reason != "end_turn":
                raise AdapterError(
                    "Grok ACP work phase ended without a terminal turn: "
                    f"{work_result.stop_reason}"
                )
            session_id = work_result.session_id
            acp_facts = {
                "stop_reason": work_result.stop_reason,
                "tool_call_count": work_result.tool_call_count,
                "update_count": work_result.update_count,
                "max_update_json_bytes": work_result.max_update_json_bytes,
            }
            if work_result.report is None or snapshot_after_work is None:
                raise AdapterError("Grok ACP terminal report is missing")
            report = dict(work_result.report)
            validate_acp_report(report)
            snapshot_after = snapshot_after_work
            acp_acceptance = work_result.acceptance
        else:
            acp_acceptance = None

            def invoke_headless(route_env: Mapping[str, str]) -> str:
                nonlocal selected_provider_env
                output = _run_grok_phase(
                    grok_runner,
                    _grok_command(
                        args,
                        session_id,
                        _prompt(
                            context,
                            args.task_mode,
                            project_context,
                            grok_discovery,
                        ),
                    ),
                    env=route_env,
                    cwd=args.workspace,
                    timeout=args.timeout,
                    phase="initial",
                )
                selected_provider_env = route_env
                return output

            stdout, selected_provider_route = run_with_provider_failover(
                invoke_headless,
                provider_routes,
                workspace=args.workspace,
                pristine_snapshot=snapshot_before,
            )
            snapshot_after_initial = workspace_snapshot(args.workspace)
            initial_run_changes = workspace_delta(
                snapshot_before, snapshot_after_initial
            )
            if args.task_mode == "review" and initial_run_changes:
                # Do not let a second model turn hide a read-only violation by
                # restoring the files it mutated in the first turn.
                raise AdapterError("Read-only review modified the workspace")
            try:
                report = parse_report(stdout, task_mode=args.task_mode)
                run_changes = _validate_workspace_report(
                    report,
                    task_mode=args.task_mode,
                    before=snapshot_before,
                    after=snapshot_after_initial,
                )
                snapshot_after = snapshot_after_initial
            except AdapterError as validation_error:
                stdout = _run_grok_phase(
                    grok_runner,
                    _resume_command(
                        args,
                        session_id,
                        validation_error,
                        sorted(initial_run_changes),
                    ),
                    env=selected_provider_env,
                    cwd=args.workspace,
                    timeout=args.correction_timeout,
                    phase="correction",
                )
                report = parse_report(stdout, task_mode=args.task_mode)
                snapshot_after = workspace_snapshot(args.workspace)
                run_changes = _validate_workspace_report(
                    report,
                    task_mode=args.task_mode,
                    before=snapshot_before,
                    after=snapshot_after,
                )

        report["preexisting_workspace_changes"] = sorted(snapshot_before)
        report["observed_run_changes"] = sorted(run_changes)
        report["observed_workspace_changes"] = sorted(snapshot_after)
        report["project_context"] = {
            "root": project_context["root"],
            "manifests": project_context["manifests"],
            "verify_commands": project_context["verify_commands"],
            "context_files": project_context["context_files"],
        }
        report["grok_discovery"] = grok_discovery
        report["provider_route"] = selected_provider_route
        report["transport"] = args.transport
        report["acp"] = acp_facts
        if workspace_head(args.workspace) != claimed_head:
            raise AdapterError("Grok work changed the claimed Git HEAD")
        enter_phase("verification")
        source_verification_snapshot_before = workspace_snapshot(args.workspace)
        source_verification_head_before = workspace_head(args.workspace)
        verification = acp_acceptance or run_adapter_verification(
            acceptance_commands,
            env=env,
            cwd=args.workspace,
            timeout=args.command_timeout,
        )
        source_verification_snapshot_after = workspace_snapshot(args.workspace)
        source_verification_head_after = workspace_head(args.workspace)
        source_verification_mutations = sorted(
            workspace_delta(
                source_verification_snapshot_before,
                source_verification_snapshot_after,
            )
        )
        verification["source_workspace_mutation_count"] = len(
            source_verification_mutations
        )
        verification["source_workspace_mutations"] = source_verification_mutations[
            :VERIFICATION_MUTATION_PATH_LIMIT
        ]
        verification["source_workspace_mutations_truncated"] = (
            len(source_verification_mutations) > VERIFICATION_MUTATION_PATH_LIMIT
        )
        verification["source_head_before"] = source_verification_head_before
        verification["source_head_after"] = source_verification_head_after
        verification["source_head_changed"] = (
            source_verification_head_before != source_verification_head_after
        )
        if source_verification_mutations or verification["source_head_changed"]:
            verification["status"] = "failed"
        report["observed_workspace_changes"] = sorted(
            source_verification_snapshot_after
        )
        report["verification"] = verification
        if verification["status"] == "failed" and report["status"] == "completed":
            verification_mutations = list(verification["workspace_mutations"])
            if verification["source_head_changed"]:
                failure = "Adapter-owned verification changed source Git HEAD"
                failure_evidence = (
                    f"{failure}; before={source_verification_head_before}; "
                    f"after={source_verification_head_after}"
                )
            elif source_verification_mutations:
                failure = (
                    "Adapter-owned verification modified the source workspace "
                    f"({len(source_verification_mutations)} path(s))"
                )
                failure_evidence = (
                    f"{failure}; paths="
                    f"{json.dumps(verification['source_workspace_mutations'])}"
                )
            elif verification["head_changed"]:
                failure = "Adapter-owned verification changed Git HEAD"
                failure_evidence = (
                    f"{failure}; before={verification['head_before']}; "
                    f"after={verification['head_after']}"
                )
            elif verification_mutations:
                failure = (
                    "Adapter-owned verification modified the workspace "
                    f"({len(verification_mutations)} path(s))"
                )
                failure_evidence = (
                    f"{failure}; paths="
                    f"{json.dumps(verification['workspace_mutations'])}"
                )
            else:
                failed_commands = [
                    result
                    for result in verification["commands"]
                    if result["timed_out"] or result["returncode"] != 0
                ]
                first_failure = failed_commands[0]
                if first_failure["timed_out"]:
                    failure = (
                        "Adapter-owned verification timed out after "
                        f"{first_failure['timeout_seconds']:g} seconds"
                    )
                else:
                    failure = (
                        "Adapter-owned verification exited with code "
                        f"{first_failure['returncode']}"
                    )
                failure_evidence = (
                    f"{failure}; command_sha256={first_failure['command_sha256']}"
                )
            report["status"] = "blocked"
            report["summary"] = "Blocked: adapter-owned verification failed."
            report["block_reason"] = failure
            report["block_kind"] = "transient"
            report["evidence"] = [
                *report["evidence"],
                failure_evidence,
            ]

        cleanup_adapter_processes()
        enter_phase("terminal")
        terminal_probe = _hermes(
            args,
            ["show", args.task_id, "--json"],
            env,
            command_runner,
        )
        try:
            terminal_record = json.loads(terminal_probe.stdout)
        except json.JSONDecodeError as exc:
            raise AdapterError(
                "Hermes terminal preflight did not return task JSON"
            ) from exc
        terminal_details = terminal_record.get("task", terminal_record)
        if not isinstance(terminal_details, dict):
            raise AdapterError("Hermes terminal preflight returned invalid task JSON")
        observed_status = terminal_details.get("status")
        terminal_observed_status = (
            observed_status if isinstance(observed_status, str) else None
        )
        reviewer = (
            str(getattr(args, "reviewer", "") or "").strip()
            if args.task_mode == "implementation"
            else ""
        )
        if report["status"] == "completed":
            desired_action = "request_review" if reviewer else "complete"
        else:
            desired_action = "block"
        if desired_action == "request_review" and terminal_observed_status == "review":
            terminal_reviewer = terminal_details.get("assignee")
            if not isinstance(terminal_reviewer, str) or (
                terminal_reviewer.strip().casefold() != reviewer.casefold()
            ):
                raise TerminalStateConflict(
                    "Hermes task entered review with a different reviewer "
                    "before adapter finalization"
                )
            terminal_action = "already_requested_review"
        elif desired_action == "complete" and terminal_observed_status in {
            "done",
            "archived",
        }:
            terminal_action = "already_complete"
        elif desired_action == "block" and terminal_observed_status == "blocked":
            terminal_action = "already_blocked"
        elif terminal_observed_status in {"done", "archived", "blocked", "review"}:
            raise TerminalStateConflict(
                "Hermes task reached a conflicting terminal state before adapter finalization"
            )
        else:
            terminal_action = desired_action
        if report["status"] == "completed":
            report["adapter_execution"] = execution_evidence(
                "completed", terminal_action
            )
            if terminal_action == "request_review":
                raw_run_id = terminal_details.get("current_run_id")
                if isinstance(raw_run_id, bool):
                    raise AdapterError(
                        "Hermes terminal preflight did not expose the current run id"
                    )
                try:
                    current_run_id = int(raw_run_id)
                except (TypeError, ValueError) as exc:
                    raise AdapterError(
                        "Hermes terminal preflight did not expose the current run id"
                    ) from exc
                if current_run_id < 1:
                    raise AdapterError(
                        "Hermes terminal preflight returned an invalid current run id"
                    )
                terminal_env = dict(env)
                terminal_env["HERMES_KANBAN_TASK"] = args.task_id
                terminal_env["HERMES_KANBAN_RUN_ID"] = str(current_run_id)
                _hermes(
                    args,
                    [
                        "request-review",
                        args.task_id,
                        "--summary",
                        report["summary"],
                        "--reviewer",
                        reviewer,
                        "--metadata",
                        _terminal_metadata(
                            {**report, "task_mode": args.task_mode}, session_id
                        ),
                    ],
                    terminal_env,
                    command_runner,
                )
            elif terminal_action == "complete":
                _hermes(
                    args,
                    [
                        "complete",
                        args.task_id,
                        "--result",
                        report["summary"],
                        "--summary",
                        report["summary"],
                        "--metadata",
                        _terminal_metadata(
                            {**report, "task_mode": args.task_mode}, session_id
                        ),
                    ],
                    env,
                    command_runner,
                )
        else:
            classification = (
                "verification_failed"
                if verification["status"] == "failed"
                else "model_blocked"
            )
            report["adapter_execution"] = execution_evidence(
                classification, terminal_action
            )
            if terminal_action == "block":
                _hermes(
                    args,
                    [
                        "block",
                        "--kind",
                        report["block_kind"],
                        args.task_id,
                        report["block_reason"],
                    ],
                    env,
                    command_runner,
                )
        cleanup_adapter_processes()
        report["adapter_execution"]["process_cleanup"] = run_cleanup_status
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (AdapterError, subprocess.TimeoutExpired, OSError) as exc:
        if isinstance(exc, AdapterError):
            message = str(exc)
        elif isinstance(exc, subprocess.TimeoutExpired):
            message = "Hermes command timed out"
        else:
            message = f"Subprocess launch failed: {type(exc).__name__}"
        block_kind = "capability" if isinstance(exc, CapabilityError) else "transient"
        classification = _failure_classification(exc, message, current_phase)
        try:
            cleanup_adapter_processes()
        except AdapterError as cleanup_exc:
            message = str(cleanup_exc)
            block_kind = "transient"
            classification = "process_cleanup_failed"
            run_cleanup_status = "failed"
        should_block = claimed and not isinstance(exc, TerminalStateConflict)
        if should_block:
            try:
                _hermes(
                    args,
                    ["block", "--kind", block_kind, args.task_id, message],
                    env,
                    command_runner,
                )
            except AdapterError:
                print(
                    json.dumps(
                        {
                            "status": "failed",
                            "block_reason": message,
                            "block_kind": block_kind,
                            "adapter_execution": execution_evidence(
                                "terminal_transition_failed", "block_failed"
                            ),
                        },
                        ensure_ascii=False,
                    )
                )
                print(
                    "Adapter failed and could not record terminal state",
                    file=sys.stderr,
                )
                return 2
        print(
            json.dumps(
                {
                    "status": "blocked" if should_block else "failed",
                    "block_reason": message,
                    "block_kind": block_kind,
                    "adapter_execution": execution_evidence(
                        classification,
                        "block"
                        if should_block
                        else ("conflict" if claimed else "none"),
                    ),
                },
                ensure_ascii=False,
            )
        )
        print(message, file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    parser.add_argument("--board", required=True)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--grok-bin", default="grok")
    parser.add_argument("--hermes-bin", default="hermes")
    parser.add_argument("--model", default="grok-4.6")
    parser.add_argument("--agent", default="general-purpose")
    parser.add_argument("--transport", choices=("acp", "headless"), default="acp")
    parser.add_argument("--allow-experimental-headless", action="store_true")
    parser.add_argument("--reasoning-effort", default="max")
    parser.add_argument(
        "--task-mode",
        choices=("implementation", "review"),
        default="implementation",
    )
    parser.add_argument(
        "--reviewer",
        default="worker-luna",
        help=(
            "Hermes profile that reviews successful implementation runs; "
            "pass an empty value only for explicit unsupervised compatibility"
        ),
    )
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--correction-timeout", type=float, default=120.0)
    parser.add_argument("--no-progress-timeout", type=float, default=300.0)
    parser.add_argument("--command-timeout", type=float, default=30.0)
    parser.add_argument(
        "--acceptance-command",
        action="append",
        default=[],
        metavar="JSON",
        help=(
            "required repeatable JSON descriptor: "
            '{"label":"tests","argv":["python","-m","pytest"],"timeout":300}'
        ),
    )
    parser.add_argument(
        "--provider-route",
        action="append",
        default=[],
        metavar="JSON",
        help="optional repeatable JSON descriptor naming endpoint/key env variables",
    )
    parser.add_argument("--claim-ttl", type=int, default=1500)
    parser.add_argument("--max-turns", type=int, default=64)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return execute(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
