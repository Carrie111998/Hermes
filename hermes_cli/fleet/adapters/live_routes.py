"""Concrete subscription-only execution adapters for current fleet lanes."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .base import safe_child_environment, validate_execution
from .external_cli import ExternalCliAdapter
from .native_provider import NativeProviderAdapter
from ..types import AdapterKind, AdapterRequest, AdapterResult, Qualification, ReasonCode


def run_native_hermes_child(**request: Any) -> Mapping[str, Any]:
    """Run a fresh exact-provider Hermes child; never mutate the parent model."""

    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_constants import parse_reasoning_effort
    from run_agent import AIAgent

    provider = str(request["provider_id"])
    model = str(request["model"])
    runtime = resolve_runtime_provider(requested=provider, target_model=model)
    if runtime.get("provider") != provider:
        raise RuntimeError("resolved provider identity mismatch")
    agent = AIAgent(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=provider,
        api_mode=runtime.get("api_mode"),
        credential_pool=runtime.get("credential_pool"),
        model=model,
        reasoning_config=parse_reasoning_effort(request["effort"]),
        fallback_model=None,
        quiet_mode=True,
        platform="subagent",
        skip_context_files=False,
        skip_memory=False,
    )
    try:
        result = agent.run_conversation(str(request["prompt"]))
        output = result.get("final_response")
        if not isinstance(output, str):
            raise RuntimeError("native child returned no final response")
        return {
            "ok": True,
            "provider_id": provider,
            "model_id": model,
            "auth_kind": "oauth_subscription",
            "output": output,
            "fallback_enabled": False,
            "fast_mode": False,
        }
    finally:
        agent.close()


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
) -> dict[str, object]:
    return {
        "chatgpt_codex": NativeProviderAdapter(native_runner),
        "claude_code": ClaudeCodeAdapter(),
        "grok": NativeProviderAdapter(native_runner),
        "antigravity": AntigravityAdapter(),
    }
