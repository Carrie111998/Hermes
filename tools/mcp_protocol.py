from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal


class ProtocolPolicy(str, Enum):
    AUTO = "auto"
    LEGACY = "legacy"
    MODERN = "modern"


class StaleConnectionGenerationError(RuntimeError):
    def __init__(self, expected: int, current: int) -> None:
        super().__init__(
            f"MCP connection generation {expected} is stale; current generation is {current}"
        )
        self.expected = expected
        self.current = current


class LegacyProofError(RuntimeError):
    def __init__(self, discovery_error: BaseException, proof_error: BaseException) -> None:
        super().__init__(
            "Modern server/discover received the canonical candidate legacy rejection "
            f"({discovery_error}); the one permitted legacy initialize proof failed "
            f"({proof_error})"
        )
        self.discovery_error = discovery_error
        self.proof_error = proof_error


@dataclass
class ProtocolNegotiationState:
    generation: int
    policy: ProtocolPolicy
    negotiated_era: Literal["modern", "legacy"] | None = None
    negotiated_protocol_version: str | None = None
    legacy_proof_attempted: bool = False
    fallback_reason: str | None = None


@dataclass(frozen=True)
class ProtocolNegotiationOutcome:
    result: Any
    era: Literal["modern", "legacy"]
    protocol_version: str | None
    fallback_reason: str | None


_OMITTED = object()


def normalize_protocol_policy(raw: object = _OMITTED) -> ProtocolPolicy:
    if raw is _OMITTED:
        return ProtocolPolicy.AUTO
    if not isinstance(raw, str):
        raise TypeError(
            f"MCP protocol must be a string when configured, got {type(raw).__name__}"
        )
    value = raw.strip().lower()
    if value == "auto":
        return ProtocolPolicy.AUTO
    if value == "legacy":
        return ProtocolPolicy.LEGACY
    if value in {"stateless", "2026-07-28"}:
        return ProtocolPolicy.MODERN
    raise ValueError(
        f"Unknown MCP protocol {raw!r}; expected auto, legacy, stateless, or 2026-07-28"
    )


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for child in exc.exceptions:
            leaves.extend(_leaf_exceptions(child))
        return leaves
    return [exc]


def is_candidate_legacy_discovery_rejection(
    exc: BaseException,
    *,
    state: ProtocolNegotiationState,
    request_method: str,
) -> bool:
    if state.policy is not ProtocolPolicy.AUTO:
        return False
    if state.negotiated_era is not None or state.legacy_proof_attempted:
        return False
    if request_method != "server/discover":
        return False
    leaves = _leaf_exceptions(exc)
    if len(leaves) != 1:
        return False
    error = getattr(leaves[0], "error", None)
    return bool(
        error is not None
        and getattr(error, "code", None) == -32602
        and getattr(error, "message", None) == "Invalid request parameters"
        and getattr(error, "data", None) == ""
    )


def _protocol_version(session: Any, result: Any, era: str) -> str | None:
    version = getattr(session, "protocol_version", None)
    if isinstance(version, str) and version:
        return version
    if era == "legacy":
        version = getattr(result, "protocol_version", None)
        if isinstance(version, str) and version:
            return version
    return "2026-07-28" if era == "modern" else None


async def negotiate_protocol(
    session: Any,
    state: ProtocolNegotiationState,
    *,
    timeout: float,
    assert_generation: Callable[[int], None] | None = None,
) -> ProtocolNegotiationOutcome:
    def assert_current() -> None:
        if assert_generation is not None:
            assert_generation(state.generation)

    async def bounded(call) -> Any:
        assert_current()
        result = await asyncio.wait_for(call(), timeout=timeout)
        assert_current()
        return result

    if state.negotiated_era is not None:
        raise RuntimeError(
            f"MCP generation {state.generation} already negotiated {state.negotiated_era}"
        )

    if state.policy is ProtocolPolicy.LEGACY:
        result = await bounded(session.initialize)
        state.negotiated_era = "legacy"
        state.negotiated_protocol_version = _protocol_version(session, result, "legacy")
        return ProtocolNegotiationOutcome(
            result,
            "legacy",
            state.negotiated_protocol_version,
            None,
        )

    try:
        result = await bounded(session.discover)
    except asyncio.CancelledError:
        raise
    except Exception as discovery_error:
        assert_current()
        if not is_candidate_legacy_discovery_rejection(
            discovery_error,
            state=state,
            request_method="server/discover",
        ):
            raise
        state.legacy_proof_attempted = True
        state.fallback_reason = "canonical-legacy-discover-rejection"
        try:
            result = await bounded(session.initialize)
        except asyncio.CancelledError:
            raise
        except Exception as proof_error:
            assert_current()
            raise LegacyProofError(discovery_error, proof_error) from proof_error
        state.negotiated_era = "legacy"
        state.negotiated_protocol_version = _protocol_version(session, result, "legacy")
        return ProtocolNegotiationOutcome(
            result,
            "legacy",
            state.negotiated_protocol_version,
            state.fallback_reason,
        )

    state.negotiated_era = "modern"
    state.negotiated_protocol_version = _protocol_version(session, result, "modern")
    return ProtocolNegotiationOutcome(
        result,
        "modern",
        state.negotiated_protocol_version,
        None,
    )
