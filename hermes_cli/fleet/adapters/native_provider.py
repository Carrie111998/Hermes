"""Exact-provider native Hermes execution contract."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .base import validate_execution
from ..types import (
    AdapterKind,
    AdapterRequest,
    AdapterResult,
    Qualification,
    ReasonCode,
)


class NativeProviderAdapter:
    """Run an injected Hermes child while enforcing pinned provenance.

    The runner boundary lets the CLI wire the existing Hermes child process
    without mutating the parent conversation's provider or model.
    """

    def __init__(self, runner: Callable[..., Mapping[str, Any]]) -> None:
        self._runner = runner

    @staticmethod
    def _failure(
        request: AdapterRequest,
        qualification: Qualification,
        reason: ReasonCode,
    ) -> AdapterResult:
        return AdapterResult(
            ok=False,
            reason=reason,
            provider_id=request.profile.provider_id,
            model_id=request.model,
            auth_kind=qualification.auth_kind or "unknown",
            adapter_kind=AdapterKind.NATIVE_PROVIDER,
        )

    def execute(
        self, request: AdapterRequest, qualification: Qualification
    ) -> AdapterResult:
        failure = validate_execution(request, qualification)
        if failure is not None:
            return self._failure(request, qualification, failure)
        try:
            payload = self._runner(
                provider_id=request.profile.provider_id,
                model=request.model,
                effort=request.effort,
                fast_mode=False,
                fallback_enabled=False,
                cwd=request.cwd,
                prompt=request.prompt,
                timeout_seconds=request.timeout_seconds,
            )
        except TimeoutError:
            return self._failure(
                request, qualification, ReasonCode.EXECUTION_TIMEOUT
            )
        except Exception:
            return self._failure(
                request, qualification, ReasonCode.EXECUTION_FAILED
            )
        if not isinstance(payload, Mapping):
            return self._failure(
                request, qualification, ReasonCode.MALFORMED_OUTPUT
            )
        if payload.get("provider_id") != request.profile.provider_id:
            return self._failure(
                request, qualification, ReasonCode.PROVIDER_MISMATCH
            )
        if payload.get("model_id") != request.model:
            return self._failure(
                request, qualification, ReasonCode.MODEL_MISMATCH
            )
        if payload.get("auth_kind") != qualification.auth_kind:
            return self._failure(
                request, qualification, ReasonCode.CREDENTIAL_MISMATCH
            )
        if payload.get("ok") is not True or not isinstance(
            payload.get("output", ""), str
        ):
            return self._failure(
                request, qualification, ReasonCode.EXECUTION_FAILED
            )
        metadata = {
            str(key): value
            for key, value in payload.items()
            if key not in {"ok", "provider_id", "model_id", "auth_kind", "output"}
        }
        return AdapterResult(
            ok=True,
            reason=ReasonCode.MET,
            provider_id=request.profile.provider_id,
            model_id=request.model,
            auth_kind=qualification.auth_kind or "unknown",
            adapter_kind=AdapterKind.NATIVE_PROVIDER,
            output=payload["output"],
            metadata=metadata,
        )
