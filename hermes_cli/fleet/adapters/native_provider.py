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
        except ValueError:
            return self._failure(
                request, qualification, ReasonCode.MALFORMED_OUTPUT
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
        if payload.get("effort") != request.effort:
            return self._failure(
                request, qualification, ReasonCode.QUALIFICATION_FAILED
            )
        if (
            qualification.auth_kind != "oauth_subscription"
            or payload.get("auth_kind") != "oauth_subscription"
            or payload.get("auth_source") != qualification.auth_source
        ):
            return self._failure(
                request, qualification, ReasonCode.CREDENTIAL_MISMATCH
            )
        if (
            payload.get("fallback_enabled") is not False
            or payload.get("fast_mode") is not False
        ):
            return self._failure(
                request, qualification, ReasonCode.QUALIFICATION_FAILED
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
            if key
            not in {
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
        }
        metadata["route_proof"] = {
            "auth_source": qualification.auth_source,
            "qualification": qualification.evidence_id,
            "effort": request.effort,
            "fallback_enabled": False,
            "fast_mode": False,
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
