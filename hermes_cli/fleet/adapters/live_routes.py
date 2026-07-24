"""Concrete subscription-only execution adapters for current fleet lanes."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .base import safe_child_environment, validate_execution
from .external_cli import ExternalCliAdapter
from .native_provider import NativeProviderAdapter
from ..types import AdapterKind, AdapterRequest, AdapterResult, Qualification, ReasonCode


_NATIVE_CHILD = Path(__file__).resolve().parents[1] / "native_child.py"
_NATIVE_SUCCESS_FIELDS = frozenset(
    {
        "ok",
        "provider_id",
        "model_id",
        "effort",
        "auth_kind",
        "auth_source",
        "fallback_enabled",
        "fast_mode",
        "output",
    }
)
_MAX_STDERR_CHARS = 4096


def _stderr_receipt(stderr: str | bytes | None) -> str:
    """Return bounded diagnostics without retaining or echoing stderr text."""

    if isinstance(stderr, bytes):
        raw = stderr[:_MAX_STDERR_CHARS]
    else:
        raw = str(stderr or "")[:_MAX_STDERR_CHARS].encode(
            "utf-8", errors="replace"
        )
    return f"bytes={len(raw)} sha256={hashlib.sha256(raw).hexdigest()}"


def run_native_hermes_child(**request: Any) -> Mapping[str, Any]:
    """Run the exact native route in a killable, isolated Python child."""

    child_request = {
        "provider_id": str(request["provider_id"]),
        "model": str(request["model"]),
        "effort": str(request["effort"]),
        "prompt": str(request["prompt"]),
    }
    process = subprocess.Popen(
        [sys.executable, str(_NATIVE_CHILD)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path(request["cwd"]),
        env=safe_child_environment(),
        shell=False,
    )
    try:
        stdout, stderr = process.communicate(
            json.dumps(child_request, separators=(",", ":"), sort_keys=True),
            timeout=request["timeout_seconds"],
        )
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise TimeoutError("native child execution timed out") from exc
    stderr_receipt = _stderr_receipt(stderr)
    if process.returncode != 0:
        raise RuntimeError(
            f"native child exited {process.returncode}; stderr {stderr_receipt}"
        )
    if not isinstance(stdout, str) or stdout != stdout.strip():
        raise ValueError("native child stdout was not a strict JSON envelope")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("native child stdout was not JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _NATIVE_SUCCESS_FIELDS:
        raise ValueError("native child stdout envelope fields were invalid")
    return payload


class _SubscriptionCliAdapter(ExternalCliAdapter):
    def __init__(
        self,
        executable: str,
        *,
        lane: str,
        run_process: Callable[..., Any] = subprocess.run,
    ) -> None:
        super().__init__(executable)
        self.lane = lane
        self._run_process = run_process

    def _argv(self, executable: Path, request: AdapterRequest) -> list[str]:
        if self.lane == "claude_code":
            return [
                str(executable),
                "-p",
                "--model",
                request.model,
                "--effort",
                request.effort,
                "--output-format",
                "json",
            ]
        return [
            str(executable),
            "-p",
            "--model",
            request.model,
            "--effort",
            request.effort,
            "--print-timeout",
            f"{request.timeout_seconds}s",
        ]

    def execute(
        self, request: AdapterRequest, qualification: Qualification
    ) -> AdapterResult:
        failure = validate_execution(request, qualification)
        if failure is not None:
            return self._failure(request, qualification, failure)
        executable = self._resolved_executable()
        if (
            executable is None
            or qualification.executable is None
            or executable != Path(qualification.executable).resolve()
        ):
            return self._failure(request, qualification, ReasonCode.QUALIFICATION_FAILED)
        try:
            completed = self._run_process(
                self._argv(executable, request),
                input=request.prompt,
                capture_output=True,
                text=True,
                cwd=request.cwd,
                env=safe_child_environment(),
                timeout=request.timeout_seconds,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return self._failure(request, qualification, ReasonCode.EXECUTION_TIMEOUT)
        except OSError:
            return self._failure(request, qualification, ReasonCode.EXECUTION_FAILED)
        if completed.returncode != 0:
            return self._failure(request, qualification, ReasonCode.EXECUTION_FAILED)
        output = completed.stdout
        metadata: dict[str, object] = {
            "route_proof": {
                "executable": str(executable),
                "version": qualification.version,
                "requested_model_id": request.model,
                "effort": request.effort,
                "auth_kind": qualification.auth_kind,
                "fast_mode": False,
                "fallback_enabled": False,
            }
        }
        if self.lane == "claude_code":
            try:
                payload = json.loads(output)
            except (TypeError, json.JSONDecodeError):
                return self._failure(request, qualification, ReasonCode.MALFORMED_OUTPUT)
            if not isinstance(payload, dict) or not isinstance(payload.get("result"), str):
                return self._failure(request, qualification, ReasonCode.MALFORMED_OUTPUT)
            usage = payload.get("modelUsage")
            if isinstance(usage, dict) and usage and request.model not in usage:
                return self._failure(request, qualification, ReasonCode.MODEL_MISMATCH)
            output = payload["result"]
            metadata["cli_receipt"] = {
                key: payload[key]
                for key in ("session_id", "is_error", "num_turns")
                if key in payload
            }
        else:
            if not isinstance(output, str) or not output.strip():
                return self._failure(
                    request, qualification, ReasonCode.MALFORMED_OUTPUT
                )
            metadata["route_proof"]["model_qualification"] = "agy models"
            metadata["route_proof"]["served_model_id"] = None
        return AdapterResult(
            ok=True,
            reason=ReasonCode.MET,
            provider_id=request.profile.provider_id,
            model_id=request.model,
            auth_kind=qualification.auth_kind or "unknown",
            adapter_kind=AdapterKind.EXTERNAL_CLI,
            output=output,
            metadata=metadata,
        )


class ClaudeCodeAdapter(_SubscriptionCliAdapter):
    def __init__(self, executable: str = "claude", **kwargs: Any) -> None:
        super().__init__(executable, lane="claude_code", **kwargs)


class AntigravityAdapter(_SubscriptionCliAdapter):
    def __init__(self, executable: str = "agy", **kwargs: Any) -> None:
        super().__init__(executable, lane="antigravity", **kwargs)


def live_adapters(
    *,
    native_runner: Callable[..., Mapping[str, Any]] = run_native_hermes_child,
    qualifications: Mapping[str, Qualification] | None = None,
) -> dict[str, object]:
    qualifications = qualifications or {}
    claude = qualifications.get("claude_code")
    antigravity = qualifications.get("antigravity")
    return {
        "chatgpt_codex": NativeProviderAdapter(native_runner),
        "claude_code": ClaudeCodeAdapter(
            claude.executable if claude and claude.qualified and claude.executable else "claude"
        ),
        "grok": NativeProviderAdapter(native_runner),
        "antigravity": AntigravityAdapter(
            antigravity.executable
            if antigravity and antigravity.qualified and antigravity.executable
            else "agy"
        ),
    }
