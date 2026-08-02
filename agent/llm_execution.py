"""Execution policy primitives shared by host-owned LLM call surfaces.

The default policy deliberately carries no new behavior.  Strict execution is
an opt-in client-side contract: Hermes dispatches one explicit route at most
once and does not enter its retry, credential-rotation, or fallback recovery
chains.  A provider may still accept a request before a timeout or connection
failure becomes visible to the client, so strict execution is not an
exactly-once delivery guarantee.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class LlmExecutionMode(StrEnum):
    """Host policy for one LLM invocation."""

    DEFAULT = "default"
    STRICT_SINGLE_ATTEMPT = "strict_single_attempt"


class LlmExecutionPolicyError(RuntimeError):
    """Base class for execution-policy failures."""


class StrictExecutionConfigurationError(LlmExecutionPolicyError, ValueError):
    """Strict execution was requested without a usable explicit route."""


class StrictExecutionUnsupported(LlmExecutionPolicyError):
    """The selected transport cannot prove the strict host contract."""


class StrictExecutionRouteMismatch(LlmExecutionPolicyError):
    """Route resolution changed the provider or model requested by the caller."""


@dataclass
class LlmExecutionAudit:
    """Secret-free execution facts for one host-owned LLM call."""

    execution_mode: str = LlmExecutionMode.DEFAULT.value
    requested_provider: str = ""
    requested_model: str = ""
    dispatched_provider: str = ""
    dispatched_model: str = ""
    response_provider: str = ""
    response_model: str = ""
    attempt_count: int = 0
    fallback_used: bool = False
    credential_rotation_used: bool = False
    route_changed: bool = False
    delivery_ambiguous: bool = False
    strict_contract_satisfied: bool = False

    def begin(
        self,
        mode: LlmExecutionMode | str,
        *,
        provider: str | None,
        model: str | None,
    ) -> None:
        resolved_mode = coerce_execution_mode(mode)
        self.execution_mode = resolved_mode.value
        self.requested_provider = str(provider or "").strip()
        self.requested_model = str(model or "").strip()
        self.dispatched_provider = ""
        self.dispatched_model = ""
        self.response_provider = ""
        self.response_model = ""
        self.attempt_count = 0
        self.fallback_used = False
        self.credential_rotation_used = False
        self.route_changed = False
        self.delivery_ambiguous = False
        self.strict_contract_satisfied = False

    def record_dispatch(self, provider: str | None, model: str | None) -> None:
        self.dispatched_provider = str(provider or "").strip()
        self.dispatched_model = str(model or "").strip()
        self.route_changed = not routes_match(
            self.requested_provider,
            self.requested_model,
            self.dispatched_provider,
            self.dispatched_model,
        )

    def record_attempt(self) -> None:
        self.attempt_count += 1

    def record_response(self, response: Any) -> None:
        response_provider = getattr(response, "provider", None)
        response_model = getattr(response, "model", None)
        self.response_provider = str(
            response_provider or self.dispatched_provider
        ).strip()
        self.response_model = str(response_model or self.dispatched_model).strip()
        self.strict_contract_satisfied = (
            _strict_mode(self.execution_mode)
            and self.attempt_count == 1
            and not self.fallback_used
            and not self.credential_rotation_used
            and not self.route_changed
        )

    def record_failure(self, exc: BaseException) -> None:
        self.delivery_ambiguous = _delivery_may_be_ambiguous(exc)
        self.strict_contract_satisfied = (
            _strict_mode(self.execution_mode)
            and self.attempt_count <= 1
            and not self.fallback_used
            and not self.credential_rotation_used
            and not self.route_changed
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def coerce_execution_mode(mode: LlmExecutionMode | str) -> LlmExecutionMode:
    """Normalize a public execution mode or fail before route resolution."""

    if isinstance(mode, LlmExecutionMode):
        return mode
    try:
        return LlmExecutionMode(str(mode).strip().lower())
    except ValueError as exc:
        choices = ", ".join(item.value for item in LlmExecutionMode)
        raise LlmExecutionPolicyError(
            f"Unknown LLM execution mode {mode!r}; expected one of: {choices}"
        ) from exc


def _strict_mode(mode: LlmExecutionMode | str) -> bool:
    return coerce_execution_mode(mode) is LlmExecutionMode.STRICT_SINGLE_ATTEMPT


def _allow_retry(mode: LlmExecutionMode | str) -> bool:
    return not _strict_mode(mode)


def _allow_fallback(mode: LlmExecutionMode | str) -> bool:
    return not _strict_mode(mode)


def validate_strict_request(
    mode: LlmExecutionMode | str,
    *,
    provider: str | None,
    model: str | None,
) -> LlmExecutionMode:
    """Validate strict route inputs without reading config or credentials."""

    resolved_mode = coerce_execution_mode(mode)
    if resolved_mode is not LlmExecutionMode.STRICT_SINGLE_ATTEMPT:
        return resolved_mode
    provider_text = str(provider or "").strip()
    model_text = str(model or "").strip()
    if not provider_text or provider_text.lower() in {"auto", "main"}:
        raise StrictExecutionConfigurationError(
            "strict_single_attempt requires an explicit provider other than "
            "'auto' or 'main'"
        )
    if not model_text or model_text.lower() in {"auto", "main"}:
        raise StrictExecutionConfigurationError(
            "strict_single_attempt requires an explicit model other than "
            "'auto' or 'main'"
        )
    return resolved_mode


def routes_match(
    requested_provider: str | None,
    requested_model: str | None,
    dispatched_provider: str | None,
    dispatched_model: str | None,
) -> bool:
    """Compare providers case-insensitively and model identifiers literally."""

    return (
        str(requested_provider or "").strip().lower()
        == str(dispatched_provider or "").strip().lower()
        and str(requested_model or "").strip() == str(dispatched_model or "").strip()
    )


def require_matching_strict_route(
    audit: LlmExecutionAudit,
    *,
    provider: str | None,
    model: str | None,
) -> None:
    audit.record_dispatch(provider, model)
    if audit.route_changed:
        audit.strict_contract_satisfied = False
        raise StrictExecutionRouteMismatch(
            "strict_single_attempt route resolution changed "
            f"{audit.requested_provider!r}/{audit.requested_model!r} to "
            f"{audit.dispatched_provider!r}/{audit.dispatched_model!r}"
        )


def _delivery_may_be_ambiguous(exc: BaseException) -> bool:
    """Conservatively classify failures that may happen after server receipt."""

    name = type(exc).__name__.lower()
    message = str(exc).lower()
    markers = (
        "timeout",
        "timed out",
        "connection",
        "connecterror",
        "readerror",
        "writeerror",
        "remoteprotocol",
        "incomplete read",
        "stream closed",
    )
    return any(marker in name or marker in message for marker in markers)


__all__ = [
    "LlmExecutionAudit",
    "LlmExecutionMode",
    "LlmExecutionPolicyError",
    "StrictExecutionConfigurationError",
    "StrictExecutionRouteMismatch",
    "StrictExecutionUnsupported",
]
