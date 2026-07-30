"""Bounded subprocess transport for the official Claude Code executable."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


class ClaudeCLIError(RuntimeError):
    reason = "execution"

    def __init__(self, message: str, *, exit_code: int | None = None):
        super().__init__(message)
        self.exit_code = exit_code


class ClaudeCLIUnavailableError(ClaudeCLIError):
    reason = "unreachable"


class ClaudeCLIAuthenticationError(ClaudeCLIError):
    reason = "authentication_required"


class ClaudeCLIQuotaError(ClaudeCLIError):
    reason = "quota_exhausted"


class ClaudeCLITimeoutError(ClaudeCLIError):
    reason = "timeout"


class ClaudeCLIExecutionError(ClaudeCLIError):
    reason = "execution"


class ClaudeCLIStaleSessionError(ClaudeCLIExecutionError):
    reason = "stale_session"


@dataclass(frozen=True)
class ClaudeCLIRunResult:
    decision: dict[str, Any]
    session_id: str
    model_reported: str | None
    exit_code: int
    duration_seconds: float
    argv: tuple[str, ...]
    shell: bool = False


_EXACT_SECRET_NAMES = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "HERMES_MODEL_PROVIDER",
}


def _sanitized_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Preserve the user environment while removing provider credential overrides."""

    clean = dict(source or os.environ)
    for name in list(clean):
        upper = name.upper()
        if upper in _EXACT_SECRET_NAMES or (
            upper.startswith("HERMES_") and upper.endswith("_API_KEY")
        ):
            clean.pop(name, None)
    return clean


class ClaudeCLIProcessRunner:
    """Launch and cancel one official-Claude CLI request at a time."""

    def __init__(
        self,
        *,
        executable: str = "claude",
        executable_args: Sequence[str] | None = None,
        timeout_seconds: float = 600,
        environment: Mapping[str, str] | None = None,
    ):
        self.executable = executable
        self.executable_args = tuple(executable_args or ())
        self.timeout_seconds = float(timeout_seconds)
        self._environment = environment
        self._lock = threading.RLock()
        self._active_process: subprocess.Popen[str] | None = None

    @property
    def active_pid(self) -> int | None:
        with self._lock:
            process = self._active_process
            return process.pid if process and process.poll() is None else None

    def _base_argv(self) -> list[str]:
        return [self.executable, *self.executable_args]

    @staticmethod
    def _classify_failure(stderr: str, stdout: str, exit_code: int) -> ClaudeCLIError:
        text = f"{stderr}\n{stdout}".lower()
        if any(
            marker in text
            for marker in (
                "authentication required",
                "please run /login",
                "not logged in",
                "login required",
            )
        ):
            return ClaudeCLIAuthenticationError(
                "Claude Code authentication is required",
                exit_code=exit_code,
            )
        if any(
            marker in text
            for marker in (
                "usage limit",
                "rate limit",
                "quota",
                "extra usage",
                "limit reached",
            )
        ):
            return ClaudeCLIQuotaError(
                "Claude subscription capacity is exhausted",
                exit_code=exit_code,
            )
        if any(
            marker in text
            for marker in (
                "no conversation found",
                "unknown session",
                "session not found",
                "invalid session",
            )
        ):
            return ClaudeCLIStaleSessionError(
                "Claude provider session is stale",
                exit_code=exit_code,
            )
        return ClaudeCLIExecutionError(
            f"Claude CLI exited with status {exit_code}",
            exit_code=exit_code,
        )

    def _terminate_process_tree(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    shell=False,
                    timeout=10,
                )
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def close(self) -> None:
        with self._lock:
            process = self._active_process
        if process is not None:
            self._terminate_process_tree(process)
        with self._lock:
            if self._active_process is process:
                self._active_process = None

    def _run(
        self,
        argv: Sequence[str],
        *,
        input_text: str = "",
        timeout_seconds: float | None = None,
    ) -> tuple[str, str, int, float]:
        started = time.monotonic()
        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": _sanitized_env(self._environment),
            "shell": False,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            popen_kwargs["start_new_session"] = True

        try:
            process = subprocess.Popen(list(argv), **popen_kwargs)
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise ClaudeCLIUnavailableError(
                f"Could not launch Claude CLI executable {self.executable!r}"
            ) from exc

        with self._lock:
            self._active_process = process
        try:
            try:
                stdout, stderr = process.communicate(
                    input=input_text,
                    timeout=timeout_seconds or self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                self._terminate_process_tree(process)
                raise ClaudeCLITimeoutError("Claude CLI request timed out") from exc
        finally:
            with self._lock:
                if self._active_process is process:
                    self._active_process = None

        duration = time.monotonic() - started
        return stdout, stderr, int(process.returncode or 0), duration

    def version(self) -> str:
        argv = [*self._base_argv(), "--version"]
        stdout, stderr, code, _ = self._run(argv, timeout_seconds=30)
        if code != 0:
            raise self._classify_failure(stderr, stdout, code)
        version = stdout.strip()
        if not version:
            raise ClaudeCLIExecutionError("Claude CLI returned an empty version")
        return version

    def auth_status(self) -> dict[str, Any]:
        argv = [*self._base_argv(), "auth", "status", "--json"]
        stdout, stderr, code, _ = self._run(argv, timeout_seconds=30)
        if code != 0:
            raise self._classify_failure(stderr, stdout, code)
        try:
            status = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ClaudeCLIExecutionError(
                "Claude CLI returned malformed authentication status"
            ) from exc
        if not isinstance(status, dict):
            raise ClaudeCLIExecutionError(
                "Claude CLI authentication status is not an object"
            )
        if not status.get("loggedIn") or status.get("apiProvider") != "firstParty":
            raise ClaudeCLIAuthenticationError(
                "Claude Code is not logged in through its first-party account"
            )
        return status

    def complete(
        self,
        *,
        prompt: str,
        schema_json: str,
        model: str | None = None,
        new_session_id: str | None = None,
        resume_session_id: str | None = None,
    ) -> ClaudeCLIRunResult:
        if bool(new_session_id) == bool(resume_session_id):
            raise ValueError("Exactly one Claude session selector is required")

        argv = [
            *self._base_argv(),
            "--print",
            "--output-format",
            "json",
            "--json-schema",
            schema_json,
            "--tools",
            "",
        ]
        if model:
            argv.extend(["--model", model])
        if new_session_id:
            argv.extend(["--session-id", new_session_id])
        else:
            argv.extend(["--resume", str(resume_session_id)])

        stdout, stderr, code, duration = self._run(argv, input_text=prompt)
        if code != 0:
            raise self._classify_failure(stderr, stdout, code)
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ClaudeCLIExecutionError("Claude CLI returned malformed result JSON") from exc
        if not isinstance(payload, dict):
            raise ClaudeCLIExecutionError("Claude CLI result is not an object")

        decision = payload.get("structured_output")
        if decision is None:
            result = payload.get("result")
            if isinstance(result, str):
                try:
                    decision = json.loads(result)
                except json.JSONDecodeError as exc:
                    raise ClaudeCLIExecutionError(
                        "Claude CLI result field is not structured JSON"
                    ) from exc
        if not isinstance(decision, dict):
            raise ClaudeCLIExecutionError(
                "Claude CLI result omitted structured output"
            )

        provider_session_id = payload.get("session_id")
        if not isinstance(provider_session_id, str) or not provider_session_id:
            provider_session_id = new_session_id or resume_session_id or ""
        model_reported = payload.get("model")
        if not isinstance(model_reported, str) or not model_reported:
            model_reported = None

        return ClaudeCLIRunResult(
            decision=dict(decision),
            session_id=provider_session_id,
            model_reported=model_reported,
            exit_code=code,
            duration_seconds=duration,
            argv=tuple(argv),
        )
