from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
import os
import re
import sqlite3
import threading
import time
from typing import Any, Literal, Mapping, Protocol, cast

from .codex_adapter import SidebarThreadVerifier, SidebarVerificationError
from .models import BridgeMarkerPayload, Provider, encode_bridge_marker
from .sidebar import (
    SidebarCandidate,
    VerifiedSidebarThread,
    build_registration_prompt,
)
from .store import SIDEBAR_FATAL_ERRORS, SIDEBAR_RETRYABLE_ERRORS, SessionBridgeStore


_PROCESS_DELIVERY_LOCK = threading.Lock()
_SIGNED_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"HERMES_SESSION_BRIDGE_V1:[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"(?![A-Za-z0-9_-])"
)


class NativeCreateAmbiguous(RuntimeError):
    """The native create call may have succeeded but returned no usable identity."""

    def __init__(self) -> None:
        super().__init__("native_create_ambiguous")


class NativeCreateRejected(RuntimeError):
    """A definite local pre-dispatch rejection with no possible native creation."""

    def __init__(self, code: str) -> None:
        if code not in SIDEBAR_RETRYABLE_ERRORS:
            raise ValueError("native create rejection code must be retryable")
        self.code = code
        super().__init__(code)


class NativeThreadStatus(StrEnum):
    ACTIVE = "active"
    IDLE = "idle"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class NativeThreadState:
    thread_id: str
    status: NativeThreadStatus
    cwd: str


class NativeSidebarDelivery(Protocol):
    """Narrow native task boundary used by the deterministic executor."""

    def create_thread(
        self,
        *,
        prompt: str,
        candidate: SidebarCandidate,
        deadline: float,
    ) -> str: ...

    def read_thread_state(
        self, *, thread_id: str, deadline: float
    ) -> NativeThreadState | None: ...

    def register_thread(
        self, *, thread_id: str, prompt: str, deadline: float
    ) -> None: ...

    def rename_thread(
        self, *, thread_id: str, title: str, deadline: float
    ) -> None: ...


class _CodexAppServerClient(Protocol):
    def initialize(self, *, timeout: float, **kwargs: object) -> dict[str, Any]: ...

    def request(
        self,
        method: str,
        params: dict[str, object] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]: ...


class CodexAppServerSidebarDelivery:
    """Concrete native sidebar delivery over the Codex app-server protocol."""

    def __init__(
        self,
        client: _CodexAppServerClient,
        *,
        monotonic=time.monotonic,
    ) -> None:
        self._client = client
        self._monotonic = monotonic
        self._initialized = bool(getattr(client, "_initialized", False))

    def create_thread(
        self,
        *,
        prompt: str,
        candidate: SidebarCandidate,
        deadline: float,
    ) -> str:
        del prompt
        self._ensure_initialized(deadline)
        try:
            result = self._client.request(
                "thread/start",
                {"cwd": candidate.cwd},
                timeout=self._remaining(deadline),
            )
        except NativeCreateRejected:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise NativeCreateAmbiguous() from exc
        try:
            thread = result.get("thread")
            if not isinstance(thread, Mapping):
                raise ValueError("thread/start response is malformed")
            return _required_text(thread.get("id"), "created Codex thread ID")
        except (AttributeError, TypeError, ValueError) as exc:
            raise NativeCreateAmbiguous() from exc

    def register_thread(
        self,
        *,
        thread_id: str,
        prompt: str,
        deadline: float,
    ) -> None:
        wanted = _required_text(thread_id, "Codex thread ID")
        marker = _registration_marker(prompt)
        self._ensure_initialized(deadline)
        response = self._client.request(
            "thread/read",
            {"threadId": wanted, "includeTurns": True},
            timeout=self._remaining(deadline),
        )
        thread = _exact_thread(response, wanted)
        if _thread_has_exact_marker(thread, marker):
            return
        self._client.request(
            "turn/start",
            {
                "threadId": wanted,
                "input": [{"type": "text", "text": prompt}],
            },
            timeout=self._remaining(deadline),
        )

    def read_thread_state(
        self,
        *,
        thread_id: str,
        deadline: float,
    ) -> NativeThreadState | None:
        wanted = _required_text(thread_id, "Codex thread ID")
        self._ensure_initialized(deadline)
        response = self._client.request(
            "thread/read",
            {"threadId": wanted, "includeTurns": True},
            timeout=self._remaining(deadline),
        )
        thread = _exact_thread(response, wanted)
        cwd = _required_text(thread.get("cwd"), "Codex thread cwd")
        turns = cast(list[object], thread["turns"])
        status = _native_thread_status(thread.get("status"), turns=turns)
        return NativeThreadState(thread_id=wanted, status=status, cwd=cwd)

    def rename_thread(
        self,
        *,
        thread_id: str,
        title: str,
        deadline: float,
    ) -> None:
        wanted = _required_text(thread_id, "Codex thread ID")
        name = _required_text(title, "Codex thread title")
        self._ensure_initialized(deadline)
        self._client.request(
            "thread/name/set",
            {"threadId": wanted, "name": name},
            timeout=self._remaining(deadline),
        )

    def _ensure_initialized(self, deadline: float) -> None:
        if self._initialized or bool(getattr(self._client, "_initialized", False)):
            self._initialized = True
            return
        try:
            self._client.initialize(timeout=self._remaining(deadline))
        except NativeCreateRejected:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except RuntimeError as exc:
            if str(exc) != "already initialized":
                raise NativeCreateRejected("codex_tool_unavailable") from exc
        except Exception as exc:
            raise NativeCreateRejected("codex_tool_unavailable") from exc
        self._initialized = True

    def _remaining(self, deadline: float) -> float:
        remaining = _finite_time(deadline) - _finite_time(self._monotonic())
        if remaining <= 0:
            raise NativeCreateRejected("broker_time_budget")
        return remaining


@dataclass(frozen=True)
class SidebarExecutionResult:
    status: Literal["idle", "visible", "retry", "failed", "unsettled"]
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
        operation_budget_seconds: float = 240.0,
    ) -> None:
        if not isinstance(marker_secret, bytes) or not marker_secret:
            raise ValueError("sidebar executor marker secret is unavailable")
        for label, value in (
            ("read timeout", read_timeout_seconds),
            ("poll interval", poll_interval),
            ("operation budget", operation_budget_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise ValueError(f"sidebar executor {label} must be positive")
        if float(operation_budget_seconds) >= 300.0:
            raise ValueError(
                "sidebar executor operation budget must be shorter than the lease"
            )
        self._store = store
        self._verifier = verifier
        self._native = native
        self._marker_secret = marker_secret
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._read_timeout_seconds = float(read_timeout_seconds)
        self._poll_interval = float(poll_interval)
        self._operation_budget_seconds = float(operation_budget_seconds)

    def run_once(self) -> SidebarExecutionResult:
        with _PROCESS_DELIVERY_LOCK:
            return self._run_once_locked()

    def _run_once_locked(self) -> SidebarExecutionResult:
        operation_deadline = (
            _finite_time(self._monotonic()) + self._operation_budget_seconds
        )
        claim_time = _finite_time(self._clock())
        claims = self._store.claim_sidebar_jobs(
            now=claim_time,
            limit=1,
            lease_seconds=300,
        )
        if not claims:
            return SidebarExecutionResult(status="idle")
        if len(claims) != 1 or not isinstance(claims[0], Mapping):
            return SidebarExecutionResult(
                status="unsettled",
                error_code="source_identity_mismatch",
            )

        claim = claims[0]
        job_id = _optional_text(claim.get("id"))
        try:
            lease_token = _required_text(
                claim.get("lease_token"), "sidebar lease token"
            )
        except ValueError:
            return SidebarExecutionResult(
                status="unsettled",
                job_id=job_id,
                error_code="source_identity_mismatch",
            )
        try:
            if job_id is None:
                raise ValueError("sidebar job ID is malformed")
            source_session_id = _required_text(
                claim.get("source_session_id"), "source session ID"
            )
            bridge_id = _required_text(claim.get("bridge_id"), "bridge ID")
            lease_expires_at = _finite_time(claim.get("lease_expires_at"))
            if lease_expires_at <= claim_time:
                raise ValueError("sidebar lease expiry is malformed")
            raw_thread_id = claim.get("codex_thread_id")
            thread_id = (
                None
                if raw_thread_id is None
                else _required_text(raw_thread_id, "Codex thread ID")
            )
            thread_was_prebound = thread_id is not None
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                error_code="source_identity_mismatch",
            )

        try:
            candidate = self._store.get_sidebar_candidate_for_delivery(
                source_session_id
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except (KeyError, TypeError, ValueError):
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                error_code="source_identity_mismatch",
            )
        except Exception:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                error_code="bridge_temporarily_unavailable",
            )
        if (
            not isinstance(candidate, SidebarCandidate)
            or candidate.source_session_id != source_session_id
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
        marker = encode_bridge_marker(expected, self._marker_secret)
        prompt = build_registration_prompt(candidate, marker)
        recovered: VerifiedSidebarThread | None = None
        if thread_id is None:
            if not self._has_budget(operation_deadline, lease_expires_at):
                return self._settle(
                    job_id=job_id,
                    lease_token=lease_token,
                    error_code="broker_time_budget",
                )
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
                if (
                    not isinstance(recovered, VerifiedSidebarThread)
                    or not _matches_expected(recovered, expected)
                ):
                    return self._settle(
                        job_id=job_id,
                        lease_token=lease_token,
                        error_code="source_identity_mismatch",
                    )
                try:
                    thread_id = _required_text(
                        recovered.thread_id,
                        "recovered Codex thread ID",
                    )
                except ValueError:
                    return self._settle(
                        job_id=job_id,
                        lease_token=lease_token,
                        error_code="source_identity_mismatch",
                    )
            else:
                if not self._has_budget(operation_deadline, lease_expires_at):
                    return self._settle(
                        job_id=job_id,
                        lease_token=lease_token,
                        error_code="broker_time_budget",
                    )
                try:
                    raw_created_thread_id = self._native.create_thread(
                        prompt=prompt,
                        candidate=candidate,
                        deadline=operation_deadline,
                    )
                except (KeyboardInterrupt, SystemExit):
                    raise
                except NativeCreateRejected as exc:
                    return self._settle(
                        job_id=job_id,
                        lease_token=lease_token,
                        error_code=exc.code,
                    )
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
                        error_code="native_create_ambiguous",
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

        if not self._has_budget(operation_deadline, lease_expires_at):
            # A successful create must still be bound below before any retry.  A
            # recovered marker identity also needs durable binding.  Only an ID
            # already present in the claim can safely yield immediately.
            if thread_was_prebound:
                return self._settle(
                    job_id=job_id,
                    lease_token=lease_token,
                    thread_id=thread_id,
                    error_code="broker_time_budget",
                )
        assert thread_id is not None
        try:
            self._store.bind_sidebar_thread(
                lease_token=lease_token,
                codex_thread_id=thread_id,
                now=_finite_time(self._clock()),
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code=_store_error_code(exc),
            )

        if not self._has_budget(operation_deadline, lease_expires_at):
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="broker_time_budget",
            )

        try:
            self._native.register_thread(
                thread_id=thread_id,
                prompt=prompt,
                deadline=operation_deadline,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="native_task_not_indexed",
            )

        read_error = self._wait_until_idle(
            thread_id,
            expected_cwd=candidate.cwd,
            operation_deadline=operation_deadline,
            lease_expires_at=lease_expires_at,
        )
        if read_error is not None:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code=read_error,
            )

        if not self._has_budget(operation_deadline, lease_expires_at):
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="broker_time_budget",
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
        if (
            not isinstance(verified, VerifiedSidebarThread)
            or not _matches_expected(verified, expected)
            or verified.thread_id != thread_id
        ):
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="source_identity_mismatch",
            )

        if not self._has_budget(operation_deadline, lease_expires_at):
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="broker_time_budget",
            )
        try:
            self._native.rename_thread(
                thread_id=thread_id,
                title=candidate.title,
                deadline=operation_deadline,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="rename_failed",
            )

        if not self._has_budget(operation_deadline, lease_expires_at):
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="broker_time_budget",
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
        except Exception as exc:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code=_store_error_code(exc),
            )
        return SidebarExecutionResult(
            status="visible",
            job_id=job_id,
            thread_id=thread_id,
        )

    def _has_budget(
        self,
        operation_deadline: float,
        lease_expires_at: float,
    ) -> bool:
        return (
            _finite_time(self._monotonic()) < operation_deadline
            and _finite_time(self._clock()) < lease_expires_at
        )

    def _wait_until_idle(
        self,
        thread_id: str,
        *,
        expected_cwd: str,
        operation_deadline: float,
        lease_expires_at: float,
    ) -> str | None:
        read_deadline = min(
            operation_deadline,
            _finite_time(self._monotonic()) + self._read_timeout_seconds,
        )
        while True:
            if not self._has_budget(operation_deadline, lease_expires_at):
                return "broker_time_budget"
            try:
                state = self._native.read_thread_state(
                    thread_id=thread_id,
                    deadline=operation_deadline,
                )
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
                if not isinstance(state.status, NativeThreadStatus):
                    return "native_task_not_indexed"
                if state.status is NativeThreadStatus.IDLE:
                    return None
                if state.status is NativeThreadStatus.TERMINAL:
                    return "native_task_not_indexed"
            now = _finite_time(self._monotonic())
            if now >= operation_deadline:
                return "broker_time_budget"
            if now >= read_deadline:
                return "native_task_not_indexed"
            self._sleep(min(self._poll_interval, read_deadline - now))

    def _settle(
        self,
        *,
        job_id: str | None,
        lease_token: str,
        error_code: str,
        thread_id: str | None = None,
    ) -> SidebarExecutionResult:
        if error_code not in SIDEBAR_RETRYABLE_ERRORS | SIDEBAR_FATAL_ERRORS:
            error_code = "bridge_temporarily_unavailable"
        try:
            settled = self._store.fail_sidebar_job(
                lease_token=lease_token,
                error_code=error_code,
                now=_finite_time(self._clock()),
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            settled = None
        state = settled.get("state") if isinstance(settled, Mapping) else None
        if state == "sidebar_failed":
            status: Literal["retry", "failed", "unsettled"] = "failed"
        elif state in {"sidebar_pending", "sidebar_retry"}:
            status = "retry"
        else:
            status = "unsettled"
        return SidebarExecutionResult(
            status=status,
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


def _registration_marker(prompt: object) -> str:
    if type(prompt) is not str:
        raise ValueError("registration prompt is malformed")
    matches = _SIGNED_MARKER_RE.findall(prompt)
    if len(matches) != 1:
        raise ValueError("registration prompt marker is malformed")
    return matches[0]


def _exact_thread(response: object, thread_id: str) -> dict[str, object]:
    if not isinstance(response, dict):
        raise ValueError("thread/read response is malformed")
    response_map = cast(dict[str, object], response)
    thread = response_map.get("thread")
    if not isinstance(thread, dict):
        raise ValueError("thread/read returned a different thread identity")
    thread = cast(dict[str, object], thread)
    if thread.get("id") != thread_id:
        raise ValueError("thread/read returned a different thread identity")
    turns = thread.get("turns")
    if not isinstance(turns, list):
        raise ValueError("thread/read response has no turns list")
    return thread


def _thread_has_exact_marker(thread: Mapping[str, object], marker: str) -> bool:
    turns = thread.get("turns")
    assert isinstance(turns, list)
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn = cast(dict[str, object], turn)
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item = cast(dict[str, object], item)
            if item.get("type") != "userMessage":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            text_parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                part = cast(dict[str, object], part)
                part_text = part.get("text")
                if part.get("type") == "text" and isinstance(part_text, str):
                    text_parts.append(part_text)
            text = "".join(text_parts)
            if marker in _SIGNED_MARKER_RE.findall(text):
                return True
    return False


def _native_thread_status(
    value: object,
    *,
    turns: list[object],
) -> NativeThreadStatus:
    if value is None:
        return NativeThreadStatus.IDLE if not turns else NativeThreadStatus.TERMINAL
    if not isinstance(value, dict):
        raise ValueError("Codex thread status is malformed")
    value = cast(dict[str, object], value)
    status_type = value.get("type")
    if status_type in {"idle", "notLoaded"}:
        return NativeThreadStatus.IDLE
    if status_type == "active":
        return NativeThreadStatus.ACTIVE
    if status_type == "systemError":
        return NativeThreadStatus.TERMINAL
    raise ValueError("Codex thread status is unknown")


def _verification_code(exc: SidebarVerificationError) -> str:
    if exc.code in SIDEBAR_RETRYABLE_ERRORS | SIDEBAR_FATAL_ERRORS:
        return exc.code
    return "bridge_temporarily_unavailable"


def _store_error_code(exc: Exception) -> str:
    if isinstance(exc, sqlite3.OperationalError):
        message = str(exc).casefold()
        if "locked" in message or "busy" in message:
            return "sqlite_busy"
    if isinstance(exc, ValueError):
        message = str(exc).casefold()
        if "source_identity_mismatch" in message:
            return "source_identity_mismatch"
        if (
            "conflicting codex thread identity" in message
            or "conflicting sidebar completion replay" in message
        ):
            return "codex_thread_conflict"
    return "bridge_temporarily_unavailable"


def _optional_text(value: object) -> str | None:
    try:
        return _required_text(value, "optional text")
    except ValueError:
        return None


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
    "CodexAppServerSidebarDelivery",
    "NativeCreateAmbiguous",
    "NativeCreateRejected",
    "NativeSidebarDelivery",
    "NativeThreadState",
    "NativeThreadStatus",
    "SidebarExecutionResult",
    "SidebarExecutor",
]
