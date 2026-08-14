"""Core-owned provenance for one physical provider execution.

This module deliberately exposes only private issuance/binding helpers.  The
conversation loop is the producer of authoritative values; plugins and tools
must not be able to create a record by passing provenance fields as ordinary
kwargs.  The frozen records are an in-process integrity aid, not a
cross-process authentication boundary.  A deployment that hands these values
to an untrusted process still needs an authenticated envelope owned by Hermes.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
import uuid
from typing import Any


_RUNTIME_INSTANCE_ID = f"hermes-runtime-{uuid.uuid4().hex}"
_CORE_AUTHORITY = object()


@dataclass(frozen=True, slots=True, init=False)
class ProviderAttemptProvenance:
    """Immutable identity and route snapshot for one provider attempt."""

    runtime_instance_id: str
    session_id: str
    task_id: str
    turn_id: str
    api_request_id: str
    provider_attempt_id: str
    attempt_index: int
    retry_count: int
    provider: str
    request_model: str
    response_model: str | None
    fallback_used: bool
    fallback_generation: int
    fallback_reason: str | None
    outcome: str
    started_at: float
    ended_at: float | None

    def __init__(
        self,
        *,
        _authority: object | None = None,
        runtime_instance_id: str,
        session_id: str,
        task_id: str,
        turn_id: str,
        api_request_id: str,
        provider_attempt_id: str,
        attempt_index: int,
        retry_count: int,
        provider: str,
        request_model: str,
        response_model: str | None,
        fallback_used: bool,
        fallback_generation: int,
        fallback_reason: str | None,
        outcome: str,
        started_at: float,
        ended_at: float | None,
    ) -> None:
        if _authority is not _CORE_AUTHORITY:
            raise TypeError("provider-attempt records are Hermes-core issued")
        if not isinstance(fallback_used, bool):
            raise TypeError("fallback_used must be producer-owned bool")
        if not isinstance(attempt_index, int) or attempt_index < 0:
            raise ValueError("attempt_index must be a non-negative integer")
        if not isinstance(retry_count, int) or retry_count < 0:
            raise ValueError("retry_count must be a non-negative integer")
        if not isinstance(fallback_generation, int) or fallback_generation < 0:
            raise ValueError("fallback_generation must be a non-negative integer")
        for name, value in (
            ("runtime_instance_id", runtime_instance_id),
            ("session_id", session_id),
            ("task_id", task_id),
            ("turn_id", turn_id),
            ("api_request_id", api_request_id),
            ("provider_attempt_id", provider_attempt_id),
            ("provider", provider),
            ("request_model", request_model),
            ("outcome", outcome),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        for name, value in (
            ("response_model", response_model),
            ("fallback_reason", fallback_reason),
        ):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} must be a string or None")
        for name, value in (("started_at", started_at), ("ended_at", ended_at)):
            if value is not None and not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be numeric or None")
        for name, value in (
            ("runtime_instance_id", runtime_instance_id),
            ("session_id", session_id),
            ("task_id", task_id),
            ("turn_id", turn_id),
            ("api_request_id", api_request_id),
            ("provider_attempt_id", provider_attempt_id),
            ("provider", provider),
            ("request_model", request_model),
            ("response_model", response_model),
            ("fallback_used", fallback_used),
            ("fallback_generation", fallback_generation),
            ("fallback_reason", fallback_reason),
            ("outcome", outcome),
            ("started_at", float(started_at)),
            ("ended_at", float(ended_at) if ended_at is not None else None),
        ):
            object.__setattr__(self, name, value)
        object.__setattr__(self, "attempt_index", attempt_index)
        object.__setattr__(self, "retry_count", retry_count)

    def complete(
        self,
        *,
        response_model: str | None,
        outcome: str,
        ended_at: float | None = None,
    ) -> "ProviderAttemptProvenance":
        """Return a completed immutable snapshot without mutating this attempt."""
        if not isinstance(outcome, str) or not outcome:
            raise ValueError("outcome must be a non-empty string")
        if response_model is not None and not isinstance(response_model, str):
            raise TypeError("response_model must be a string or None")
        if ended_at is None:
            ended_at = time.time()
        return ProviderAttemptProvenance(
            _authority=_CORE_AUTHORITY,
            runtime_instance_id=self.runtime_instance_id,
            session_id=self.session_id,
            task_id=self.task_id,
            turn_id=self.turn_id,
            api_request_id=self.api_request_id,
            provider_attempt_id=self.provider_attempt_id,
            attempt_index=self.attempt_index,
            retry_count=self.retry_count,
            provider=self.provider,
            request_model=self.request_model,
            response_model=response_model,
            fallback_used=self.fallback_used,
            fallback_generation=self.fallback_generation,
            fallback_reason=self.fallback_reason,
            outcome=outcome,
            started_at=self.started_at,
            ended_at=float(ended_at),
        )

    def bind_tool_call(self, tool_call_id: str) -> "ToolCallProvenance":
        """Bind one model-emitted tool call to this exact response attempt."""
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise ValueError("tool_call_id must be a non-empty string")
        return ToolCallProvenance._issue(
            authority=_CORE_AUTHORITY,
            attempt=self,
            tool_call_id=tool_call_id,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a detached observer projection, never the authority object."""
        return {
            "runtime_instance_id": self.runtime_instance_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "api_request_id": self.api_request_id,
            "provider_attempt_id": self.provider_attempt_id,
            "attempt_index": self.attempt_index,
            "retry_count": self.retry_count,
            "provider": self.provider,
            "request_model": self.request_model,
            "response_model": self.response_model,
            "fallback_used": self.fallback_used,
            "fallback_generation": self.fallback_generation,
            "fallback_reason": self.fallback_reason,
            "outcome": self.outcome,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


@dataclass(frozen=True, slots=True, init=False)
class ToolCallProvenance:
    """Immutable response-attempt plus tool-call binding."""

    attempt: ProviderAttemptProvenance
    tool_call_id: str

    @classmethod
    def _issue(
        cls,
        *,
        authority: object,
        attempt: ProviderAttemptProvenance,
        tool_call_id: str,
    ) -> "ToolCallProvenance":
        return cls(
            _authority=authority,
            attempt=attempt,
            tool_call_id=tool_call_id,
        )

    def __init__(
        self,
        *,
        _authority: object | None = None,
        attempt: ProviderAttemptProvenance,
        tool_call_id: str,
    ) -> None:
        if _authority is not _CORE_AUTHORITY:
            raise TypeError("tool-call provenance is Hermes-core issued")
        if not isinstance(attempt, ProviderAttemptProvenance):
            raise TypeError("attempt must be Hermes provider-attempt provenance")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise ValueError("tool_call_id must be a non-empty string")
        object.__setattr__(self, "attempt", attempt)
        object.__setattr__(self, "tool_call_id", tool_call_id)

    @property
    def provider_attempt_id(self) -> str:
        return self.attempt.provider_attempt_id

    @property
    def provider(self) -> str:
        return self.attempt.provider

    @property
    def request_model(self) -> str:
        return self.attempt.request_model

    @property
    def response_model(self) -> str | None:
        return self.attempt.response_model

    @property
    def fallback_used(self) -> bool:
        return self.attempt.fallback_used

    def as_dict(self) -> dict[str, Any]:
        result = self.attempt.as_dict()
        result["tool_call_id"] = self.tool_call_id
        return result


def _issue_retry_provider_attempt(
    issue_provider_attempt,
    *,
    retry_index: int,
):
    """Issue a fresh core attempt before an internal physical retry.

    The outer conversation loop issues the first attempt.  A lower transport
    retry must either obtain a new producer-issued record here or fail closed;
    silently reusing the outer record would collapse two physical requests into
    one identity.
    """
    if retry_index <= 0:
        return None
    if not callable(issue_provider_attempt):
        raise RuntimeError("physical provider retry occurred without an attempt issuer")
    return issue_provider_attempt()


def _begin_provider_attempt(
    *,
    session_id: str,
    task_id: str,
    turn_id: str,
    api_request_id: str,
    attempt_index: int,
    retry_count: int,
    provider: str,
    request_model: str,
    fallback_used: bool,
    fallback_generation: int,
    fallback_reason: str | None,
    started_at: float | None = None,
) -> ProviderAttemptProvenance:
    """Issue one attempt record; called only by Hermes' provider loop."""
    return ProviderAttemptProvenance(
        _authority=_CORE_AUTHORITY,
        runtime_instance_id=_RUNTIME_INSTANCE_ID,
        session_id=session_id,
        task_id=task_id,
        turn_id=turn_id,
        api_request_id=api_request_id,
        provider_attempt_id=f"{_RUNTIME_INSTANCE_ID}:attempt:{uuid.uuid4().hex}",
        attempt_index=attempt_index,
        retry_count=retry_count,
        provider=provider,
        request_model=request_model,
        response_model=None,
        fallback_used=fallback_used,
        fallback_generation=fallback_generation,
        fallback_reason=fallback_reason,
        outcome="started",
        started_at=time.time() if started_at is None else float(started_at),
        ended_at=None,
    )
