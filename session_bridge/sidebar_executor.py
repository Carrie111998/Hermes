from __future__ import annotations

from dataclasses import dataclass
import math
import os
import threading
import time
from typing import Literal, Mapping, Protocol

from .codex_adapter import SidebarThreadVerifier, SidebarVerificationError
from .models import BridgeMarkerPayload, Provider, encode_bridge_marker
from .sidebar import (
    SidebarCandidate,
    VerifiedSidebarThread,
    build_registration_prompt,
)
from .store import SIDEBAR_FATAL_ERRORS, SIDEBAR_RETRYABLE_ERRORS, SessionBridgeStore


_PROCESS_DELIVERY_LOCK = threading.Lock()


class NativeCreateAmbiguous(RuntimeError):
    """The native create call may have succeeded but returned no usable identity."""

    def __init__(self) -> None:
        super().__init__("native_create_ambiguous")


@dataclass(frozen=True)
class NativeThreadState:
    thread_id: str
    status: str
    cwd: str


class NativeSidebarDelivery(Protocol):
    """Narrow native task boundary used by the deterministic executor."""

    def create_thread(self, *, prompt: str, candidate: SidebarCandidate) -> str: ...

    def read_thread_state(self, *, thread_id: str) -> NativeThreadState | None: ...

    def rename_thread(self, *, thread_id: str, title: str) -> None: ...


@dataclass(frozen=True)
class SidebarExecutionResult:
    status: Literal["idle", "visible", "retry", "failed"]
    job_id: str | None = None
    thread_id: str | None = None
    error_code: str | None = None


class SidebarExecutor:
    """Lease and deterministically deliver at most one native sidebar task."""

    def __init__(
        self,
        *,
        store: SessionBridgeStore,
        verifier: SidebarThreadVerifier,
        native: NativeSidebarDelivery,
        marker_secret: bytes,
        clock=time.time,
        monotonic=time.monotonic,
        sleep=time.sleep,
        read_timeout_seconds: float = 60.0,
        poll_interval: float = 0.25,
    ) -> None:
        if not isinstance(marker_secret, bytes) or not marker_secret:
            raise ValueError("sidebar executor marker secret is unavailable")
        for label, value in (
            ("read timeout", read_timeout_seconds),
            ("poll interval", poll_interval),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise ValueError(f"sidebar executor {label} must be positive")
        self._store = store
        self._verifier = verifier
        self._native = native
        self._marker_secret = marker_secret
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._read_timeout_seconds = float(read_timeout_seconds)
        self._poll_interval = float(poll_interval)

    def run_once(self) -> SidebarExecutionResult:
        with _PROCESS_DELIVERY_LOCK:
            return self._run_once_locked()

    def _run_once_locked(self) -> SidebarExecutionResult:
        claim_time = _finite_time(self._clock())
        claims = self._store.claim_sidebar_jobs(
            now=claim_time,
            limit=1,
            lease_seconds=300,
        )
        if not claims:
            return SidebarExecutionResult(status="idle")
        if len(claims) != 1 or not isinstance(claims[0], Mapping):
            raise ValueError("sidebar executor claim is malformed")

        claim = claims[0]
        job_id = _required_text(claim.get("id"), "sidebar job ID")
        lease_token = _required_text(claim.get("lease_token"), "sidebar lease token")
        source_session_id = _required_text(
            claim.get("source_session_id"), "source session ID"
        )
        bridge_id = _required_text(claim.get("bridge_id"), "bridge ID")
        candidate = self._store.get_sidebar_candidate_for_delivery(source_session_id)
        if (
            candidate.source_session_id != source_session_id
            or candidate.bridge_id != bridge_id
        ):
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                error_code="source_identity_mismatch",
            )

        expected = BridgeMarkerPayload(
            bridge_id=bridge_id,
            source_session_id=source_session_id,
            target_provider=Provider.CODEX,
            policy_generation=1,
        )
        raw_thread_id = claim.get("codex_thread_id")
        thread_id = (
            None
            if raw_thread_id is None
            else _required_text(raw_thread_id, "Codex thread ID")
        )
        if thread_id is None:
            try:
                recovered = self._verifier.find_by_marker(expected)
            except (KeyboardInterrupt, SystemExit):
                raise
            except SidebarVerificationError as exc:
                return self._settle(
                    job_id=job_id,
                    lease_token=lease_token,
                    error_code=_verification_code(exc),
                )
            except Exception:
                return self._settle(
                    job_id=job_id,
                    lease_token=lease_token,
                    error_code="bridge_temporarily_unavailable",
                )
            if recovered is not None:
                if not _matches_expected(recovered, expected):
                    return self._settle(
                        job_id=job_id,
                        lease_token=lease_token,
                        error_code="source_identity_mismatch",
                    )
                thread_id = recovered.thread_id
            else:
                marker = encode_bridge_marker(expected, self._marker_secret)
                prompt = build_registration_prompt(candidate, marker)
                try:
                    raw_created_thread_id = self._native.create_thread(
                        prompt=prompt,
                        candidate=candidate,
                    )
                except (KeyboardInterrupt, SystemExit):
                    raise
                except NativeCreateAmbiguous:
                    return self._settle(
                        job_id=job_id,
                        lease_token=lease_token,
                        error_code="native_create_ambiguous",
                    )
                except Exception:
                    return self._settle(
                        job_id=job_id,
                        lease_token=lease_token,
                        error_code="native_task_not_indexed",
                    )
                try:
                    thread_id = _required_text(
                        raw_created_thread_id,
                        "created Codex thread ID",
                    )
                except ValueError:
                    return self._settle(
                        job_id=job_id,
                        lease_token=lease_token,
                        error_code="native_create_ambiguous",
                    )

        try:
            self._store.bind_sidebar_thread(
                lease_token=lease_token,
                codex_thread_id=thread_id,
                now=_finite_time(self._clock()),
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="bridge_temporarily_unavailable",
            )

        read_error = self._wait_until_idle(thread_id, expected_cwd=candidate.cwd)
        if read_error is not None:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code=read_error,
            )

        try:
            verified = self._verifier.verify_thread(
                thread_id=thread_id,
                expected=expected,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except SidebarVerificationError as exc:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code=_verification_code(exc),
            )
        except Exception:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="bridge_temporarily_unavailable",
            )
        if not _matches_expected(verified, expected) or verified.thread_id != thread_id:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="source_identity_mismatch",
            )

        try:
            self._native.rename_thread(thread_id=thread_id, title=candidate.title)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="rename_failed",
            )

        try:
            committed = self._store.commit_sidebar_job_with_lineage(
                lease_token=lease_token,
                codex_thread_id=thread_id,
                source_session_id=source_session_id,
                bridge_id=bridge_id,
                now=_finite_time(self._clock()),
            )
            if (
                not isinstance(committed, Mapping)
                or committed.get("state") != "sidebar_visible"
                or committed.get("codex_thread_id") != thread_id
            ):
                raise ValueError("sidebar executor commit is malformed")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="bridge_temporarily_unavailable",
            )
        return SidebarExecutionResult(
            status="visible",
            job_id=job_id,
            thread_id=thread_id,
        )

    def _wait_until_idle(self, thread_id: str, *, expected_cwd: str) -> str | None:
        deadline = _finite_time(self._monotonic()) + self._read_timeout_seconds
        while True:
            try:
                state = self._native.read_thread_state(thread_id=thread_id)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                return "native_task_not_indexed"
            if state is not None:
                if not isinstance(state, NativeThreadState):
                    return "native_task_not_indexed"
                if (
                    state.thread_id != thread_id
                    or not _filesystem_equivalent(state.cwd, expected_cwd)
                ):
                    return "codex_thread_conflict"
                if state.status == "idle":
                    return None
            now = _finite_time(self._monotonic())
            if now >= deadline:
                return "native_task_not_indexed"
            self._sleep(min(self._poll_interval, deadline - now))

    def _settle(
        self,
        *,
        job_id: str,
        lease_token: str,
        error_code: str,
        thread_id: str | None = None,
    ) -> SidebarExecutionResult:
        if error_code not in SIDEBAR_RETRYABLE_ERRORS | SIDEBAR_FATAL_ERRORS:
            error_code = "bridge_temporarily_unavailable"
        try:
            self._store.fail_sidebar_job(
                lease_token=lease_token,
                error_code=error_code,
                now=_finite_time(self._clock()),
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            pass
        return SidebarExecutionResult(
            status="failed" if error_code in SIDEBAR_FATAL_ERRORS else "retry",
            job_id=job_id,
            thread_id=thread_id,
            error_code=error_code,
        )


def _matches_expected(
    verified: VerifiedSidebarThread, expected: BridgeMarkerPayload
) -> bool:
    return (
        verified.source_session_id == expected.source_session_id
        and verified.bridge_id == expected.bridge_id
    )


def _verification_code(exc: SidebarVerificationError) -> str:
    if exc.code in SIDEBAR_RETRYABLE_ERRORS | SIDEBAR_FATAL_ERRORS:
        return exc.code
    return "bridge_temporarily_unavailable"


def _required_text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} is malformed")
    return value


def _finite_time(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError("sidebar executor clock is malformed")
    return float(value)


def _filesystem_equivalent(left: object, right: object) -> bool:
    if type(left) is not str or type(right) is not str or not left or not right:
        return False
    try:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(right)
        )
    except (OSError, ValueError):
        return False


__all__ = [
    "NativeCreateAmbiguous",
    "NativeSidebarDelivery",
    "NativeThreadState",
    "SidebarExecutionResult",
    "SidebarExecutor",
]
