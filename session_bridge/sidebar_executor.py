from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hmac
import math
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Literal, Mapping, Protocol, cast

from agent.transports.codex_app_server import CodexAppServerError

from .codex_adapter import SidebarThreadVerifier, SidebarVerificationError
from .models import BridgeMarkerPayload, OriginKind, Provider, encode_bridge_marker
from .preview import build_session_preview
from .sidebar import (
    SidebarCandidate,
    VerifiedSidebarThread,
    build_registration_prompt,
    decode_sidebar_registration_identity,
    sidebar_create_recovery_key,
    validate_sidebar_create_reservation,
)
from .sidebar_placement import (
    SidebarPlacement,
    SidebarPlacementError,
    filesystem_path_identity,
    placement_paths_equivalent,
)
from .sidebar_reconciliation import (
    SidebarReconciliationEvidence,
    SidebarReconciliationState,
)
from .store import (
    SIDEBAR_FATAL_ERRORS,
    SIDEBAR_RETRYABLE_ERRORS,
    SessionBridgeStore,
    SidebarNativeTaskNotIndexed,
)

_PROCESS_DELIVERY_LOCK = threading.Lock()
_SIDEBAR_EXECUTION_BLOCKER_ORDER = (
    "sidebar_failed",
    "sidebar_terminal_resolution_mismatch",
    "sidebar_terminal_resolution_ledger_invalid",
    "unknown_retry_code",
)
_SIGNED_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"HERMES_SESSION_BRIDGE_V1:[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"(?![A-Za-z0-9_-])"
)
_AUTHENTICATED_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"HERMES_SESSION_(?:BRIDGE|HYDRATION)_V1:"
    r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
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


class NativeTurnAmbiguous(RuntimeError):
    """A native text turn may have persisted after dispatch uncertainty."""

    def __init__(self) -> None:
        super().__init__("native_turn_ambiguous")


class NativeThreadUnrecoverable(RuntimeError):
    """A durable exact ID has no rollout and cannot be resumed."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = _required_text(thread_id, "Codex thread ID")
        super().__init__("native_create_ambiguous")


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

    def preflight(self, *, deadline: float) -> None: ...

    def create_thread(
        self,
        *,
        prompt: str,
        candidate: SidebarCandidate,
        placement: SidebarPlacement,
        recovery_key: str,
        deadline: float,
    ) -> str: ...

    def read_thread_state(
        self, *, thread_id: str, deadline: float
    ) -> NativeThreadState | None: ...

    def read_thread_initial_prompt(
        self, *, thread_id: str, deadline: float
    ) -> str: ...

    def register_thread(
        self,
        *,
        thread_id: str,
        prompt: str,
        deadline: float,
        fresh: bool = False,
    ) -> None: ...

    def rename_thread(self, *, thread_id: str, title: str, deadline: float) -> None: ...


class _CodexAppServerClient(Protocol):
    def initialize(self, *, timeout: float, **kwargs: object) -> dict[str, Any]: ...

    def request(
        self,
        method: str,
        params: dict[str, object] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]: ...

    def take_notification(self, timeout: float = 0.0) -> dict[str, Any] | None: ...

    def take_server_request(self, timeout: float = 0.0) -> dict[str, Any] | None: ...

    def respond_error(
        self,
        request_id: object,
        code: int,
        message: str,
        data: object | None = None,
    ) -> None: ...

    def close(self, timeout: float = 3.0) -> None: ...


class CodexAppServerSidebarDelivery:
    """Concrete native sidebar delivery over the Codex app-server protocol."""

    def __init__(
        self,
        client: _CodexAppServerClient,
        *,
        fresh_client_factory: Callable[[], _CodexAppServerClient] | None = None,
        monotonic=time.monotonic,
    ) -> None:
        self._client = client
        self._fresh_client_factory = fresh_client_factory
        self._monotonic = monotonic
        self._initialized = bool(getattr(client, "_initialized", False))

    def preflight(self, *, deadline: float) -> None:
        self._ensure_initialized(deadline)
        try:
            response = self._client.request(
                "thread/list",
                {"archived": False, "limit": 1},
                timeout=self._remaining(deadline),
            )
            if not isinstance(response, Mapping):
                raise ValueError("thread/list response is malformed")
            entries = response.get("data", response.get("threads"))
            if not isinstance(entries, list):
                raise ValueError("thread/list response has no entries list")
        except NativeCreateRejected:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise NativeCreateRejected("codex_tool_unavailable") from exc

    def create_thread(
        self,
        *,
        prompt: str,
        candidate: SidebarCandidate,
        placement: SidebarPlacement,
        recovery_key: str,
        deadline: float,
    ) -> str:
        del prompt
        expected_recovery_key = _required_recovery_key(recovery_key)
        try:
            placement = _validated_sidebar_placement(placement, candidate)
        except SidebarPlacementError as exc:
            raise NativeCreateRejected(exc.code) from exc
        self._ensure_initialized(deadline)
        # Expiry is the sole trusted pre-dispatch rejection. Once request()
        # is entered, even a nonconforming injected client that raises
        # NativeCreateRejected has crossed the ambiguity boundary.
        timeout = self._remaining(deadline)
        try:
            result = self._client.request(
                "thread/start",
                {
                    "cwd": placement.inbox_cwd,
                    "runtimeWorkspaceRoots": list(placement.runtime_workspace_roots),
                    "threadSource": expected_recovery_key,
                },
                timeout=timeout,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise NativeCreateAmbiguous() from exc
        try:
            thread = result.get("thread")
            if not isinstance(thread, Mapping):
                raise ValueError("thread/start response is malformed")
            thread_id = _required_text(thread.get("id"), "created Codex thread ID")
            returned_recovery_key = _required_recovery_key(thread.get("threadSource"))
            returned_cwd = _required_text(thread.get("cwd"), "created Codex cwd")
            if not hmac.compare_digest(
                returned_recovery_key, expected_recovery_key
            ) or not placement_paths_equivalent(returned_cwd, placement.inbox_cwd):
                raise ValueError("thread/start response identity mismatch")
        except (AttributeError, TypeError, ValueError) as exc:
            raise NativeCreateAmbiguous() from exc
        return thread_id

    def register_thread(
        self,
        *,
        thread_id: str,
        prompt: str,
        deadline: float,
        fresh: bool = False,
    ) -> None:
        wanted = _required_text(thread_id, "Codex thread ID")
        marker = _registration_marker(prompt)
        self._ensure_initialized(deadline)
        if not isinstance(fresh, bool):
            raise ValueError("fresh registration flag is malformed")
        if not fresh:
            thread = self._read_or_resume_thread(
                wanted,
                deadline=deadline,
                resume_not_loaded=True,
            )
            if _thread_has_exact_marker(thread, marker):
                return
        # A just-started thread exists only in this app-server process until
        # the first non-empty turn. Reading it through stored history first
        # reproduces the missing-rollout failure this executor must avoid.
        self._start_and_wait_for_registration(
            thread_id=wanted,
            prompt=prompt,
            deadline=deadline,
        )

    def _start_and_wait_for_registration(
        self,
        *,
        thread_id: str,
        prompt: str,
        deadline: float,
    ) -> None:
        wanted = _required_text(thread_id, "Codex thread ID")
        _registration_marker(prompt)
        try:
            response = self._client.request(
                "turn/start",
                {
                    "threadId": wanted,
                    "input": [{"type": "text", "text": prompt}],
                },
                timeout=self._remaining(deadline),
            )
            turn_id = _started_turn_id(response)
            self._wait_for_exact_turn_completion(
                thread_id=wanted,
                turn_id=turn_id,
                deadline=deadline,
            )
            # Codex emits turn/completed after an attempted persistence flush,
            # but local write errors are logged rather than surfaced. Treat the
            # event only as a wakeup and prove durability from a new process.
            self._verify_persisted_registration(
                thread_id=wanted,
                turn_id=turn_id,
                marker=_registration_marker(prompt),
                deadline=deadline,
            )
        except (NativeCreateAmbiguous, NativeThreadUnrecoverable):
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise NativeCreateAmbiguous() from exc

    def _wait_for_exact_turn_completion(
        self,
        *,
        thread_id: str,
        turn_id: str,
        deadline: float,
    ) -> None:
        wanted_thread = _required_text(thread_id, "Codex thread ID")
        wanted_turn = _required_text(turn_id, "Codex turn ID")
        while True:
            try:
                remaining = self._remaining(deadline)
                server_request = self._client.take_server_request(timeout=0.0)
                if server_request is not None:
                    request_id = (
                        server_request.get("id")
                        if isinstance(server_request, Mapping)
                        else None
                    )
                    if request_id is not None:
                        self._client.respond_error(
                            request_id,
                            -32600,
                            "session bridge registration forbids server requests",
                        )
                    raise NativeCreateAmbiguous()
                notification = self._client.take_notification(
                    timeout=min(0.25, remaining)
                )
            except NativeCreateAmbiguous:
                raise
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                raise NativeCreateAmbiguous() from exc
            if notification is None:
                continue
            if not isinstance(notification, Mapping):
                raise NativeCreateAmbiguous()
            if notification.get("method") != "turn/completed":
                continue
            params = notification.get("params")
            if not isinstance(params, Mapping):
                raise NativeCreateAmbiguous()
            observed_thread = _optional_text(params.get("threadId"))
            turn = params.get("turn")
            if not isinstance(turn, Mapping):
                raise NativeCreateAmbiguous()
            observed_turn = _optional_text(turn.get("id"))
            if observed_thread == wanted_thread and observed_turn == wanted_turn:
                return
            if observed_turn == wanted_turn:
                raise NativeCreateAmbiguous()

    def _verify_persisted_registration(
        self,
        *,
        thread_id: str,
        turn_id: str,
        marker: str,
        deadline: float,
    ) -> None:
        wanted_thread = _required_text(thread_id, "Codex thread ID")
        wanted_turn = _required_text(turn_id, "Codex turn ID")
        if self._fresh_client_factory is None:
            raise NativeCreateAmbiguous()
        client = self._fresh_client_factory()
        if client is self._client:
            raise NativeCreateAmbiguous()
        try:
            if not bool(getattr(client, "_initialized", False)):
                client.initialize(
                    timeout=self._remaining(deadline),
                    capabilities={"experimentalApi": True},
                )
            try:
                response = client.request(
                    "thread/resume",
                    {"threadId": wanted_thread},
                    timeout=self._remaining(deadline),
                )
            except CodexAppServerError as exc:
                if (
                    exc.code == -32600
                    and exc.message == f"no rollout found for thread id {wanted_thread}"
                ):
                    raise NativeThreadUnrecoverable(wanted_thread) from exc
                raise
            thread = _exact_thread(response, wanted_thread)
            if not _thread_has_exact_marker(
                thread,
                marker,
                turn_id=wanted_turn,
            ):
                raise NativeCreateAmbiguous()
        except (NativeCreateAmbiguous, NativeThreadUnrecoverable):
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise NativeCreateAmbiguous() from exc
        finally:
            try:
                client.close()
            except Exception:
                pass

    def read_thread_state(
        self,
        *,
        thread_id: str,
        deadline: float,
    ) -> NativeThreadState | None:
        wanted = _required_text(thread_id, "Codex thread ID")
        self._ensure_initialized(deadline)
        thread = self._read_or_resume_thread(wanted, deadline=deadline)
        cwd = _required_text(thread.get("cwd"), "Codex thread cwd")
        turns = cast(list[object], thread["turns"])
        status = _native_thread_status(thread.get("status"), turns=turns)
        return NativeThreadState(thread_id=wanted, status=status, cwd=cwd)

    def read_thread_initial_prompt(
        self,
        *,
        thread_id: str,
        deadline: float,
    ) -> str:
        wanted = _required_text(thread_id, "Codex thread ID")
        self._ensure_initialized(deadline)
        thread = self._read_or_resume_thread(
            wanted,
            deadline=deadline,
            resume_not_loaded=True,
        )
        return _exact_first_user_text(thread)

    def thread_has_exact_marker(
        self,
        *,
        thread_id: str,
        marker: str,
        deadline: float,
    ) -> bool:
        wanted = _required_text(thread_id, "Codex thread ID")
        exact_marker = _required_text(marker, "authenticated marker")
        self._ensure_initialized(deadline)
        thread = self._read_or_resume_thread(
            wanted,
            deadline=deadline,
            resume_not_loaded=True,
        )
        return _thread_has_exact_marker(
            thread,
            exact_marker,
            allow_interrupted=True,
        )

    def start_text_turn_and_verify_marker(
        self,
        *,
        thread_id: str,
        message: str,
        marker: str,
        deadline: float,
    ) -> None:
        wanted = _required_text(thread_id, "Codex thread ID")
        text = _required_text(message, "Codex turn message")
        exact_marker = _required_text(marker, "authenticated marker")
        if exact_marker not in _AUTHENTICATED_MARKER_RE.findall(text):
            raise ValueError("Codex turn marker is missing or embedded")
        self._ensure_initialized(deadline)
        try:
            resumed = self._client.request(
                "thread/resume",
                {"threadId": wanted},
                timeout=self._remaining(deadline),
            )
            _exact_thread(resumed, wanted)
            response = self._client.request(
                "turn/start",
                {
                    "threadId": wanted,
                    "input": [{"type": "text", "text": text}],
                },
                timeout=self._remaining(deadline),
            )
            turn_id = _started_turn_id(response)
            self._wait_for_exact_turn_completion(
                thread_id=wanted,
                turn_id=turn_id,
                deadline=deadline,
            )
            self._verify_persisted_registration(
                thread_id=wanted,
                turn_id=turn_id,
                marker=exact_marker,
                deadline=deadline,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise NativeTurnAmbiguous() from exc

    def _read_or_resume_thread(
        self,
        thread_id: str,
        *,
        deadline: float,
        resume_not_loaded: bool = False,
    ) -> Mapping[str, Any]:
        wanted = _required_text(thread_id, "Codex thread ID")
        try:
            response = self._client.request(
                "thread/read",
                {"threadId": wanted, "includeTurns": True},
                timeout=self._remaining(deadline),
            )
        except CodexAppServerError as exc:
            if exc.code != -32600 or exc.message != f"thread not loaded: {wanted}":
                raise
            try:
                response = self._client.request(
                    "thread/resume",
                    {"threadId": wanted},
                    timeout=self._remaining(deadline),
                )
            except CodexAppServerError as resume_exc:
                if (
                    resume_exc.code == -32600
                    and resume_exc.message == f"no rollout found for thread id {wanted}"
                ):
                    raise NativeThreadUnrecoverable(wanted) from resume_exc
                raise
        thread = _exact_thread(response, wanted)
        status = thread.get("status")
        if (
            resume_not_loaded
            and isinstance(status, Mapping)
            and status.get("type") == "notLoaded"
        ):
            try:
                response = self._client.request(
                    "thread/resume",
                    {"threadId": wanted},
                    timeout=self._remaining(deadline),
                )
            except CodexAppServerError as resume_exc:
                if (
                    resume_exc.code == -32600
                    and resume_exc.message == f"no rollout found for thread id {wanted}"
                ):
                    raise NativeThreadUnrecoverable(wanted) from resume_exc
                raise
            thread = _exact_thread(response, wanted)
        return thread

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
            self._client.initialize(
                timeout=self._remaining(deadline),
                capabilities={"experimentalApi": True},
            )
        except NativeCreateRejected:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except RuntimeError as exc:
            if str(exc) != "already initialized":
                raise NativeCreateRejected("codex_tool_unavailable") from exc
        except Exception as exc:
            raise NativeCreateRejected("codex_tool_unavailable") from exc
        try:
            setattr(self._client, "_session_bridge_experimental_api", True)
        except (AttributeError, TypeError):
            pass
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
        placement_resolver: Callable[[SidebarCandidate], SidebarPlacement],
        marker_secret: bytes,
        clock=time.time,
        monotonic=time.monotonic,
        sleep=time.sleep,
        read_timeout_seconds: float = 60.0,
        poll_interval: float = 0.25,
        operation_budget_seconds: float = 240.0,
        readable_preview_enabled: bool = False,
        preview_budget_chars: int = 24_000,
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
        if type(readable_preview_enabled) is not bool:
            raise ValueError("sidebar executor readable preview flag must be boolean")
        if (
            type(preview_budget_chars) is not int
            or not 1 <= preview_budget_chars <= 100_000
        ):
            raise ValueError(
                "sidebar executor preview budget must be between 1 and 100000"
            )
        self._store = store
        self._verifier = verifier
        self._native = native
        self._placement_resolver = placement_resolver
        self._marker_secret = marker_secret
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._read_timeout_seconds = float(read_timeout_seconds)
        self._poll_interval = float(poll_interval)
        self._operation_budget_seconds = float(operation_budget_seconds)
        self._readable_preview_enabled = readable_preview_enabled
        self._preview_budget_chars = preview_budget_chars

    def run_once(self) -> SidebarExecutionResult:
        with _PROCESS_DELIVERY_LOCK:
            try:
                worker_lock = self._store.try_acquire_sidebar_worker_lock()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                return SidebarExecutionResult(
                    status="unsettled",
                    error_code="bridge_temporarily_unavailable",
                )
            if worker_lock is None:
                return SidebarExecutionResult(
                    status="unsettled",
                    error_code="bridge_temporarily_unavailable",
                )
            try:
                return self._run_once_locked()
            finally:
                worker_lock.release()

    def _run_once_locked(self) -> SidebarExecutionResult:
        operation_deadline = (
            _finite_time(self._monotonic()) + self._operation_budget_seconds
        )
        try:
            self._native.preflight(deadline=operation_deadline)
        except (KeyboardInterrupt, SystemExit):
            raise
        except NativeCreateRejected as exc:
            return SidebarExecutionResult(status="unsettled", error_code=exc.code)
        except Exception:
            return SidebarExecutionResult(
                status="unsettled",
                error_code="bridge_temporarily_unavailable",
            )
        claim_time = _finite_time(self._clock())
        gate = self._store_gate(now=claim_time)
        if gate is not None:
            return gate
        claims = self._store.claim_sidebar_jobs(
            now=claim_time,
            limit=1,
            lease_seconds=300,
        )
        recoverable_tokens, malformed_claims = _direct_claim_tokens(claims, limit=1)
        if malformed_claims:
            for lease_token in dict.fromkeys(recoverable_tokens):
                self._settle(
                    job_id=None,
                    lease_token=lease_token,
                    error_code="broker_time_budget",
                )
            return SidebarExecutionResult(
                status="unsettled",
                error_code="source_identity_mismatch",
            )
        assert isinstance(claims, list)
        if not claims:
            gate = self._store_gate(now=claim_time)
            if gate is not None:
                return gate
            try:
                self._store.record_sidebar_broker_heartbeat(now=claim_time)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                return SidebarExecutionResult(
                    status="unsettled",
                    error_code="bridge_temporarily_unavailable",
                )
            return SidebarExecutionResult(status="idle")
        assert len(claims) == 1 and isinstance(claims[0], Mapping)

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
            self._store.record_sidebar_broker_heartbeat(now=claim_time)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                error_code="bridge_temporarily_unavailable",
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
            thread_was_created_here = False
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

        try:
            placement = self._placement_resolver(candidate)
        except (KeyboardInterrupt, SystemExit):
            raise
        except SidebarPlacementError as exc:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                error_code=exc.code,
            )
        except Exception:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                error_code="bridge_temporarily_unavailable",
            )
        try:
            placement = _validated_sidebar_placement(placement, candidate)
        except SidebarPlacementError as exc:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                error_code=exc.code,
            )

        expected = BridgeMarkerPayload(
            bridge_id=bridge_id,
            source_session_id=source_session_id,
            target_provider=Provider.CODEX,
            policy_generation=1,
        )
        marker = encode_bridge_marker(expected, self._marker_secret)
        preview = None
        if self._readable_preview_enabled:
            try:
                snapshot = self._store.get_sidebar_preview_source(
                    source_session_id
                )
                if (
                    not isinstance(snapshot, Mapping)
                    or snapshot.get("source_session_id") != source_session_id
                    or snapshot.get("provider") != candidate.provider.value
                ):
                    raise ValueError("sidebar source preview identity is malformed")
                preview = build_session_preview(
                    source_session_id=source_session_id,
                    source_cursor=_required_text(
                        snapshot.get("source_cursor"),
                        "preview source cursor",
                    ),
                    source_hash=_required_text(
                        snapshot.get("source_hash"),
                        "preview source hash",
                    ),
                    title=cast(str | None, snapshot.get("title")),
                    provider=candidate.provider.value,
                    cwd=candidate.cwd,
                    captured_at=cast(float, snapshot.get("captured_at")),
                    messages=cast(
                        list[Mapping[str, Any]],
                        snapshot.get("messages"),
                    ),
                    git_root=candidate.git_root,
                    git_branch=candidate.git_branch,
                    git_head=candidate.git_head,
                    worktree_id=candidate.worktree_id,
                    budget_chars=self._preview_budget_chars,
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
        prompt = build_registration_prompt(candidate, marker, preview=preview)
        expected_recovery_key = sidebar_create_recovery_key(
            marker,
            self._marker_secret,
        )
        recovered: VerifiedSidebarThread | None = None
        if thread_id is None:
            if not self._has_budget(operation_deadline, lease_expires_at):
                return self._settle(
                    job_id=job_id,
                    lease_token=lease_token,
                    error_code="broker_time_budget",
                )
            try:
                proof_time = _finite_time(self._clock())
                evidence = self._verifier.reconcile_marker(
                    expected,
                    now=proof_time,
                    ttl_seconds=30.0,
                )
                if not isinstance(evidence, SidebarReconciliationEvidence):
                    raise TypeError("sidebar reconciliation evidence is malformed")
                proof = self._store.record_sidebar_reconciliation_proof(
                    lease_token=lease_token,
                    evidence=evidence,
                    marker_digest=evidence.marker_digest,
                    placement_generation=placement.placement_generation,
                    delivery_generation=1,
                    now=proof_time,
                )
                if not isinstance(proof, Mapping):
                    raise TypeError("sidebar reconciliation proof is malformed")
                reconciliation_proof_digest = _required_text(
                    proof.get("proof_digest"),
                    "sidebar reconciliation proof digest",
                )
                reconciliation_generation = _required_text(
                    proof.get("reconciliation_generation"),
                    "sidebar reconciliation generation",
                )
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
            if evidence.state is SidebarReconciliationState.BLOCKED:
                return self._settle(
                    job_id=job_id,
                    lease_token=lease_token,
                    error_code=_verification_code(
                        SidebarVerificationError(
                            evidence.fixed_reason
                            or "bridge_temporarily_unavailable"
                        )
                    ),
                )
            recovered = (
                VerifiedSidebarThread(
                    thread_id=_required_text(
                        evidence.recovered_thread_id,
                        "recovered Codex thread ID",
                    ),
                    source_session_id=source_session_id,
                    bridge_id=bridge_id,
                )
                if evidence.state is SidebarReconciliationState.RECOVERED
                else None
            )
            if recovered is not None:
                if not isinstance(
                    recovered, VerifiedSidebarThread
                ) or not _matches_expected(recovered, expected):
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
                    reservation = self._store.get_sidebar_create_reservation(
                        source_session_id
                    )
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as exc:
                    return self._settle(
                        job_id=job_id,
                        lease_token=lease_token,
                        error_code=_store_error_code(exc),
                    )
                if reservation is not None:
                    try:
                        recovery_key = validate_sidebar_create_reservation(
                            reservation,
                            job_id=job_id,
                            source_session_id=source_session_id,
                            bridge_id=bridge_id,
                            expected_recovery_key=expected_recovery_key,
                        )
                    except ValueError:
                        return self._settle(
                            job_id=job_id,
                            lease_token=lease_token,
                            error_code="native_create_ambiguous",
                        )
                    thread_id, recovery_error = self._recover_reserved_thread(
                        recovery_key,
                        expected_cwd=placement.inbox_cwd,
                        operation_deadline=operation_deadline,
                    )
                    if recovery_error is not None:
                        return self._settle(
                            job_id=job_id,
                            lease_token=lease_token,
                            error_code=recovery_error,
                        )
                    if thread_id is None:
                        return self._settle(
                            job_id=job_id,
                            lease_token=lease_token,
                            error_code="native_create_ambiguous",
                        )
                else:
                    try:
                        reservation = self._store.reserve_sidebar_create(
                            lease_token=lease_token,
                            recovery_key=expected_recovery_key,
                            reconciliation_proof_digest=(
                                reconciliation_proof_digest
                            ),
                            reconciliation_generation=reconciliation_generation,
                            now=_finite_time(self._clock()),
                        )
                        recovery_key = validate_sidebar_create_reservation(
                            reservation,
                            job_id=job_id,
                            source_session_id=source_session_id,
                            bridge_id=bridge_id,
                            expected_recovery_key=expected_recovery_key,
                        )
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except Exception as exc:
                        return self._settle(
                            job_id=job_id,
                            lease_token=lease_token,
                            error_code=_store_error_code(exc),
                        )
                    try:
                        raw_created_thread_id = self._native.create_thread(
                            prompt=prompt,
                            candidate=candidate,
                            placement=placement,
                            recovery_key=recovery_key,
                            deadline=operation_deadline,
                        )
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except NativeCreateRejected as exc:
                        try:
                            self._store.clear_sidebar_create_reservation(
                                lease_token=lease_token,
                                recovery_key=recovery_key,
                                now=_finite_time(self._clock()),
                            )
                        except (KeyboardInterrupt, SystemExit):
                            raise
                        except Exception:
                            return self._settle(
                                job_id=job_id,
                                lease_token=lease_token,
                                error_code="native_create_ambiguous",
                            )
                        return self._settle(
                            job_id=job_id,
                            lease_token=lease_token,
                            error_code=exc.code,
                        )
                    except NativeCreateAmbiguous:
                        thread_id, recovery_error = self._recover_reserved_thread(
                            recovery_key,
                            expected_cwd=placement.inbox_cwd,
                            operation_deadline=operation_deadline,
                        )
                        if recovery_error is not None:
                            return self._settle(
                                job_id=job_id,
                                lease_token=lease_token,
                                error_code=recovery_error,
                            )
                        if thread_id is None:
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
                    else:
                        try:
                            thread_id = _required_text(
                                raw_created_thread_id,
                                "created Codex thread ID",
                            )
                            thread_was_created_here = True
                        except ValueError:
                            return self._settle(
                                job_id=job_id,
                                lease_token=lease_token,
                                error_code="native_create_ambiguous",
                            )

        if thread_id is None:
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
        if not thread_was_created_here:
            prebound_error = self._validate_existing_thread_placement(
                thread_id,
                expected_cwd=placement.inbox_cwd,
                operation_deadline=operation_deadline,
            )
            if prebound_error is not None:
                return self._settle(
                    job_id=job_id,
                    lease_token=lease_token,
                    thread_id=thread_id,
                    error_code=prebound_error,
                )
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
                fresh=thread_was_created_here,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except NativeThreadUnrecoverable:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="native_create_ambiguous",
            )
        except Exception:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="native_task_not_indexed",
            )

        read_error = self._wait_until_idle(
            thread_id,
            expected_cwd=placement.inbox_cwd,
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

        if verified.projection is not None and not placement_paths_equivalent(
            verified.projection.cwd,
            placement.inbox_cwd,
        ):
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="placement_mismatch",
            )

        try:
            initial_prompt = self._native.read_thread_initial_prompt(
                thread_id=thread_id,
                deadline=operation_deadline,
            )
            registration_identity = decode_sidebar_registration_identity(
                initial_prompt,
                self._marker_secret,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except NativeCreateRejected as exc:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code=exc.code,
            )
        except ValueError:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="source_identity_mismatch",
            )
        except Exception:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code="native_task_not_indexed",
            )
        if (
            registration_identity.source_session_id != candidate.source_session_id
            or registration_identity.bridge_id != candidate.bridge_id
            or not placement_paths_equivalent(
                registration_identity.source_cwd,
                candidate.cwd,
            )
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

        if verified.projection is not None:
            try:
                self._store.upsert_projection(verified.projection)
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
        commit_error = self._commit_when_indexed(
            lease_token=lease_token,
            thread_id=thread_id,
            source_session_id=source_session_id,
            bridge_id=bridge_id,
            placement_generation=placement.placement_generation,
            operation_deadline=operation_deadline,
            lease_expires_at=lease_expires_at,
        )
        if commit_error is not None:
            return self._settle(
                job_id=job_id,
                lease_token=lease_token,
                thread_id=thread_id,
                error_code=commit_error,
            )
        return SidebarExecutionResult(
            status="visible",
            job_id=job_id,
            thread_id=thread_id,
        )

    def _commit_when_indexed(
        self,
        *,
        lease_token: str,
        thread_id: str,
        source_session_id: str,
        bridge_id: str,
        placement_generation: int,
        operation_deadline: float,
        lease_expires_at: float,
    ) -> str | None:
        index_deadline = min(
            operation_deadline,
            _finite_time(self._monotonic()) + self._read_timeout_seconds,
        )
        while True:
            if not self._has_budget(operation_deadline, lease_expires_at):
                return "broker_time_budget"
            try:
                committed = self._store.commit_sidebar_job_with_lineage(
                    lease_token=lease_token,
                    codex_thread_id=thread_id,
                    source_session_id=source_session_id,
                    bridge_id=bridge_id,
                    placement_generation=placement_generation,
                    now=_finite_time(self._clock()),
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except SidebarNativeTaskNotIndexed:
                pass
            except Exception as exc:
                return _store_error_code(exc)
            else:
                if (
                    not isinstance(committed, Mapping)
                    or committed.get("state") != "sidebar_visible"
                    or committed.get("codex_thread_id") != thread_id
                ):
                    return "bridge_temporarily_unavailable"
                return None
            now = _finite_time(self._monotonic())
            if now >= index_deadline:
                return "native_task_not_indexed"
            self._sleep(min(self._poll_interval, index_deadline - now))

    def _store_gate(self, *, now: float) -> SidebarExecutionResult | None:
        try:
            blockers = self._store.sidebar_execution_blockers()
            active_lease = self._store.sidebar_has_active_lease(now=now)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return SidebarExecutionResult(
                status="unsettled",
                error_code="bridge_temporarily_unavailable",
            )
        if (
            not isinstance(blockers, tuple)
            or blockers
            != tuple(
                code for code in _SIDEBAR_EXECUTION_BLOCKER_ORDER if code in blockers
            )
            or not isinstance(active_lease, bool)
        ):
            return SidebarExecutionResult(
                status="unsettled",
                error_code="bridge_temporarily_unavailable",
            )
        if blockers:
            return SidebarExecutionResult(
                status="unsettled",
                error_code="source_identity_mismatch",
            )
        if active_lease:
            return SidebarExecutionResult(
                status="unsettled",
                error_code="bridge_temporarily_unavailable",
            )
        return None

    def _recover_reserved_thread(
        self,
        recovery_key: str,
        *,
        expected_cwd: str,
        operation_deadline: float,
    ) -> tuple[str | None, str | None]:
        try:
            raw_thread_id = self._verifier.find_by_recovery_key(
                recovery_key,
                expected_cwd=expected_cwd,
                deadline=operation_deadline,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except SidebarVerificationError as exc:
            return None, _verification_code(exc)
        except Exception:
            return None, "bridge_temporarily_unavailable"
        if raw_thread_id is None:
            return None, None
        try:
            return _required_text(raw_thread_id, "recovered Codex thread ID"), None
        except ValueError:
            return None, "codex_thread_conflict"

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
                if state.thread_id != thread_id:
                    return "codex_thread_conflict"
                if not placement_paths_equivalent(state.cwd, expected_cwd):
                    return "placement_mismatch"
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

    def _validate_existing_thread_placement(
        self,
        thread_id: str,
        *,
        expected_cwd: str,
        operation_deadline: float,
    ) -> str | None:
        try:
            state = self._native.read_thread_state(
                thread_id=thread_id,
                deadline=operation_deadline,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except NativeCreateRejected as exc:
            return exc.code
        except Exception:
            return "native_task_not_indexed"
        if not isinstance(state, NativeThreadState):
            return "native_task_not_indexed"
        if state.thread_id != thread_id:
            return "codex_thread_conflict"
        if not placement_paths_equivalent(state.cwd, expected_cwd):
            return "placement_mismatch"
        if not isinstance(state.status, NativeThreadStatus):
            return "native_task_not_indexed"
        return None

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
                codex_thread_id=thread_id,
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
    projection = verified.projection
    return (
        verified.source_session_id == expected.source_session_id
        and verified.bridge_id == expected.bridge_id
        and (
            projection is None
            or (
                projection.provider is Provider.CODEX
                and projection.native_id == verified.thread_id
                and projection.origin_kind is OriginKind.BRIDGE_PLACEHOLDER
                and projection.origin_bridge_id == expected.bridge_id
            )
        )
    )


def _direct_claim_tokens(claims: object, *, limit: int) -> tuple[list[str], bool]:
    """Extract every concrete lease token before rejecting a malformed batch."""

    if not isinstance(claims, (list, tuple)):
        return [], True
    malformed = not isinstance(claims, list) or len(claims) > limit
    tokens: list[str] = []
    for claim in claims:
        if not isinstance(claim, Mapping):
            malformed = True
            continue
        claim_map = cast(Mapping[str, object], claim)
        try:
            tokens.append(_required_text(claim_map.get("lease_token"), "lease token"))
        except ValueError:
            malformed = True
    if len(set(tokens)) != len(tokens):
        malformed = True
    return tokens, malformed


def _registration_marker(prompt: object) -> str:
    if type(prompt) is not str:
        raise ValueError("registration prompt is malformed")
    matches = _SIGNED_MARKER_RE.findall(prompt)
    if len(matches) != 1:
        raise ValueError("registration prompt marker is malformed")
    return matches[0]


def _started_turn_id(response: object) -> str:
    if not isinstance(response, dict):
        raise ValueError("turn/start response is malformed")
    response_map = cast(dict[str, object], response)
    turn = response_map.get("turn")
    if not isinstance(turn, dict):
        raise ValueError("turn/start response has no turn")
    turn_map = cast(dict[str, object], turn)
    return _required_text(turn_map.get("id"), "started Codex turn ID")


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


def _thread_has_exact_marker(
    thread: Mapping[str, object],
    marker: str,
    *,
    turn_id: str | None = None,
    allow_interrupted: bool = False,
) -> bool:
    turns = thread.get("turns")
    assert isinstance(turns, list)
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn = cast(dict[str, object], turn)
        accepted_statuses = (
            {"completed", "interrupted"}
            if allow_interrupted
            else {"completed"}
        )
        if turn.get("status") not in accepted_statuses:
            continue
        if turn_id is not None and turn.get("id") != turn_id:
            continue
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
            if marker in _AUTHENTICATED_MARKER_RE.findall(text):
                return True
    return False


def _exact_first_user_text(thread: Mapping[str, object]) -> str:
    turns = thread.get("turns")
    if not isinstance(turns, list):
        raise ValueError("Codex thread has no turns list")
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "userMessage":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                raise ValueError("Codex initial user prompt is malformed")
            parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if part.get("type") == "text" and isinstance(text, str):
                    parts.append(text)
            return _required_text("".join(parts), "Codex initial user prompt")
    raise ValueError("Codex initial user prompt is missing")


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
        if "sidebar create reservation" in message:
            return "native_create_ambiguous"
        if "sidebar lease has expired" in message:
            return "broker_time_budget"
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


def _required_recovery_key(value: object) -> str:
    recovery_key = _required_text(value, "sidebar recovery key")
    prefix = "hermes-session-bridge-create-v1:"
    digest = recovery_key.removeprefix(prefix)
    if digest == recovery_key or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("sidebar recovery key is malformed")
    return recovery_key


def _validated_sidebar_placement(
    placement: object,
    candidate: SidebarCandidate,
) -> SidebarPlacement:
    if not isinstance(placement, SidebarPlacement):
        raise SidebarPlacementError("inbox_unavailable")
    try:
        inbox_cwd = _required_text(placement.inbox_cwd, "sidebar inbox cwd")
        if filesystem_path_identity(inbox_cwd) is None:
            raise SidebarPlacementError("inbox_unavailable")
        if (
            placement.local_host != "local"
            or type(placement.placement_generation) is not int
            or placement.placement_generation != 1
        ):
            raise SidebarPlacementError("inbox_unavailable")
        roots = placement.runtime_workspace_roots
        if type(roots) is not tuple or len(roots) not in {1, 2}:
            raise SidebarPlacementError("inbox_unavailable")
        if any(
            filesystem_path_identity(
                _required_text(root, "sidebar runtime workspace root")
            ) is None
            for root in roots
        ):
            raise SidebarPlacementError("inbox_unavailable")
        if not placement_paths_equivalent(roots[0], inbox_cwd):
            raise SidebarPlacementError("inbox_unavailable")
        if len(roots) == 1:
            if not placement_paths_equivalent(inbox_cwd, candidate.cwd):
                raise SidebarPlacementError("inbox_unavailable")
        elif (
            placement_paths_equivalent(roots[0], roots[1])
            or not placement_paths_equivalent(roots[1], candidate.cwd)
        ):
            raise SidebarPlacementError("inbox_unavailable")
    except (AttributeError, TypeError, ValueError) as exc:
        raise SidebarPlacementError("inbox_unavailable") from exc
    return placement


def _finite_time(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError("sidebar executor clock is malformed")
    return float(value)


__all__ = [
    "CodexAppServerSidebarDelivery",
    "NativeCreateAmbiguous",
    "NativeCreateRejected",
    "NativeTurnAmbiguous",
    "NativeSidebarDelivery",
    "NativeThreadState",
    "NativeThreadStatus",
    "SidebarExecutionResult",
    "SidebarExecutor",
]
