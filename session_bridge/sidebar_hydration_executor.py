from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import math
import time
from typing import Literal, Protocol

from .coordinator import SidebarHydrationClaim
from .models import HydrationMarkerPayload, SidebarHydrationState
from .sidebar import (
    SidebarInitialPromptKind,
    classify_sidebar_initial_prompt,
    decode_hydration_marker,
    sidebar_bridge_id,
)
from .sidebar_executor import NativeTurnAmbiguous, _PROCESS_DELIVERY_LOCK
from .store import SessionBridgeStore


@dataclass(frozen=True)
class SidebarHydrationExecutionResult:
    status: Literal["idle", "visible", "retry", "failed", "unsettled"]
    job_id: str | None = None
    error_code: str | None = None


class NativeSidebarHydrationDelivery(Protocol):
    def read_thread_initial_prompt(
        self,
        *,
        thread_id: str,
        deadline: float,
    ) -> str: ...

    def thread_has_exact_marker(
        self,
        *,
        thread_id: str,
        marker: str,
        deadline: float,
    ) -> bool: ...

    def start_text_turn_and_verify_marker(
        self,
        *,
        thread_id: str,
        message: str,
        marker: str,
        deadline: float,
    ) -> None: ...


class SidebarHydrationExecutor:
    def __init__(
        self,
        *,
        claim_once: Callable[[], Sequence[SidebarHydrationClaim]],
        store: SessionBridgeStore,
        native: NativeSidebarHydrationDelivery,
        marker_secret: bytes,
        clock=time.time,
        monotonic=time.monotonic,
        operation_budget_seconds: float = 240.0,
    ) -> None:
        if not callable(claim_once):
            raise TypeError("hydration claim callback must be callable")
        if type(marker_secret) is not bytes or not marker_secret:
            raise ValueError("hydration marker secret is unavailable")
        if (
            isinstance(operation_budget_seconds, bool)
            or not isinstance(operation_budget_seconds, (int, float))
            or not math.isfinite(float(operation_budget_seconds))
            or not 0 < float(operation_budget_seconds) < 300
        ):
            raise ValueError(
                "hydration operation budget must be positive and shorter than lease"
            )
        self._claim_once = claim_once
        self._store = store
        self._native = native
        self._marker_secret = marker_secret
        self._clock = clock
        self._monotonic = monotonic
        self._operation_budget_seconds = float(operation_budget_seconds)

    def run_once(self) -> SidebarHydrationExecutionResult:
        with _PROCESS_DELIVERY_LOCK:
            return self._run_once_locked()

    def _run_once_locked(self) -> SidebarHydrationExecutionResult:
        deadline = _finite_time(self._monotonic()) + self._operation_budget_seconds
        try:
            claims = self._claim_once()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return SidebarHydrationExecutionResult(
                status="unsettled",
                error_code="bridge_temporarily_unavailable",
            )
        if not isinstance(claims, Sequence) or isinstance(
            claims,
            (str, bytes, bytearray),
        ):
            return SidebarHydrationExecutionResult(
                status="unsettled",
                error_code="source_identity_mismatch",
            )
        if not claims:
            return SidebarHydrationExecutionResult(status="idle")
        if len(claims) != 1 or not isinstance(claims[0], SidebarHydrationClaim):
            return SidebarHydrationExecutionResult(
                status="unsettled",
                error_code="source_identity_mismatch",
            )
        claim = claims[0]

        try:
            self._validate_claim(claim)
            prompt = self._native.read_thread_initial_prompt(
                thread_id=claim.codex_thread_id,
                deadline=deadline,
            )
            kind = classify_sidebar_initial_prompt(prompt, self._marker_secret)
            expected_source_line = (
                "Source session ID: "
                + json.dumps(
                    claim.source_session_id,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            if (
                kind is not SidebarInitialPromptKind.LEGACY_PLACEHOLDER
                or expected_source_line not in prompt.splitlines()
            ):
                return self._fail(claim, "source_identity_mismatch")
            if self._native.thread_has_exact_marker(
                thread_id=claim.codex_thread_id,
                marker=claim.hydration_marker,
                deadline=deadline,
            ):
                if not claim.send_reserved:
                    try:
                        self._store.reserve_sidebar_hydration_send(
                            lease_token=claim.lease_token,
                            now=_finite_time(self._clock()),
                        )
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except Exception:
                        return self._fail(
                            claim,
                            "bridge_temporarily_unavailable",
                        )
                return self._commit(claim)
            if claim.send_reserved:
                return self._fail(claim, "hydration_send_ambiguous")
        except (KeyboardInterrupt, SystemExit):
            raise
        except (TypeError, ValueError):
            return self._fail(claim, "source_identity_mismatch")
        except Exception:
            return self._fail(claim, "bridge_temporarily_unavailable")

        try:
            self._store.reserve_sidebar_hydration_send(
                lease_token=claim.lease_token,
                now=_finite_time(self._clock()),
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._fail(claim, "bridge_temporarily_unavailable")

        try:
            self._native.start_text_turn_and_verify_marker(
                thread_id=claim.codex_thread_id,
                message=claim.hydration_message,
                marker=claim.hydration_marker,
                deadline=deadline,
            )
            return self._commit(claim)
        except (KeyboardInterrupt, SystemExit):
            raise
        except NativeTurnAmbiguous:
            return self._fail(claim, "hydration_send_ambiguous")
        except Exception:
            return self._fail(claim, "hydration_send_ambiguous")

    def _validate_claim(self, claim: SidebarHydrationClaim) -> None:
        payload = decode_hydration_marker(
            claim.hydration_marker,
            self._marker_secret,
        )
        expected = HydrationMarkerPayload(
            bridge_id=claim.bridge_id,
            codex_thread_id=claim.codex_thread_id,
            preview_digest=claim.preview_digest,
            preview_version=claim.preview_version,
            source_cursor=claim.source_cursor,
            source_hash=claim.source_hash,
            source_session_id=claim.source_session_id,
        )
        if (
            payload != expected
            or sidebar_bridge_id(claim.source_session_id) != claim.bridge_id
            or claim.hydration_message.count(claim.hydration_marker) != 1
            or not claim.hydration_message.startswith("# ")
            or type(claim.send_reserved) is not bool
        ):
            raise ValueError("hydration claim identity mismatch")

    def _commit(
        self,
        claim: SidebarHydrationClaim,
    ) -> SidebarHydrationExecutionResult:
        try:
            result = self._store.commit_sidebar_hydration_job(
                lease_token=claim.lease_token,
                codex_thread_id=claim.codex_thread_id,
                hydration_marker=claim.hydration_marker,
                now=_finite_time(self._clock()),
            )
            if (
                not isinstance(result, Mapping)
                or result.get("state") != SidebarHydrationState.VISIBLE.value
            ):
                raise ValueError("hydration completion is malformed")
            return SidebarHydrationExecutionResult(status="visible")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._fail(claim, "hydration_send_ambiguous")

    def _fail(
        self,
        claim: SidebarHydrationClaim,
        error_code: str,
    ) -> SidebarHydrationExecutionResult:
        try:
            result = self._store.fail_sidebar_hydration_job(
                lease_token=claim.lease_token,
                error_code=error_code,
                codex_thread_id=claim.codex_thread_id,
                now=_finite_time(self._clock()),
            )
            state = (
                result.get("state")
                if isinstance(result, Mapping)
                else None
            )
            if state == SidebarHydrationState.RETRY.value:
                status: Literal["retry", "failed", "unsettled"] = "retry"
            elif state == SidebarHydrationState.FAILED.value:
                status = "failed"
            else:
                status = "unsettled"
            return SidebarHydrationExecutionResult(
                status=status,
                error_code=error_code,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return SidebarHydrationExecutionResult(
                status="unsettled",
                error_code="bridge_temporarily_unavailable",
            )


def _finite_time(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError("hydration executor clock is malformed")
    return float(value)


__all__ = [
    "NativeTurnAmbiguous",
    "NativeSidebarHydrationDelivery",
    "SidebarHydrationExecutionResult",
    "SidebarHydrationExecutor",
]
