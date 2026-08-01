from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import math
import os
import sqlite3
import threading
from typing import Any, Callable, cast

import pytest

from agent.transports.codex_app_server import CodexAppServerError
import session_bridge.sidebar_executor as sidebar_executor_module
from session_bridge.models import (
    BridgeMarkerPayload,
    OriginKind,
    Provider,
    SessionProjection,
    encode_bridge_marker,
)
from session_bridge.codex_adapter import SidebarThreadVerifier
from session_bridge.sidebar import (
    SidebarCandidate,
    VerifiedSidebarThread,
    build_registration_prompt,
    sidebar_bridge_id,
)
from session_bridge.sidebar_placement import (
    SidebarPlacement,
    SidebarPlacementError,
    resolve_sidebar_placement,
)
from session_bridge.sidebar_reconciliation import (
    SidebarReconciliationEvidence,
    SidebarReconciliationState,
)
from session_bridge.sidebar_executor import (
    CodexAppServerSidebarDelivery,
    NativeCreateAmbiguous,
    NativeCreateRejected,
    NativeTurnAmbiguous,
    NativeThreadState,
    NativeThreadStatus,
    NativeThreadUnrecoverable,
    SidebarExecutionResult,
    SidebarExecutor,
)
from session_bridge.store import (
    SIDEBAR_FATAL_ERRORS,
    SessionBridgeStore,
    SidebarNativeTaskNotIndexed,
)


SOURCE_1 = "claude:source-1"
SOURCE_2 = "claude:source-2"
THREAD_1 = "11111111-1111-4111-8111-111111111111"
THREAD_2 = "22222222-2222-4222-8222-222222222222"
TURN_1 = "33333333-3333-4333-8333-333333333333"
SECRET = b"sidebar-executor-test-secret"
RECOVERY_KEY = "hermes-session-bridge-create-v1:" + "a" * 64
INBOX_CWD = "C:/Users/diego/.hermes" if os.name == "nt" else "/srv/session-inbox"
SOURCE_CWD = "C:/source" if os.name == "nt" else "/srv/session-source"


def _placement() -> SidebarPlacement:
    return SidebarPlacement(
        inbox_cwd=INBOX_CWD,
        local_host="local",
        runtime_workspace_roots=(INBOX_CWD, SOURCE_CWD),
        placement_generation=1,
    )


def test_concrete_codex_app_server_delivery_is_available() -> None:
    assert hasattr(sidebar_executor_module, "CodexAppServerSidebarDelivery")
    assert "CodexAppServerSidebarDelivery" in sidebar_executor_module.__all__


class FakeCodexAppServerClient:
    def __init__(
        self,
        responses: dict[str, list[object]],
        *,
        initialize_error: Exception | None = None,
        notifications: list[object] | None = None,
        server_requests: list[object] | None = None,
    ) -> None:
        self.responses = {method: list(values) for method, values in responses.items()}
        self.calls: list[tuple[str, dict[str, object], float]] = []
        self.initialize_timeouts: list[float] = []
        self.initialize_error = initialize_error
        self.notifications = list(notifications or [])
        self.server_requests = list(server_requests or [])
        self.notification_timeouts: list[float] = []
        self.server_request_timeouts: list[float] = []
        self.response_errors: list[tuple[object, int, str]] = []
        self.close_calls = 0
        self._initialized = False

    def initialize(self, *, timeout: float, **_kwargs: object) -> dict[str, object]:
        self.initialize_timeouts.append(timeout)
        if self.initialize_error is not None:
            raise self.initialize_error
        self._initialized = True
        return {}

    def request(
        self,
        method: str,
        params: dict[str, object] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        self.calls.append((method, dict(params or {}), timeout))
        response = self.responses[method].pop(0)
        if isinstance(response, BaseException):
            raise response
        return cast(dict[str, Any], response)

    def take_notification(self, timeout: float = 0.0) -> dict[str, Any] | None:
        self.notification_timeouts.append(timeout)
        if not self.notifications:
            return None
        notification = self.notifications.pop(0)
        if isinstance(notification, BaseException):
            raise notification
        return cast(dict[str, Any], notification)

    def take_server_request(self, timeout: float = 0.0) -> dict[str, Any] | None:
        self.server_request_timeouts.append(timeout)
        if not self.server_requests:
            return None
        request = self.server_requests.pop(0)
        if isinstance(request, BaseException):
            raise request
        return cast(dict[str, Any], request)

    def respond_error(
        self,
        request_id: object,
        code: int,
        message: str,
        data: object | None = None,
    ) -> None:
        assert data is None
        self.response_errors.append((request_id, code, message))

    def close(self, timeout: float = 3.0) -> None:
        assert timeout == 3.0
        self.close_calls += 1


def _turn_completed(
    *,
    thread_id: str = THREAD_1,
    turn_id: str = TURN_1,
    status: str = "completed",
) -> dict[str, object]:
    return {
        "method": "turn/completed",
        "params": {
            "threadId": thread_id,
            "turn": {"id": turn_id, "status": status, "items": []},
        },
    }


def _persisted_registration(
    prompt: str,
    *,
    thread_id: str = THREAD_1,
    turn_id: str = TURN_1,
    status: str = "completed",
) -> dict[str, object]:
    return {
        "thread": {
            "id": thread_id,
            "cwd": SOURCE_CWD,
            "status": {"type": "idle"},
            "turns": [
                {
                    "id": turn_id,
                    "status": status,
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": prompt}],
                        }
                    ],
                }
            ],
        }
    }


def test_codex_delivery_starts_inbox_cwd_and_returns_exact_thread_id() -> None:
    clock = FakeClock()
    client = FakeCodexAppServerClient({
        "thread/start": [
            {
                "thread": {
                    "id": THREAD_1,
                    "cwd": INBOX_CWD,
                    "threadSource": RECOVERY_KEY,
                }
            }
        ],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=clock)

    created = delivery.create_thread(
        prompt="registration happens only after durable binding",
        candidate=_candidate(SOURCE_1),
        placement=_placement(),
        recovery_key=RECOVERY_KEY,
        deadline=105.0,
    )

    assert created == THREAD_1
    assert client.initialize_timeouts == [5.0]
    assert client.calls[0] == (
        "thread/start",
        {
            "cwd": INBOX_CWD,
            "runtimeWorkspaceRoots": [INBOX_CWD, SOURCE_CWD],
            "threadSource": RECOVERY_KEY,
        },
        5.0,
    )


@pytest.mark.parametrize(
    "placement",
    [
        SidebarPlacement(
            inbox_cwd=INBOX_CWD,
            local_host="local",
            runtime_workspace_roots=(SOURCE_CWD,),
            placement_generation=1,
        ),
        SidebarPlacement(
            inbox_cwd=INBOX_CWD,
            local_host="local",
            runtime_workspace_roots=(
                INBOX_CWD,
                42,
            ),  # type: ignore[arg-type]
            placement_generation=1,
        ),
        SidebarPlacement(
            inbox_cwd=INBOX_CWD,
            local_host="local",
            runtime_workspace_roots=(INBOX_CWD, SOURCE_CWD),
            placement_generation=True,  # type: ignore[arg-type]
        ),
        SidebarPlacement(
            inbox_cwd="relative/inbox",
            local_host="local",
            runtime_workspace_roots=("relative/inbox", SOURCE_CWD),
            placement_generation=1,
        ),
        SidebarPlacement(
            inbox_cwd=INBOX_CWD,
            local_host="local",
            runtime_workspace_roots=(INBOX_CWD, "relative/source"),
            placement_generation=1,
        ),
        SidebarPlacement(
            inbox_cwd=INBOX_CWD,
            local_host="local",
            runtime_workspace_roots=(
                f"{INBOX_CWD}/../session-inbox",
                SOURCE_CWD,
            ),
            placement_generation=1,
        ),
        SidebarPlacement(
            inbox_cwd="\\Users\\diego\\.hermes",
            local_host="local",
            runtime_workspace_roots=("\\Users\\diego\\.hermes", SOURCE_CWD),
            placement_generation=1,
        ),
        SidebarPlacement(
            inbox_cwd="/Users/diego/.hermes",
            local_host="local",
            runtime_workspace_roots=("/Users/diego/.hermes", SOURCE_CWD),
            placement_generation=1,
        ),
        SidebarPlacement(
            inbox_cwd="\\\\?\\C:\\Users\\diego\\.hermes",
            local_host="local",
            runtime_workspace_roots=(
                "\\\\?\\C:\\Users\\diego\\.hermes",
                SOURCE_CWD,
            ),
            placement_generation=1,
        ),
        SidebarPlacement(
            inbox_cwd="\\\\?\\UNC\\server\\share\\inbox",
            local_host="local",
            runtime_workspace_roots=(
                "\\\\?\\UNC\\server\\share\\inbox",
                SOURCE_CWD,
            ),
            placement_generation=1,
        ),
        SidebarPlacement(
            inbox_cwd="\\\\.\\pipe\\session-inbox",
            local_host="local",
            runtime_workspace_roots=(
                "\\\\.\\pipe\\session-inbox",
                SOURCE_CWD,
            ),
            placement_generation=1,
        ),
        SidebarPlacement(
            inbox_cwd="\\\\server\\pipe\\session-inbox",
            local_host="local",
            runtime_workspace_roots=(
                "\\\\server\\pipe\\session-inbox",
                SOURCE_CWD,
            ),
            placement_generation=1,
        ),
    ],
)
def test_codex_delivery_rejects_malformed_placement_before_create_dispatch(
    placement: SidebarPlacement,
) -> None:
    client = FakeCodexAppServerClient({"thread/start": []})
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    with pytest.raises(NativeCreateRejected) as caught:
        delivery.create_thread(
            prompt="registration happens later",
            candidate=_candidate(SOURCE_1),
            placement=placement,
            recovery_key=RECOVERY_KEY,
            deadline=105.0,
        )

    assert caught.value.code == "inbox_unavailable"
    assert client.calls == []


def test_codex_delivery_rejects_duplicate_equivalent_roots_before_create_dispatch() -> None:
    client = FakeCodexAppServerClient({"thread/start": []})
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)
    placement = SidebarPlacement(
        inbox_cwd=INBOX_CWD,
        local_host="local",
        runtime_workspace_roots=(
            INBOX_CWD,
            INBOX_CWD,
        ),
        placement_generation=1,
    )

    with pytest.raises(NativeCreateRejected) as caught:
        delivery.create_thread(
            prompt="registration happens later",
            candidate=_candidate(SOURCE_1, cwd=INBOX_CWD),
            placement=placement,
            recovery_key=RECOVERY_KEY,
            deadline=105.0,
        )

    assert caught.value.code == "inbox_unavailable"
    assert client.calls == []


def test_codex_delivery_rejects_returned_source_cwd_as_ambiguous() -> None:
    client = FakeCodexAppServerClient({
        "thread/start": [
            {
                "thread": {
                    "id": THREAD_1,
                    "cwd": SOURCE_CWD,
                    "threadSource": RECOVERY_KEY,
                }
            }
        ],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    with pytest.raises(NativeCreateAmbiguous):
        delivery.create_thread(
            prompt="registration happens later",
            candidate=_candidate(SOURCE_1),
            placement=_placement(),
            recovery_key=RECOVERY_KEY,
            deadline=105.0,
        )

    assert client.calls[0][1]["cwd"] == INBOX_CWD


def test_sidebar_executor_settles_placement_failure_before_reservation() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)])
    native = FakeNative(events)
    executor = _executor(
        store,
        FakeVerifier(events),
        native,
        clock,
        placement_resolver=lambda _candidate: (_ for _ in ()).throw(
            SidebarPlacementError("inbox_unavailable")
        ),
    )

    result = executor.run_once()

    assert result == SidebarExecutionResult(
        status="retry",
        job_id=f"sidebar-job:{SOURCE_1}",
        error_code="inbox_unavailable",
    )
    assert native.create_calls == 0
    assert not any(event[0] == "reserve" for event in events)


def test_sidebar_executor_maps_invalid_resolver_home_to_inbox_unavailable(
    tmp_path,
) -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    inbox = tmp_path / ".hermes"
    source = tmp_path / "source"
    inbox.mkdir()
    source.mkdir()
    store = FakeStore(events, [_job(SOURCE_1)])
    native = FakeNative(events)

    result = _executor(
        store,
        FakeVerifier(events),
        native,
        clock,
        placement_resolver=lambda _candidate: resolve_sidebar_placement(
            configured_inbox_cwd=str(inbox),
            hermes_home=str(inbox) + "\\.",
            placement_generation=1,
            source_cwd=str(source),
        ),
    ).run_once()

    assert result == SidebarExecutionResult(
        status="retry",
        job_id=f"sidebar-job:{SOURCE_1}",
        error_code="inbox_unavailable",
    )
    assert native.create_calls == 0
    assert not any(event[0] == "reserve" for event in events)


@pytest.mark.parametrize(
    ("placement", "prebound"),
    [
        (
            SidebarPlacement(
                inbox_cwd="relative/inbox",
                local_host="local",
                runtime_workspace_roots=("relative/inbox", SOURCE_CWD),
                placement_generation=1,
            ),
            False,
        ),
        (
            SidebarPlacement(
                inbox_cwd=INBOX_CWD,
                local_host="local",
                runtime_workspace_roots=(
                    INBOX_CWD,
                    f"{SOURCE_CWD}/../session-source",
                ),
                placement_generation=1,
            ),
            True,
        ),
        (
            SidebarPlacement(
                inbox_cwd="\\Users\\diego\\.hermes",
                local_host="local",
                runtime_workspace_roots=("\\Users\\diego\\.hermes", SOURCE_CWD),
                placement_generation=1,
            ),
            False,
        ),
        (
            SidebarPlacement(
                inbox_cwd="/Users/diego/.hermes",
                local_host="local",
                runtime_workspace_roots=("/Users/diego/.hermes", SOURCE_CWD),
                placement_generation=1,
            ),
            False,
        ),
        (
            SidebarPlacement(
                inbox_cwd="\\\\?\\C:\\Users\\diego\\.hermes",
                local_host="local",
                runtime_workspace_roots=(
                    "\\\\?\\C:\\Users\\diego\\.hermes",
                    SOURCE_CWD,
                ),
                placement_generation=1,
            ),
            False,
        ),
        (
            SidebarPlacement(
                inbox_cwd="\\\\?\\UNC\\server\\share\\inbox",
                local_host="local",
                runtime_workspace_roots=(
                    "\\\\?\\UNC\\server\\share\\inbox",
                    SOURCE_CWD,
                ),
                placement_generation=1,
            ),
            False,
        ),
        (
            SidebarPlacement(
                inbox_cwd="\\\\.\\pipe\\session-inbox",
                local_host="local",
                runtime_workspace_roots=(
                    "\\\\.\\pipe\\session-inbox",
                    SOURCE_CWD,
                ),
                placement_generation=1,
            ),
            False,
        ),
        (
            SidebarPlacement(
                inbox_cwd="\\\\server\\pipe\\session-inbox",
                local_host="local",
                runtime_workspace_roots=(
                    "\\\\server\\pipe\\session-inbox",
                    SOURCE_CWD,
                ),
                placement_generation=1,
            ),
            False,
        ),
    ],
)
def test_sidebar_executor_rejects_malformed_placement_before_native_paths(
    placement: SidebarPlacement,
    prebound: bool,
) -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(
        events,
        [_job(SOURCE_1, thread_id=THREAD_1 if prebound else None)],
    )
    native = FakeNative(events)

    result = _executor(
        store,
        FakeVerifier(events),
        native,
        clock,
        placement_resolver=lambda _candidate: placement,
    ).run_once()

    assert result == SidebarExecutionResult(
        status="retry",
        job_id=f"sidebar-job:{SOURCE_1}",
        error_code="inbox_unavailable",
    )
    assert native.create_calls == 0
    assert not any(event[0] in {"reserve", "recover", "read"} for event in events)


def test_sidebar_executor_rejects_duplicate_equivalent_roots_before_reservation() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(
        events,
        [_job(SOURCE_1)],
        candidate_override=_candidate(SOURCE_1, cwd=INBOX_CWD),
    )
    native = FakeNative(events)
    placement = SidebarPlacement(
        inbox_cwd=INBOX_CWD,
        local_host="local",
        runtime_workspace_roots=(
            INBOX_CWD,
            INBOX_CWD,
        ),
        placement_generation=1,
    )

    result = _executor(
        store,
        FakeVerifier(events),
        native,
        clock,
        placement_resolver=lambda _candidate: placement,
    ).run_once()

    assert result == SidebarExecutionResult(
        status="retry",
        job_id=f"sidebar-job:{SOURCE_1}",
        error_code="inbox_unavailable",
    )
    assert native.create_calls == 0
    assert not any(event[0] in {"reserve", "recover", "read"} for event in events)


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("timeout"),
        RuntimeError("unknown"),
        NativeCreateRejected("codex_tool_unavailable"),
    ],
)
def test_codex_delivery_maps_post_dispatch_create_failures_to_ambiguity(
    failure: Exception,
) -> None:
    client = FakeCodexAppServerClient({"thread/start": [failure]})
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    with pytest.raises(NativeCreateAmbiguous):
        delivery.create_thread(
            prompt="registration happens later",
            candidate=_candidate(SOURCE_1),
            placement=_placement(),
            recovery_key=RECOVERY_KEY,
            deadline=105.0,
        )


@pytest.mark.parametrize(
    "response",
    [{}, {"thread": {}}, {"thread": {"id": ""}}, {"thread": {"id": 42}}],
)
def test_codex_delivery_treats_missing_exact_create_identity_as_ambiguous(
    response: dict[str, object],
) -> None:
    client = FakeCodexAppServerClient({"thread/start": [response]})
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    with pytest.raises(NativeCreateAmbiguous):
        delivery.create_thread(
            prompt="registration happens later",
            candidate=_candidate(SOURCE_1),
            placement=_placement(),
            recovery_key=RECOVERY_KEY,
            deadline=105.0,
        )


def test_codex_delivery_rejects_expired_deadline_before_create_dispatch() -> None:
    client = FakeCodexAppServerClient({
        "thread/start": [{"thread": {"id": THREAD_1}}],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    with pytest.raises(NativeCreateRejected) as caught:
        delivery.create_thread(
            prompt="registration happens later",
            candidate=_candidate(SOURCE_1),
            placement=_placement(),
            recovery_key=RECOVERY_KEY,
            deadline=100.0,
        )

    assert caught.value.code == "broker_time_budget"
    assert client.initialize_timeouts == []
    assert client.calls == []


def test_codex_delivery_maps_initialize_failure_to_pre_dispatch_rejection() -> None:
    client = FakeCodexAppServerClient(
        {"thread/start": [{"thread": {"id": THREAD_1}}]},
        initialize_error=RuntimeError("app-server unavailable"),
    )
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    with pytest.raises(NativeCreateRejected) as caught:
        delivery.create_thread(
            prompt="registration happens later",
            candidate=_candidate(SOURCE_1),
            placement=_placement(),
            recovery_key=RECOVERY_KEY,
            deadline=105.0,
        )

    assert caught.value.code == "codex_tool_unavailable"
    assert client.calls == []


def test_codex_delivery_preflight_initializes_experimental_api_and_reads_inventory() -> (
    None
):
    client = FakeCodexAppServerClient({
        "thread/list": [{"data": [], "nextCursor": None}],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    delivery.preflight(deadline=105.0)

    assert client.initialize_timeouts == [5.0]
    assert getattr(client, "_session_bridge_experimental_api") is True
    assert client.calls == [("thread/list", {"archived": False, "limit": 1}, 5.0)]


@pytest.mark.parametrize(
    "thread",
    [
        {"id": THREAD_1, "cwd": "C:/source", "threadSource": "wrong"},
        {"id": THREAD_1, "cwd": "C:/different", "threadSource": RECOVERY_KEY},
        {"id": THREAD_1, "cwd": "C:/source"},
    ],
)
def test_codex_delivery_rejects_create_response_identity_mismatch_as_ambiguous(
    thread: dict[str, object],
) -> None:
    client = FakeCodexAppServerClient({"thread/start": [{"thread": thread}]})
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    with pytest.raises(NativeCreateAmbiguous):
        delivery.create_thread(
            prompt="registration happens later",
            candidate=_candidate(SOURCE_1),
            placement=_placement(),
            recovery_key=RECOVERY_KEY,
            deadline=105.0,
        )


def test_codex_delivery_observes_client_initialized_after_construction() -> None:
    client = FakeCodexAppServerClient({
        "thread/read": [{"thread": {"id": THREAD_1, "cwd": "C:/source", "turns": []}}],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)
    client._initialized = True

    delivery.read_thread_state(thread_id=THREAD_1, deadline=105.0)

    assert client.initialize_timeouts == []


def test_codex_delivery_reads_exact_initial_user_prompt_without_mutation() -> None:
    prompt = _registration_prompt()
    client = FakeCodexAppServerClient({
        "thread/read": [_persisted_registration(prompt)],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    observed = delivery.read_thread_initial_prompt(
        thread_id=THREAD_1,
        deadline=105.0,
    )

    assert observed == prompt
    assert [method for method, _params, _timeout in client.calls] == ["thread/read"]


def test_codex_delivery_resumes_before_reading_exact_initial_user_prompt() -> None:
    prompt = _registration_prompt()
    client = FakeCodexAppServerClient({
        "thread/read": [
            CodexAppServerError(
                code=-32600,
                message=f"thread not loaded: {THREAD_1}",
            )
        ],
        "thread/resume": [_persisted_registration(prompt)],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    observed = delivery.read_thread_initial_prompt(
        thread_id=THREAD_1,
        deadline=105.0,
    )

    assert observed == prompt
    assert [method for method, _params, _timeout in client.calls] == [
        "thread/read",
        "thread/resume",
    ]


def test_codex_delivery_rejects_missing_initial_user_prompt() -> None:
    client = FakeCodexAppServerClient({
        "thread/read": [
            {
                "thread": {
                    "id": THREAD_1,
                    "cwd": "C:/source",
                    "turns": [{"id": TURN_1, "status": "completed", "items": []}],
                }
            }
        ],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    with pytest.raises(ValueError, match="initial user prompt"):
        delivery.read_thread_initial_prompt(
            thread_id=THREAD_1,
            deadline=105.0,
        )


def test_codex_delivery_reads_exact_hydration_marker_without_mutation() -> None:
    marker = "HERMES_SESSION_HYDRATION_V1:canonical.marker"
    client = FakeCodexAppServerClient({
        "thread/read": [_persisted_registration(f"hydrate\n{marker}")],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    found = delivery.thread_has_exact_marker(
        thread_id=THREAD_1,
        marker=marker,
        deadline=105.0,
    )

    assert found is True
    assert [method for method, _params, _timeout in client.calls] == ["thread/read"]


def test_codex_delivery_resumes_not_loaded_snapshot_before_marker_reconciliation() -> None:
    marker = "HERMES_SESSION_HYDRATION_V1:canonical.marker"
    client = FakeCodexAppServerClient({
        "thread/read": [
            {
                "thread": {
                    "id": THREAD_1,
                    "cwd": "C:/source",
                    "status": {"type": "notLoaded"},
                    "turns": [],
                }
            }
        ],
        "thread/resume": [
            _persisted_registration(f"hydrate\n{marker}", status="interrupted")
        ],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    found = delivery.thread_has_exact_marker(
        thread_id=THREAD_1,
        marker=marker,
        deadline=105.0,
    )

    assert found is True
    assert [method for method, _params, _timeout in client.calls] == [
        "thread/read",
        "thread/resume",
    ]


def test_codex_delivery_starts_one_text_turn_and_verifies_hydration_marker() -> None:
    marker = "HERMES_SESSION_HYDRATION_V1:canonical.marker"
    message = f"# Imported Claude Code Session\n\nHydration marker: {marker}"
    client = FakeCodexAppServerClient(
        {
            "thread/resume": [
                {"thread": {"id": THREAD_1, "cwd": "C:/source", "turns": []}}
            ],
            "turn/start": [{"turn": {"id": TURN_1}}],
        },
        notifications=[_turn_completed()],
    )
    fresh_client = FakeCodexAppServerClient({
        "thread/resume": [_persisted_registration(message)],
    })
    delivery = CodexAppServerSidebarDelivery(
        client,
        fresh_client_factory=lambda: fresh_client,
        monotonic=lambda: 100.0,
    )

    delivery.start_text_turn_and_verify_marker(
        thread_id=THREAD_1,
        message=message,
        marker=marker,
        deadline=105.0,
    )

    assert [method for method, _params, _timeout in client.calls] == [
        "thread/resume",
        "turn/start",
    ]
    assert client.calls[0][1] == {"threadId": THREAD_1}
    assert client.calls[1][1] == {
        "threadId": THREAD_1,
        "input": [{"type": "text", "text": message}],
    }
    assert [method for method, _params, _timeout in fresh_client.calls] == [
        "thread/resume"
    ]


def test_codex_delivery_converts_post_dispatch_failure_to_turn_ambiguity() -> None:
    marker = "HERMES_SESSION_HYDRATION_V1:canonical.marker"
    message = f"# Imported Claude Code Session\n\nHydration marker: {marker}"
    client = FakeCodexAppServerClient(
        {
            "thread/resume": [
                {"thread": {"id": THREAD_1, "cwd": "C:/source", "turns": []}}
            ],
            "turn/start": [RuntimeError("transport closed after dispatch")],
        }
    )
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    with pytest.raises(NativeTurnAmbiguous):
        delivery.start_text_turn_and_verify_marker(
            thread_id=THREAD_1,
            message=message,
            marker=marker,
            deadline=105.0,
        )


def _registration_prompt(source: str = SOURCE_1) -> str:
    expected = BridgeMarkerPayload(
        bridge_id=sidebar_bridge_id(source),
        source_session_id=source,
        target_provider=Provider.CODEX,
        policy_generation=1,
    )
    return build_registration_prompt(
        _candidate(source),
        encode_bridge_marker(expected, SECRET),
    )


def test_codex_delivery_registration_is_idempotent_when_exact_marker_exists() -> None:
    prompt = _registration_prompt()
    client = FakeCodexAppServerClient({
        "thread/read": [
            {
                "thread": {
                    "id": THREAD_1,
                    "cwd": "C:/source",
                    "turns": [
                        {
                            "status": "completed",
                            "items": [
                                {
                                    "type": "userMessage",
                                    "content": [{"type": "text", "text": prompt}],
                                }
                            ],
                        }
                    ],
                }
            }
        ],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    delivery.register_thread(thread_id=THREAD_1, prompt=prompt, deadline=105.0)

    assert [method for method, _params, _timeout in client.calls] == ["thread/read"]


def test_codex_delivery_resumes_exact_unloaded_thread_before_registration() -> None:
    prompt = _registration_prompt()
    client = FakeCodexAppServerClient({
        "thread/read": [
            CodexAppServerError(
                code=-32600,
                message=f"thread not loaded: {THREAD_1}",
            )
        ],
        "thread/resume": [
            {
                "thread": {
                    "id": THREAD_1,
                    "cwd": "C:/source",
                    "turns": [
                        {
                            "status": "completed",
                            "items": [
                                {
                                    "type": "userMessage",
                                    "content": [{"type": "text", "text": prompt}],
                                }
                            ],
                        }
                    ],
                }
            }
        ],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    delivery.register_thread(thread_id=THREAD_1, prompt=prompt, deadline=105.0)

    assert [method for method, _params, _timeout in client.calls] == [
        "thread/read",
        "thread/resume",
    ]
    assert client.calls[1][1] == {"threadId": THREAD_1}


@pytest.mark.parametrize("embedded", ["X{marker}", "{marker}X"])
def test_codex_delivery_does_not_accept_marker_embedded_in_larger_token(
    embedded: str,
) -> None:
    prompt = _registration_prompt()
    marker = next(
        line.removeprefix("Signed marker: ")
        for line in prompt.splitlines()
        if line.startswith("Signed marker: ")
    )
    client = FakeCodexAppServerClient(
        {
            "thread/read": [
                {
                    "thread": {
                        "id": THREAD_1,
                        "cwd": "C:/source",
                        "turns": [
                            {
                                "items": [
                                    {
                                        "type": "userMessage",
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": embedded.format(marker=marker),
                                            }
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                }
            ],
            "turn/start": [{"turn": {"id": TURN_1}}],
        },
        notifications=[_turn_completed()],
    )
    fresh_client = FakeCodexAppServerClient({
        "thread/resume": [_persisted_registration(prompt)]
    })
    delivery = CodexAppServerSidebarDelivery(
        client,
        fresh_client_factory=lambda: fresh_client,
        monotonic=lambda: 100.0,
    )

    delivery.register_thread(thread_id=THREAD_1, prompt=prompt, deadline=105.0)

    assert [method for method, _params, _timeout in client.calls] == [
        "thread/read",
        "turn/start",
    ]
    assert client.notification_timeouts == [0.25]
    assert [method for method, _params, _timeout in fresh_client.calls] == [
        "thread/resume"
    ]
    assert fresh_client.close_calls == 1


def test_codex_delivery_starts_registration_turn_when_exact_marker_is_absent() -> None:
    prompt = _registration_prompt()
    client = FakeCodexAppServerClient(
        {
            "thread/read": [
                {"thread": {"id": THREAD_1, "cwd": "C:/source", "turns": []}}
            ],
            "turn/start": [{"turn": {"id": TURN_1}}],
        },
        notifications=[_turn_completed()],
    )
    fresh_client = FakeCodexAppServerClient({
        "thread/resume": [_persisted_registration(prompt)]
    })
    delivery = CodexAppServerSidebarDelivery(
        client,
        fresh_client_factory=lambda: fresh_client,
        monotonic=lambda: 100.0,
    )

    delivery.register_thread(thread_id=THREAD_1, prompt=prompt, deadline=105.0)

    assert client.calls == [
        (
            "thread/read",
            {"threadId": THREAD_1, "includeTurns": True},
            5.0,
        ),
        (
            "turn/start",
            {
                "threadId": THREAD_1,
                "input": [{"type": "text", "text": prompt}],
            },
            5.0,
        ),
    ]
    assert client.notification_timeouts == [0.25]
    assert fresh_client.initialize_timeouts == [5.0]
    assert fresh_client.calls == [("thread/resume", {"threadId": THREAD_1}, 5.0)]
    assert fresh_client.close_calls == 1


def test_codex_delivery_fresh_registration_never_reads_before_first_turn() -> None:
    prompt = _registration_prompt()
    client = FakeCodexAppServerClient(
        {"turn/start": [{"turn": {"id": TURN_1}}]},
        notifications=[_turn_completed()],
    )
    fresh_client = FakeCodexAppServerClient({
        "thread/resume": [_persisted_registration(prompt)]
    })
    delivery = CodexAppServerSidebarDelivery(
        client,
        fresh_client_factory=lambda: fresh_client,
        monotonic=lambda: 100.0,
    )

    delivery.register_thread(
        thread_id=THREAD_1,
        prompt=prompt,
        deadline=105.0,
        fresh=True,
    )

    assert [method for method, _params, _timeout in client.calls] == ["turn/start"]
    assert [method for method, _params, _timeout in fresh_client.calls] == [
        "thread/resume"
    ]


def test_codex_delivery_uses_durable_read_over_failed_completion_event() -> None:
    prompt = _registration_prompt()
    client = FakeCodexAppServerClient(
        {"turn/start": [{"turn": {"id": TURN_1}}]},
        notifications=[_turn_completed(status="failed")],
    )
    fresh_client = FakeCodexAppServerClient({
        "thread/resume": [_persisted_registration(prompt)]
    })
    delivery = CodexAppServerSidebarDelivery(
        client,
        fresh_client_factory=lambda: fresh_client,
        monotonic=lambda: 100.0,
    )

    delivery.register_thread(
        thread_id=THREAD_1,
        prompt=prompt,
        deadline=105.0,
        fresh=True,
    )

    assert fresh_client.close_calls == 1


def test_codex_delivery_rejects_completion_without_durable_exact_turn() -> None:
    prompt = _registration_prompt()
    client = FakeCodexAppServerClient(
        {"turn/start": [{"turn": {"id": TURN_1}}]},
        notifications=[_turn_completed()],
    )
    fresh_client = FakeCodexAppServerClient({
        "thread/resume": [_persisted_registration(prompt, status="failed")]
    })
    delivery = CodexAppServerSidebarDelivery(
        client,
        fresh_client_factory=lambda: fresh_client,
        monotonic=lambda: 100.0,
    )

    with pytest.raises(NativeCreateAmbiguous):
        delivery.register_thread(
            thread_id=THREAD_1,
            prompt=prompt,
            deadline=105.0,
            fresh=True,
        )

    assert fresh_client.close_calls == 1


def test_codex_delivery_quarantines_fresh_registration_with_no_rollout() -> None:
    prompt = _registration_prompt()
    client = FakeCodexAppServerClient(
        {"turn/start": [{"turn": {"id": TURN_1}}]},
        notifications=[_turn_completed()],
    )
    fresh_client = FakeCodexAppServerClient({
        "thread/resume": [
            CodexAppServerError(
                code=-32600,
                message=f"no rollout found for thread id {THREAD_1}",
            )
        ]
    })
    delivery = CodexAppServerSidebarDelivery(
        client,
        fresh_client_factory=lambda: fresh_client,
        monotonic=lambda: 100.0,
    )

    with pytest.raises(NativeThreadUnrecoverable) as caught:
        delivery.register_thread(
            thread_id=THREAD_1,
            prompt=prompt,
            deadline=105.0,
            fresh=True,
        )

    assert caught.value.thread_id == THREAD_1
    assert fresh_client.close_calls == 1


def test_codex_delivery_rejects_unexpected_registration_server_request() -> None:
    client = FakeCodexAppServerClient(
        {
            "thread/read": [
                {"thread": {"id": THREAD_1, "cwd": "C:/source", "turns": []}}
            ],
            "turn/start": [{"turn": {"id": TURN_1}}],
        },
        server_requests=[{"id": 91, "method": "item/commandExecution/requestApproval"}],
    )
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    with pytest.raises(NativeCreateAmbiguous):
        delivery.register_thread(
            thread_id=THREAD_1,
            prompt=_registration_prompt(),
            deadline=105.0,
        )

    assert client.response_errors == [
        (91, -32600, "session bridge registration forbids server requests")
    ]


def test_codex_delivery_reads_exact_idle_thread_with_remaining_deadline() -> None:
    client = FakeCodexAppServerClient({
        "thread/read": [{"thread": {"id": THREAD_1, "cwd": "C:/source", "turns": []}}],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    state = delivery.read_thread_state(thread_id=THREAD_1, deadline=105.0)

    assert state == NativeThreadState(
        thread_id=THREAD_1,
        status=NativeThreadStatus.IDLE,
        cwd="C:/source",
    )
    assert client.calls == [
        (
            "thread/read",
            {"threadId": THREAD_1, "includeTurns": True},
            5.0,
        )
    ]


def test_codex_delivery_resumes_exact_unloaded_thread_before_reading_state() -> None:
    client = FakeCodexAppServerClient({
        "thread/read": [
            CodexAppServerError(
                code=-32600,
                message=f"thread not loaded: {THREAD_1}",
            )
        ],
        "thread/resume": [
            {
                "thread": {
                    "id": THREAD_1,
                    "cwd": "C:/source",
                    "status": {"type": "idle"},
                    "turns": [],
                }
            }
        ],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    state = delivery.read_thread_state(thread_id=THREAD_1, deadline=105.0)

    assert state == NativeThreadState(
        thread_id=THREAD_1,
        status=NativeThreadStatus.IDLE,
        cwd="C:/source",
    )
    assert [method for method, _params, _timeout in client.calls] == [
        "thread/read",
        "thread/resume",
    ]
    assert client.calls[1][1] == {"threadId": THREAD_1}


def test_codex_delivery_quarantines_exact_id_with_no_rollout() -> None:
    client = FakeCodexAppServerClient({
        "thread/read": [
            CodexAppServerError(
                code=-32600,
                message=f"thread not loaded: {THREAD_1}",
            )
        ],
        "thread/resume": [
            CodexAppServerError(
                code=-32600,
                message=f"no rollout found for thread id {THREAD_1}",
            )
        ],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    with pytest.raises(NativeThreadUnrecoverable) as caught:
        delivery.register_thread(
            thread_id=THREAD_1,
            prompt=_registration_prompt(),
            deadline=105.0,
        )

    assert caught.value.thread_id == THREAD_1


def test_codex_delivery_never_resumes_when_not_loaded_error_names_other_id() -> None:
    client = FakeCodexAppServerClient({
        "thread/read": [
            CodexAppServerError(
                code=-32600,
                message=f"thread not loaded: {THREAD_2}",
            )
        ],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    with pytest.raises(CodexAppServerError):
        delivery.read_thread_state(thread_id=THREAD_1, deadline=105.0)

    assert [method for method, _params, _timeout in client.calls] == ["thread/read"]


@pytest.mark.parametrize(
    ("wire_status", "expected"),
    [
        ({"type": "idle"}, NativeThreadStatus.IDLE),
        ({"type": "notLoaded"}, NativeThreadStatus.IDLE),
        ({"type": "active", "activeFlags": []}, NativeThreadStatus.ACTIVE),
        ({"type": "systemError"}, NativeThreadStatus.TERMINAL),
    ],
)
def test_codex_delivery_maps_closed_app_server_thread_statuses(
    wire_status: dict[str, object],
    expected: NativeThreadStatus,
) -> None:
    client = FakeCodexAppServerClient({
        "thread/read": [
            {
                "thread": {
                    "id": THREAD_1,
                    "cwd": "C:/source",
                    "status": wire_status,
                    "turns": [],
                }
            }
        ],
    })
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    state = delivery.read_thread_state(thread_id=THREAD_1, deadline=105.0)

    assert state is not None
    assert state.status is expected


def test_codex_delivery_renames_exact_thread_with_remaining_deadline() -> None:
    client = FakeCodexAppServerClient({"thread/name/set": [{}]})
    delivery = CodexAppServerSidebarDelivery(client, monotonic=lambda: 100.0)

    delivery.rename_thread(
        thread_id=THREAD_1,
        title="[Claude] exact title",
        deadline=105.0,
    )

    assert client.calls == [
        (
            "thread/name/set",
            {"threadId": THREAD_1, "name": "[Claude] exact title"},
            5.0,
        )
    ]


@dataclass
class FakeClock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _candidate(source: str, *, cwd: str = SOURCE_CWD) -> SidebarCandidate:
    return SidebarCandidate(
        source_session_id=source,
        provider=Provider.CLAUDE,
        bridge_id=sidebar_bridge_id(source),
        title=f"[Claude] {source}",
        cwd=cwd,
        git_root=cwd,
        git_branch="main",
        git_head="a" * 40,
        worktree_id=None,
        eligible_at=10.0,
    )


def _expected_recovery_key(source: str) -> str:
    expected = BridgeMarkerPayload(
        bridge_id=sidebar_bridge_id(source),
        source_session_id=source,
        target_provider=Provider.CODEX,
        policy_generation=1,
    )
    marker = encode_bridge_marker(expected, SECRET)
    digest = hmac.new(
        SECRET,
        b"sidebar-create-v1\0" + marker.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"hermes-session-bridge-create-v1:{digest}"


def _job(source: str, *, thread_id: str | None = None) -> dict[str, Any]:
    return {
        "id": f"sidebar-job:{source}",
        "source_session_id": source,
        "bridge_id": sidebar_bridge_id(source),
        "state": "sidebar_pending",
        "codex_thread_id": thread_id,
    }


class FakeStore:
    def __init__(
        self,
        events: list[tuple[Any, ...]],
        jobs: list[dict[str, Any]],
        *,
        bind_error: Exception | None = None,
        commit_error: Exception | None = None,
        candidate_error: Exception | None = None,
        candidate_override: SidebarCandidate | None = None,
        failure_state: str | None = None,
        failure_error: Exception | None = None,
        failure_result: object | None = None,
        heartbeat_error: Exception | None = None,
        reservations: dict[str, dict[str, Any]] | None = None,
        worker_lock_available: bool = True,
        execution_blockers: tuple[str, ...] = (),
        active_lease: bool = False,
        execution_blockers_after_claim: tuple[str, ...] = (),
        preview_sources: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.events = events
        self.jobs = jobs
        self.bind_error = bind_error
        self.commit_error = commit_error
        self.candidate_error = candidate_error
        self.candidate_override = candidate_override
        self.failure_state = failure_state
        self.failure_error = failure_error
        self.failure_result = failure_result
        self.heartbeat_error = heartbeat_error
        self.reservations = dict(reservations or {})
        self.worker_lock_available = worker_lock_available
        self.execution_blockers = execution_blockers
        self.active_lease = active_lease
        self.execution_blockers_after_claim = execution_blockers_after_claim
        self.preview_sources = dict(preview_sources or {})
        self.candidates = {
            source: _candidate(source)
            for job in jobs
            if isinstance((source := job.get("source_session_id")), str)
        }
        self.failures: list[str] = []
        self.failure_thread_ids: list[str | None] = []
        self.heartbeats: list[float] = []

    class _Lock:
        def release(self) -> None:
            return None

    def try_acquire_sidebar_worker_lock(self) -> _Lock | None:
        return self._Lock() if self.worker_lock_available else None

    def sidebar_execution_blockers(self) -> tuple[str, ...]:
        return self.execution_blockers

    def sidebar_has_active_lease(self, *, now: float) -> bool:
        del now
        return self.active_lease

    def claim_sidebar_jobs(
        self, *, now: float, limit: int, lease_seconds: int
    ) -> list[dict[str, Any]]:
        self.events.append(("claim", limit, lease_seconds, now))
        if self.execution_blockers_after_claim:
            self.execution_blockers = self.execution_blockers_after_claim
            return []
        claimed = self.jobs[:limit]
        self.jobs = self.jobs[limit:]
        return [
            {
                **job,
                "state": "sidebar_leased",
                "lease_token": f"lease:{job['id']}",
                "lease_expires_at": now + lease_seconds,
            }
            for job in claimed
        ]

    def record_sidebar_broker_heartbeat(self, *, now: float) -> None:
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        self.heartbeats.append(now)

    def get_sidebar_candidate_for_delivery(
        self, source_session_id: str
    ) -> SidebarCandidate:
        self.events.append(("candidate", source_session_id))
        if self.candidate_error is not None:
            raise self.candidate_error
        if self.candidate_override is not None:
            return self.candidate_override
        return self.candidates[source_session_id]

    def get_sidebar_preview_source(self, source_session_id: str) -> dict[str, Any]:
        self.events.append(("preview", source_session_id))
        return self.preview_sources[source_session_id]

    def bind_sidebar_thread(
        self, *, lease_token: str, codex_thread_id: str, now: float
    ) -> dict[str, Any]:
        self.events.append(("bind", codex_thread_id))
        if self.bind_error is not None:
            raise self.bind_error
        return {
            "state": "sidebar_leased",
            "codex_thread_id": codex_thread_id,
            "lease_token": lease_token,
            "updated_at": now,
        }

    def get_sidebar_create_reservation(
        self, source_session_id: str
    ) -> dict[str, Any] | None:
        return self.reservations.get(source_session_id)

    def reserve_sidebar_create(
        self,
        *,
        lease_token: str,
        recovery_key: str,
        reconciliation_proof_digest: str,
        reconciliation_generation: str,
        now: float,
    ) -> dict[str, Any]:
        del lease_token
        source_session_id = next(iter(self.candidates))
        candidate = self.candidates[source_session_id]
        existing = self.reservations.get(source_session_id)
        if existing is not None:
            if existing["recovery_key"] != recovery_key:
                raise ValueError("conflicting sidebar create reservation")
            return existing
        reservation = {
            "version": 2,
            "job_id": f"sidebar-job:{source_session_id}",
            "source_session_id": source_session_id,
            "bridge_id": candidate.bridge_id,
            "recovery_key": recovery_key,
            "reconciliation_proof_digest": reconciliation_proof_digest,
            "reconciliation_generation": reconciliation_generation,
            "reserved_at": now,
        }
        self.events.append(("reserve", recovery_key))
        self.reservations[source_session_id] = reservation
        return reservation

    def record_sidebar_reconciliation_proof(
        self,
        *,
        lease_token: str,
        evidence: SidebarReconciliationEvidence,
        marker_digest: str,
        placement_generation: int,
        delivery_generation: int,
        now: float,
    ) -> dict[str, Any]:
        del lease_token, marker_digest, placement_generation, delivery_generation, now
        evidence.validate()
        return {
            "proof_digest": hashlib.sha256(
                evidence.generation.encode("utf-8")
            ).hexdigest(),
            "reconciliation_generation": evidence.generation,
            "state": evidence.state.value,
            "recovered_thread_id": evidence.recovered_thread_id,
        }

    def clear_sidebar_create_reservation(
        self, *, lease_token: str, recovery_key: str, now: float
    ) -> None:
        del lease_token, now
        source_session_id = next(iter(self.candidates))
        existing = self.reservations.get(source_session_id)
        if existing is None or existing["recovery_key"] != recovery_key:
            raise ValueError("conflicting sidebar create reservation")
        self.events.append(("clear_reservation", recovery_key))
        del self.reservations[source_session_id]

    def commit_sidebar_job_with_lineage(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
        source_session_id: str,
        bridge_id: str,
        placement_generation: int,
        now: float,
    ) -> dict[str, Any]:
        self.events.append(("commit", codex_thread_id, placement_generation))
        if self.commit_error is not None:
            raise self.commit_error
        return {
            "state": "sidebar_visible",
            "codex_thread_id": codex_thread_id,
            "source_session_id": source_session_id,
            "bridge_id": bridge_id,
            "placement_generation": placement_generation,
            "lease_token": lease_token,
            "updated_at": now,
        }

    def upsert_projection(self, projection: SessionProjection) -> object:
        self.events.append(("index", projection.native_id))
        return object()

    def fail_sidebar_job(
        self,
        *,
        lease_token: str,
        error_code: str,
        now: float,
        codex_thread_id: str | None = None,
    ) -> dict[str, Any]:
        del lease_token, now
        self.events.append(("fail", error_code))
        self.failures.append(error_code)
        self.failure_thread_ids.append(codex_thread_id)
        if self.failure_error is not None:
            raise self.failure_error
        if self.failure_result is not None:
            return self.failure_result  # type: ignore[return-value]
        state = self.failure_state or (
            "sidebar_failed" if error_code in SIDEBAR_FATAL_ERRORS else "sidebar_retry"
        )
        return {"state": state, "error_code": error_code}


class FakeVerifier:
    def __init__(
        self,
        events: list[tuple[Any, ...]],
        *,
        find_result: VerifiedSidebarThread | None = None,
        recovery_result: str | None = None,
        recovery_error: Exception | None = None,
        projection: SessionProjection | None = None,
    ) -> None:
        self.events = events
        self.find_result = find_result
        self.recovery_result = recovery_result
        self.recovery_error = recovery_error
        self.projection = projection

    def find_by_marker(
        self, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread | None:
        self.events.append(("find", expected.source_session_id))
        return self.find_result

    def reconcile_marker(
        self,
        expected: BridgeMarkerPayload,
        *,
        now: float,
        ttl_seconds: float,
    ) -> SidebarReconciliationEvidence:
        recovered = self.find_by_marker(expected)
        marker = encode_bridge_marker(expected, SECRET)
        return SidebarReconciliationEvidence.create(
            state=(
                SidebarReconciliationState.RECOVERED
                if recovered is not None
                else SidebarReconciliationState.ABSENCE_PROVEN
            ),
            generation=f"fake:{expected.source_session_id}:{now}",
            completed_at=now,
            expires_at=now + ttl_seconds,
            inventory_digest=hashlib.sha256(
                repr(recovered).encode("utf-8")
            ).hexdigest(),
            marker_digest=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            match_count=int(recovered is not None),
            recovered_thread_id=(
                recovered.thread_id if recovered is not None else None
            ),
            fixed_reason=None,
        )

    def find_by_recovery_key(
        self,
        recovery_key: str,
        *,
        expected_cwd: str,
        deadline: float,
    ) -> str | None:
        self.events.append(("recover", recovery_key, expected_cwd, deadline))
        if self.recovery_error is not None:
            raise self.recovery_error
        return self.recovery_result

    def verify_thread(
        self, *, thread_id: str, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread:
        self.events.append(("verify", thread_id))
        return VerifiedSidebarThread(
            thread_id=thread_id,
            source_session_id=expected.source_session_id,
            bridge_id=expected.bridge_id,
            projection=self.projection,
        )


class FakeNative:
    def __init__(
        self,
        events: list[tuple[Any, ...]],
        *,
        create_result: object = THREAD_1,
        statuses: list[str | None] | None = None,
        rename_error: Exception | None = None,
        read_thread_id: str | None = None,
        read_cwd: str = INBOX_CWD,
        after_create: Callable[[], None] | None = None,
        register_error: Exception | None = None,
        preflight_error: Exception | None = None,
        initial_prompt: str | None = None,
    ) -> None:
        self.events = events
        self.create_result = create_result
        self.statuses = list(statuses or ["idle"])
        self.rename_error = rename_error
        self.read_thread_id = read_thread_id
        self.read_cwd = read_cwd
        self.after_create = after_create
        self.register_error = register_error
        self.preflight_error = preflight_error
        self.initial_prompt = initial_prompt
        self.preflight_calls = 0
        self.create_calls = 0
        self.created_prompts: list[str] = []
        self.registered_prompts: list[str] = []
        self.initial_prompt_reads: list[str] = []

    def preflight(self, *, deadline: float) -> None:
        del deadline
        self.preflight_calls += 1
        if self.preflight_error is not None:
            raise self.preflight_error

    def create_thread(
        self,
        *,
        prompt: str,
        candidate: SidebarCandidate,
        placement: SidebarPlacement,
        recovery_key: str,
        deadline: float,
    ) -> str:
        assert candidate.cwd == SOURCE_CWD
        assert placement == _placement()
        assert "Signed marker:" in prompt
        assert recovery_key.startswith("hermes-session-bridge-create-v1:")
        self.events.append((
            "create",
            candidate.source_session_id,
            recovery_key,
            deadline,
        ))
        self.create_calls += 1
        self.created_prompts.append(prompt)
        if isinstance(self.create_result, Exception):
            raise self.create_result
        if self.after_create is not None:
            self.after_create()
        return self.create_result  # type: ignore[return-value]

    def register_thread(
        self,
        *,
        thread_id: str,
        prompt: str,
        deadline: float,
        fresh: bool = False,
    ) -> None:
        assert "Signed marker:" in prompt
        assert isinstance(fresh, bool)
        self.registered_prompts.append(prompt)
        self.events.append(("register", thread_id, deadline, fresh))
        if self.register_error is not None:
            raise self.register_error

    def read_thread_state(
        self, *, thread_id: str, deadline: float
    ) -> NativeThreadState | None:
        self.events.append(("read", thread_id, deadline))
        if len(self.statuses) > 1:
            status = self.statuses.pop(0)
        else:
            status = self.statuses[0]
        if status is None:
            return None
        return NativeThreadState(
            thread_id=self.read_thread_id or thread_id,
            status=NativeThreadStatus(status),
            cwd=self.read_cwd,
        )

    def read_thread_initial_prompt(
        self, *, thread_id: str, deadline: float
    ) -> str:
        del deadline
        self.initial_prompt_reads.append(thread_id)
        if self.initial_prompt is not None:
            return self.initial_prompt
        return self.registered_prompts[-1]

    def rename_thread(self, *, thread_id: str, title: str, deadline: float) -> None:
        assert title.startswith("[Claude]")
        self.events.append(("rename", thread_id, deadline))
        if self.rename_error is not None:
            raise self.rename_error


def _executor(
    store: FakeStore,
    verifier: FakeVerifier,
    native: FakeNative,
    clock: FakeClock,
    *,
    read_timeout_seconds: float = 60.0,
    operation_budget_seconds: float = 240.0,
    readable_preview_enabled: bool = False,
    preview_budget_chars: int = 24_000,
    placement_resolver: Callable[[SidebarCandidate], SidebarPlacement] = (
        lambda _candidate: _placement()
    ),
) -> SidebarExecutor:
    return SidebarExecutor(
        store=cast(SessionBridgeStore, store),
        verifier=cast(SidebarThreadVerifier, verifier),
        native=native,
        placement_resolver=placement_resolver,
        marker_secret=SECRET,
        clock=clock,
        monotonic=clock,
        sleep=clock.sleep,
        read_timeout_seconds=read_timeout_seconds,
        poll_interval=1.0,
        operation_budget_seconds=operation_budget_seconds,
        readable_preview_enabled=readable_preview_enabled,
        preview_budget_chars=preview_budget_chars,
    )


def test_recovered_exact_id_is_verified_renamed_and_committed_without_create() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)])
    native = FakeNative(events)
    executor = _executor(store, FakeVerifier(events), native, clock)

    result = executor.run_once()

    assert result == SidebarExecutionResult(
        status="visible",
        job_id=f"sidebar-job:{SOURCE_1}",
        thread_id=THREAD_1,
    )
    assert native.create_calls == 0
    assert store.heartbeats == [100.0]
    assert [event[0] for event in events] == [
        "claim",
        "candidate",
        "read",
        "bind",
        "register",
        "read",
        "verify",
        "rename",
        "commit",
    ]
    assert events[4][3] is False


def test_verified_projection_is_indexed_before_lineage_commit() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)])
    native = FakeNative(events)
    projection = SessionProjection(
        provider=Provider.CODEX,
        native_id=THREAD_1,
        title="[Claude] Indexed before commit",
        cwd=INBOX_CWD,
        started_at=90.0,
        last_active=100.0,
        messages=(),
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id=sidebar_bridge_id(SOURCE_1),
    )
    verifier = FakeVerifier(events, projection=projection)

    result = _executor(store, verifier, native, clock).run_once()

    assert result.status == "visible"
    assert [event[0] for event in events] == [
        "claim",
        "candidate",
        "read",
        "bind",
        "register",
        "read",
        "verify",
        "index",
        "rename",
        "commit",
    ]


def test_final_verification_rejects_projection_in_source_placement() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)])
    projection = SessionProjection(
        provider=Provider.CODEX,
        native_id=THREAD_1,
        title="[Claude] Wrong placement",
        cwd="C:/source",
        started_at=90.0,
        last_active=100.0,
        messages=(),
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id=sidebar_bridge_id(SOURCE_1),
    )

    result = _executor(
        store,
        FakeVerifier(events, projection=projection),
        FakeNative(events),
        clock,
    ).run_once()

    assert result == SidebarExecutionResult(
        status="failed",
        job_id=f"sidebar-job:{SOURCE_1}",
        thread_id=THREAD_1,
        error_code="placement_mismatch",
    )
    assert not any(event[0] in {"index", "rename", "commit"} for event in events)


def test_final_verification_rejects_authenticated_registration_source_cwd_mismatch() -> (
    None
):
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)])
    expected = BridgeMarkerPayload(
        bridge_id=sidebar_bridge_id(SOURCE_1),
        source_session_id=SOURCE_1,
        target_provider=Provider.CODEX,
        policy_generation=1,
    )
    marker = encode_bridge_marker(expected, SECRET)
    wrong_source = (
        "C:/other-source" if os.name == "nt" else "/srv/other-session-source"
    )
    initial_prompt = build_registration_prompt(
        _candidate(SOURCE_1, cwd=wrong_source),
        marker,
    )
    native = FakeNative(events, initial_prompt=initial_prompt)

    result = _executor(
        store,
        FakeVerifier(events),
        native,
        clock,
    ).run_once()

    assert result == SidebarExecutionResult(
        status="failed",
        job_id=f"sidebar-job:{SOURCE_1}",
        thread_id=THREAD_1,
        error_code="source_identity_mismatch",
    )
    assert native.initial_prompt_reads == [THREAD_1]
    assert not any(event[0] in {"index", "rename", "commit"} for event in events)


def test_final_verification_accepts_exact_inbox_and_authenticated_source_identity() -> (
    None
):
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)])
    native = FakeNative(events)

    result = _executor(
        store,
        FakeVerifier(events),
        native,
        clock,
    ).run_once()

    assert result == SidebarExecutionResult(
        status="visible",
        job_id=f"sidebar-job:{SOURCE_1}",
        thread_id=THREAD_1,
    )
    assert native.initial_prompt_reads == [THREAD_1]
    assert store.failures == []


def test_initial_prompt_read_budget_expiry_stops_before_projection_index() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)])
    projection = SessionProjection(
        provider=Provider.CODEX,
        native_id=THREAD_1,
        title="[Claude] Budget expires during prompt read",
        cwd=INBOX_CWD,
        started_at=90.0,
        last_active=100.0,
        messages=(),
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id=sidebar_bridge_id(SOURCE_1),
    )

    class ExpiringPromptReadNative(FakeNative):
        def read_thread_initial_prompt(
            self, *, thread_id: str, deadline: float
        ) -> str:
            prompt = super().read_thread_initial_prompt(
                thread_id=thread_id,
                deadline=deadline,
            )
            clock.sleep(241.0)
            return prompt

    native = ExpiringPromptReadNative(events)
    result = _executor(
        store,
        FakeVerifier(events, projection=projection),
        native,
        clock,
    ).run_once()

    assert result == SidebarExecutionResult(
        status="retry",
        job_id=f"sidebar-job:{SOURCE_1}",
        thread_id=THREAD_1,
        error_code="broker_time_budget",
    )
    assert store.failures == ["broker_time_budget"]
    assert store.failure_thread_ids == [THREAD_1]
    assert not any(event[0] in {"index", "rename", "commit"} for event in events)


def test_initial_prompt_read_preserves_native_budget_rejection() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)])

    class BudgetRejectedPromptReadNative(FakeNative):
        def read_thread_initial_prompt(
            self, *, thread_id: str, deadline: float
        ) -> str:
            del deadline
            self.initial_prompt_reads.append(thread_id)
            raise NativeCreateRejected("broker_time_budget")

    native = BudgetRejectedPromptReadNative(events)
    result = _executor(
        store,
        FakeVerifier(events),
        native,
        clock,
    ).run_once()

    assert result == SidebarExecutionResult(
        status="retry",
        job_id=f"sidebar-job:{SOURCE_1}",
        thread_id=THREAD_1,
        error_code="broker_time_budget",
    )
    assert native.initial_prompt_reads == [THREAD_1]
    assert store.failures == ["broker_time_budget"]
    assert store.failure_thread_ids == [THREAD_1]
    assert not any(event[0] in {"index", "rename", "commit"} for event in events)


def test_idle_cycle_records_broker_heartbeat_under_the_executor_lock() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [])
    executor = _executor(store, FakeVerifier(events), FakeNative(events), clock)

    result = executor.run_once()

    assert result == SidebarExecutionResult(status="idle")
    assert store.heartbeats == [100.0]
    assert events == [("claim", 1, 300, 100.0)]


def test_provider_preflight_failure_never_claims_a_sidebar_job() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)])
    native = FakeNative(
        events,
        preflight_error=NativeCreateRejected("codex_tool_unavailable"),
    )

    result = _executor(store, FakeVerifier(events), native, clock).run_once()

    assert result == SidebarExecutionResult(
        status="unsettled",
        error_code="codex_tool_unavailable",
    )
    assert native.preflight_calls == 1
    assert events == []
    assert store.jobs == [_job(SOURCE_1)]


def test_cross_process_lock_contention_is_degraded_and_never_claims() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(
        events,
        [_job(SOURCE_1)],
        worker_lock_available=False,
    )
    native = FakeNative(events)

    result = _executor(store, FakeVerifier(events), native, clock).run_once()

    assert result == SidebarExecutionResult(
        status="unsettled",
        error_code="bridge_temporarily_unavailable",
    )
    assert native.preflight_calls == 0
    assert events == []
    assert store.jobs == [_job(SOURCE_1)]


@pytest.mark.parametrize(
    "blocker",
    [
        "sidebar_failed",
        "sidebar_terminal_resolution_mismatch",
        "sidebar_terminal_resolution_ledger_invalid",
        "unknown_retry_code",
    ],
)
def test_existing_hard_stop_row_prevents_claim(blocker: str) -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(
        events,
        [_job(SOURCE_1)],
        execution_blockers=(blocker,),
    )
    native = FakeNative(events)

    result = _executor(store, FakeVerifier(events), native, clock).run_once()

    assert result == SidebarExecutionResult(
        status="unsettled",
        error_code="source_identity_mismatch",
    )
    assert native.preflight_calls == 1
    assert events == []
    assert store.jobs == [_job(SOURCE_1)]


def test_active_database_lease_is_unsettled_and_never_claims_or_heartbeats() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)], active_lease=True)
    native = FakeNative(events)

    result = _executor(store, FakeVerifier(events), native, clock).run_once()

    assert result == SidebarExecutionResult(
        status="unsettled",
        error_code="bridge_temporarily_unavailable",
    )
    assert native.preflight_calls == 1
    assert events == []
    assert store.heartbeats == []
    assert store.jobs == [_job(SOURCE_1)]


def test_empty_claim_that_creates_hard_stop_is_never_reported_idle() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(
        events,
        [_job(SOURCE_1)],
        execution_blockers_after_claim=("sidebar_failed",),
    )

    result = _executor(
        store,
        FakeVerifier(events),
        FakeNative(events),
        clock,
    ).run_once()

    assert result == SidebarExecutionResult(
        status="unsettled",
        error_code="source_identity_mismatch",
    )
    assert events == [("claim", 1, 300, 100.0)]
    assert store.heartbeats == []
    assert store.jobs == [_job(SOURCE_1)]


def test_idle_cycle_fails_closed_when_broker_heartbeat_cannot_persist() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [], heartbeat_error=RuntimeError("write failed"))
    executor = _executor(store, FakeVerifier(events), FakeNative(events), clock)

    result = executor.run_once()

    assert result == SidebarExecutionResult(
        status="unsettled",
        error_code="bridge_temporarily_unavailable",
    )
    assert events == [("claim", 1, 300, 100.0)]


def test_claimed_cycle_releases_lease_when_broker_heartbeat_cannot_persist() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(
        events,
        [_job(SOURCE_1)],
        heartbeat_error=RuntimeError("write failed"),
    )
    native = FakeNative(events)
    executor = _executor(store, FakeVerifier(events), native, clock)

    result = executor.run_once()

    assert result == SidebarExecutionResult(
        status="retry",
        job_id=f"sidebar-job:{SOURCE_1}",
        error_code="bridge_temporarily_unavailable",
    )
    assert native.create_calls == 0
    assert [event[0] for event in events] == ["claim", "fail"]


def test_zero_marker_candidates_create_bind_and_register_before_any_read() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)])
    native = FakeNative(events, create_result=THREAD_1)
    executor = _executor(store, FakeVerifier(events), native, clock)

    result = executor.run_once()

    assert result.status == "visible"
    assert result.thread_id == THREAD_1
    assert [event[0] for event in events] == [
        "claim",
        "candidate",
        "find",
        "reserve",
        "create",
        "bind",
        "register",
        "read",
        "verify",
        "rename",
        "commit",
    ]
    assert events[3][1] == _expected_recovery_key(SOURCE_1)
    assert events[4][2] == _expected_recovery_key(SOURCE_1)
    assert events[6][3] is True


def test_continuous_executor_creates_readable_registration_prompt_when_enabled() -> (
    None
):
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    preview_source = {
        "source_session_id": SOURCE_1,
        "provider": "claude",
        "source_cursor": "cursor-1",
        "source_hash": "hash-1",
        "title": "Readable canary",
        "captured_at": 8.0,
        "messages": [
            {"role": "user", "content": "first message", "timestamp": 1.0},
            {"role": "assistant", "content": "second message", "timestamp": 2.0},
        ],
    }
    store = FakeStore(
        events,
        [_job(SOURCE_1)],
        preview_sources={SOURCE_1: preview_source},
    )
    native = FakeNative(events, create_result=THREAD_1)

    result = _executor(
        store,
        FakeVerifier(events),
        native,
        clock,
        readable_preview_enabled=True,
    ).run_once()

    assert result.status == "visible"
    assert native.created_prompts == native.registered_prompts
    prompt = native.created_prompts[0]
    assert prompt.startswith("# Imported Claude Code Session")
    assert "## Last 5 Messages" in prompt
    assert "first message" in prompt
    assert "second message" in prompt
    assert "## Bridge Registration" in prompt
    assert "Signed marker:" in prompt
    assert [event[0] for event in events][:6] == [
        "claim",
        "candidate",
        "preview",
        "find",
        "reserve",
        "create",
    ]


def test_existing_create_reservation_with_zero_recovery_never_creates() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    recovery_key = _expected_recovery_key(SOURCE_1)
    store = FakeStore(
        events,
        [_job(SOURCE_1)],
        reservations={
            SOURCE_1: {
                "version": 1,
                "job_id": f"sidebar-job:{SOURCE_1}",
                "source_session_id": SOURCE_1,
                "bridge_id": sidebar_bridge_id(SOURCE_1),
                "recovery_key": recovery_key,
                "reserved_at": 50.0,
            }
        },
    )
    native = FakeNative(events)

    result = _executor(store, FakeVerifier(events), native, clock).run_once()
    replay = _executor(store, FakeVerifier(events), native, clock).run_once()

    assert result == SidebarExecutionResult(
        status="failed",
        job_id=f"sidebar-job:{SOURCE_1}",
        error_code="native_create_ambiguous",
    )
    assert native.create_calls == 0
    assert replay == SidebarExecutionResult(status="idle")
    assert store.reservations[SOURCE_1]["recovery_key"] == recovery_key
    assert [event[0] for event in events] == [
        "claim",
        "candidate",
        "find",
        "recover",
        "fail",
        "claim",
    ]


def test_existing_create_reservation_recovers_exact_tagged_thread_without_create() -> (
    None
):
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    recovery_key = _expected_recovery_key(SOURCE_1)
    store = FakeStore(
        events,
        [_job(SOURCE_1)],
        reservations={
            SOURCE_1: {
                "version": 1,
                "job_id": f"sidebar-job:{SOURCE_1}",
                "source_session_id": SOURCE_1,
                "bridge_id": sidebar_bridge_id(SOURCE_1),
                "recovery_key": recovery_key,
                "reserved_at": 50.0,
            }
        },
    )
    native = FakeNative(events)
    verifier = FakeVerifier(events, recovery_result=THREAD_1)

    result = _executor(store, verifier, native, clock).run_once()

    assert result.status == "visible"
    assert result.thread_id == THREAD_1
    assert native.create_calls == 0
    assert [event[0] for event in events][:6] == [
        "claim",
        "candidate",
        "find",
        "recover",
        "read",
        "bind",
    ]


def test_ambiguous_registration_keeps_bound_exact_id_and_retries() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)])
    native = FakeNative(
        events,
        create_result=THREAD_1,
        register_error=TimeoutError("turn/start outcome unknown"),
    )

    result = _executor(store, FakeVerifier(events), native, clock).run_once()

    assert result == SidebarExecutionResult(
        status="retry",
        job_id=f"sidebar-job:{SOURCE_1}",
        thread_id=THREAD_1,
        error_code="native_task_not_indexed",
    )
    assert [event[0] for event in events][-3:] == ["bind", "register", "fail"]
    assert native.create_calls == 1


def test_unrecoverable_prebound_id_is_quarantined_without_replacement() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)])
    native = FakeNative(
        events,
        register_error=NativeThreadUnrecoverable(THREAD_1),
    )

    result = _executor(store, FakeVerifier(events), native, clock).run_once()

    assert result == SidebarExecutionResult(
        status="failed",
        job_id=f"sidebar-job:{SOURCE_1}",
        thread_id=THREAD_1,
        error_code="native_create_ambiguous",
    )
    assert native.create_calls == 0
    assert store.failure_thread_ids == [THREAD_1]


def test_unrecoverable_fresh_id_is_quarantined_after_one_create() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)])
    native = FakeNative(
        events,
        create_result=THREAD_1,
        register_error=NativeThreadUnrecoverable(THREAD_1),
    )

    result = _executor(store, FakeVerifier(events), native, clock).run_once()

    assert result == SidebarExecutionResult(
        status="failed",
        job_id=f"sidebar-job:{SOURCE_1}",
        thread_id=THREAD_1,
        error_code="native_create_ambiguous",
    )
    assert native.create_calls == 1
    assert [event[0] for event in events][-3:] == ["bind", "register", "fail"]
    assert store.failure_thread_ids == [THREAD_1]


def test_ambiguous_create_is_quarantined_and_never_replaced() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)])
    native = FakeNative(events, create_result=NativeCreateAmbiguous())
    executor = _executor(store, FakeVerifier(events), native, clock)

    first = executor.run_once()
    second = executor.run_once()

    assert first == SidebarExecutionResult(
        status="failed",
        job_id=f"sidebar-job:{SOURCE_1}",
        error_code="native_create_ambiguous",
    )
    assert second == SidebarExecutionResult(status="idle")
    assert native.create_calls == 1
    assert store.failures == ["native_create_ambiguous"]
    assert not any(event[0] in {"bind", "read", "rename", "commit"} for event in events)


def test_unknown_create_exception_is_fatal_ambiguity() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)])
    native = FakeNative(events, create_result=RuntimeError())

    result = _executor(store, FakeVerifier(events), native, clock).run_once()

    assert result.status == "failed"
    assert result.error_code == "native_create_ambiguous"
    assert store.failures == ["native_create_ambiguous"]


def test_explicit_pre_dispatch_create_rejection_is_retryable() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)])
    native = FakeNative(
        events,
        create_result=NativeCreateRejected("codex_tool_unavailable"),
    )

    result = _executor(store, FakeVerifier(events), native, clock).run_once()

    assert result.status == "retry"
    assert result.error_code == "codex_tool_unavailable"
    assert store.failures == ["codex_tool_unavailable"]
    assert store.reservations == {}
    assert [event[0] for event in events][-2:] == ["clear_reservation", "fail"]


@pytest.mark.parametrize("create_result", [None, "", 42])
def test_missing_or_malformed_create_result_is_fatal_ambiguity(
    create_result: object,
) -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)])
    native = FakeNative(events, create_result=create_result)
    executor = _executor(store, FakeVerifier(events), native, clock)

    result = executor.run_once()

    assert result == SidebarExecutionResult(
        status="failed",
        job_id=f"sidebar-job:{SOURCE_1}",
        error_code="native_create_ambiguous",
    )
    assert store.failures == ["native_create_ambiguous"]
    assert native.create_calls == 1
    assert not any(event[0] in {"bind", "read", "rename", "commit"} for event in events)


def test_bind_ambiguity_releases_once_without_read_rename_or_commit() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)], bind_error=TimeoutError())
    native = FakeNative(events)
    executor = _executor(store, FakeVerifier(events), native, clock)

    result = executor.run_once()

    assert result.error_code == "bridge_temporarily_unavailable"
    assert result.status == "retry"
    assert store.failures == ["bridge_temporarily_unavailable"]
    assert store.failure_thread_ids == [THREAD_1]
    assert [event[0] for event in events] == [
        "claim",
        "candidate",
        "find",
        "reserve",
        "create",
        "bind",
        "fail",
    ]


def test_retryable_failure_reports_failed_when_retry_budget_is_exhausted() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(
        events,
        [_job(SOURCE_1)],
        bind_error=TimeoutError(),
        failure_state="sidebar_failed",
    )

    result = _executor(
        store,
        FakeVerifier(events),
        FakeNative(events),
        clock,
    ).run_once()

    assert result.status == "failed"
    assert result.error_code == "bridge_temporarily_unavailable"


@pytest.mark.parametrize(
    ("failure_error", "failure_result"),
    [
        (TimeoutError(), None),
        (None, {"state": "not-a-sidebar-state"}),
    ],
)
def test_settlement_failure_reports_unsettled_without_claiming_transition(
    failure_error: Exception | None,
    failure_result: object | None,
) -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(
        events,
        [_job(SOURCE_1)],
        bind_error=TimeoutError(),
        failure_error=failure_error,
        failure_result=failure_result,
    )

    result = _executor(
        store,
        FakeVerifier(events),
        FakeNative(events),
        clock,
    ).run_once()

    assert result.status == "unsettled"
    assert result.error_code == "bridge_temporarily_unavailable"
    assert store.failures == ["bridge_temporarily_unavailable"]


def test_candidate_lookup_corruption_settles_once_as_source_identity_mismatch() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(
        events,
        [_job(SOURCE_1)],
        candidate_error=ValueError("corrupt candidate"),
    )

    result = _executor(
        store,
        FakeVerifier(events),
        FakeNative(events),
        clock,
    ).run_once()

    assert result.status == "failed"
    assert result.error_code == "source_identity_mismatch"
    assert store.failures == ["source_identity_mismatch"]
    assert [event[0] for event in events] == ["claim", "candidate", "fail"]


def test_post_claim_parse_error_settles_once_when_lease_token_is_available() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    malformed = _job(SOURCE_1)
    malformed["source_session_id"] = None
    store = FakeStore(events, [malformed])

    result = _executor(
        store,
        FakeVerifier(events),
        FakeNative(events),
        clock,
    ).run_once()

    assert result.status == "failed"
    assert result.error_code == "source_identity_mismatch"
    assert store.failures == ["source_identity_mismatch"]


@pytest.mark.parametrize("container_type", [list, tuple])
def test_multi_claim_batch_releases_every_recoverable_lease(container_type) -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()

    class OverclaimStore(FakeStore):
        def claim_sidebar_jobs(
            self, *, now: float, limit: int, lease_seconds: int
        ) -> object:
            self.events.append(("claim", limit, lease_seconds, now))
            claims = [
                {
                    **job,
                    "state": "sidebar_leased",
                    "lease_token": f"lease:{job['id']}",
                    "lease_expires_at": now + lease_seconds,
                }
                for job in self.jobs
            ]
            self.jobs = []
            return container_type(claims)

    store = OverclaimStore(
        events,
        [_job(SOURCE_1), _job(SOURCE_2)],
    )

    result = _executor(
        store,
        FakeVerifier(events),
        FakeNative(events),
        clock,
    ).run_once()

    assert result.status == "unsettled"
    assert result.error_code == "source_identity_mismatch"
    assert store.failures == [
        "broker_time_budget",
        "broker_time_budget",
    ]
    assert [event[0] for event in events] == ["claim", "fail", "fail"]


def test_post_claim_parse_error_without_lease_token_is_explicitly_unsettled() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()

    class MissingLeaseStore(FakeStore):
        def claim_sidebar_jobs(
            self, *, now: float, limit: int, lease_seconds: int
        ) -> list[dict[str, Any]]:
            claims = super().claim_sidebar_jobs(
                now=now,
                limit=limit,
                lease_seconds=lease_seconds,
            )
            claims[0]["lease_token"] = None
            return claims

    store = MissingLeaseStore(events, [_job(SOURCE_1)])

    result = _executor(
        store,
        FakeVerifier(events),
        FakeNative(events),
        clock,
    ).run_once()

    assert result.status == "unsettled"
    assert result.error_code == "source_identity_mismatch"
    assert store.failures == []


def test_read_until_idle_timeout_is_retryable_and_never_renames() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)])
    native = FakeNative(events, statuses=["active"])
    executor = _executor(
        store,
        FakeVerifier(events),
        native,
        clock,
        read_timeout_seconds=2.0,
    )

    result = executor.run_once()

    assert result.error_code == "native_task_not_indexed"
    assert result.status == "retry"
    assert sum(event[0] == "read" for event in events) == 4
    assert not any(event[0] in {"verify", "rename", "commit"} for event in events)


@pytest.mark.parametrize(
    ("read_thread_id", "read_cwd", "expected_code"),
    [
        (THREAD_2, INBOX_CWD, "codex_thread_conflict"),
        (THREAD_1, "/different-source", "placement_mismatch"),
    ],
)
def test_native_read_identity_mismatch_is_fatal(
    read_thread_id: str,
    read_cwd: str,
    expected_code: str,
) -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)])
    native = FakeNative(
        events,
        read_thread_id=read_thread_id,
        read_cwd=read_cwd,
    )
    executor = _executor(store, FakeVerifier(events), native, clock)

    result = executor.run_once()

    assert result.status == "failed"
    assert result.error_code == expected_code
    assert store.failures == [expected_code]
    assert not any(event[0] in {"bind", "register"} for event in events)
    assert not any(event[0] in {"verify", "rename", "commit"} for event in events)


def test_prebound_rejected_placement_read_releases_exact_id_without_mutation() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(
        events,
        [_job(SOURCE_1, thread_id=THREAD_1)],
        failure_state="sidebar_pending",
    )

    class BudgetReadNative(FakeNative):
        def read_thread_state(
            self, *, thread_id: str, deadline: float
        ) -> NativeThreadState | None:
            self.events.append(("read", thread_id, deadline))
            raise NativeCreateRejected("broker_time_budget")

    result = _executor(
        store,
        FakeVerifier(events),
        BudgetReadNative(events),
        clock,
    ).run_once()

    assert result == SidebarExecutionResult(
        status="retry",
        job_id=f"sidebar-job:{SOURCE_1}",
        thread_id=THREAD_1,
        error_code="broker_time_budget",
    )
    assert store.failures == ["broker_time_budget"]
    assert store.failure_thread_ids == [THREAD_1]
    assert not any(event[0] in {"bind", "register"} for event in events)


def test_native_read_accepts_platform_equivalent_cwd() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)])
    native = FakeNative(
        events,
        read_cwd=("c:\\Users\\diego\\.hermes" if os.name == "nt" else INBOX_CWD),
    )
    executor = _executor(store, FakeVerifier(events), native, clock)

    result = executor.run_once()

    assert result.status == "visible"


def test_terminal_native_state_fails_without_polling() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)])
    native = FakeNative(events, statuses=["terminal"])

    result = _executor(store, FakeVerifier(events), native, clock).run_once()

    assert result.status == "retry"
    assert result.error_code == "native_task_not_indexed"
    assert sum(event[0] == "read" for event in events) == 2


def test_rename_failure_retries_without_commit_or_replacement() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)])
    native = FakeNative(events, rename_error=RuntimeError())
    executor = _executor(store, FakeVerifier(events), native, clock)

    result = executor.run_once()

    assert result.error_code == "rename_failed"
    assert result.status == "retry"
    assert native.create_calls == 0
    assert [event[0] for event in events][-2:] == ["rename", "fail"]
    assert not any(event[0] == "commit" for event in events)


def test_commit_ambiguity_releases_once_without_replacement() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(
        events, [_job(SOURCE_1, thread_id=THREAD_1)], commit_error=TimeoutError()
    )
    native = FakeNative(events)
    executor = _executor(store, FakeVerifier(events), native, clock)

    result = executor.run_once()

    assert result.error_code == "bridge_temporarily_unavailable"
    assert result.status == "retry"
    assert native.create_calls == 0
    assert [event[0] for event in events][-2:] == ["commit", "fail"]
    assert store.failures == ["bridge_temporarily_unavailable"]


def test_commit_waits_for_fresh_thread_lineage_index_without_releasing_lease() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()

    class EventuallyIndexedStore(FakeStore):
        commit_attempts = 0

        def commit_sidebar_job_with_lineage(
            self,
            *,
            lease_token: str,
            codex_thread_id: str,
            source_session_id: str,
            bridge_id: str,
            placement_generation: int,
            now: float,
        ) -> dict[str, Any]:
            self.commit_attempts += 1
            if self.commit_attempts == 1:
                self.events.append(("commit", codex_thread_id))
                raise SidebarNativeTaskNotIndexed()
            return super().commit_sidebar_job_with_lineage(
                lease_token=lease_token,
                codex_thread_id=codex_thread_id,
                source_session_id=source_session_id,
                bridge_id=bridge_id,
                placement_generation=placement_generation,
                now=now,
            )

    store = EventuallyIndexedStore(events, [_job(SOURCE_1)])
    native = FakeNative(events)

    result = _executor(store, FakeVerifier(events), native, clock).run_once()

    assert result == SidebarExecutionResult(
        status="visible",
        job_id=f"sidebar-job:{SOURCE_1}",
        thread_id=THREAD_1,
    )
    assert native.create_calls == 1
    assert store.commit_attempts == 2
    assert store.failures == []
    assert clock.now == 101.0
    assert [event[0] for event in events][-3:] == ["rename", "commit", "commit"]


def test_commit_index_wait_timeout_retains_exact_thread_without_recreating() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()

    class NeverIndexedStore(FakeStore):
        commit_attempts = 0

        def commit_sidebar_job_with_lineage(
            self,
            *,
            lease_token: str,
            codex_thread_id: str,
            source_session_id: str,
            bridge_id: str,
            placement_generation: int,
            now: float,
        ) -> dict[str, Any]:
            del (
                lease_token,
                source_session_id,
                bridge_id,
                placement_generation,
                now,
            )
            self.commit_attempts += 1
            self.events.append(("commit", codex_thread_id))
            raise SidebarNativeTaskNotIndexed()

    store = NeverIndexedStore(events, [_job(SOURCE_1)])
    native = FakeNative(events)

    result = _executor(
        store,
        FakeVerifier(events),
        native,
        clock,
        read_timeout_seconds=2.0,
    ).run_once()

    assert result == SidebarExecutionResult(
        status="retry",
        job_id=f"sidebar-job:{SOURCE_1}",
        thread_id=THREAD_1,
        error_code="native_task_not_indexed",
    )
    assert native.create_calls == 1
    assert store.commit_attempts == 3
    assert store.failure_thread_ids == [THREAD_1]
    assert store.failures == ["native_task_not_indexed"]
    assert clock.now == 102.0


@pytest.mark.parametrize(
    ("stage", "store_error", "expected_code", "expected_status"),
    [
        (
            "bind",
            ValueError("conflicting Codex thread identity"),
            "codex_thread_conflict",
            "failed",
        ),
        (
            "commit",
            ValueError("source_identity_mismatch"),
            "source_identity_mismatch",
            "failed",
        ),
        (
            "bind",
            sqlite3.OperationalError("database is locked"),
            "sqlite_busy",
            "retry",
        ),
        (
            "commit",
            sqlite3.OperationalError("database is busy"),
            "sqlite_busy",
            "retry",
        ),
    ],
)
def test_store_errors_use_fixed_identity_or_transient_codes(
    stage: str,
    store_error: Exception,
    expected_code: str,
    expected_status: str,
) -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(
        events,
        [_job(SOURCE_1, thread_id=THREAD_1)],
        bind_error=store_error if stage == "bind" else None,
        commit_error=store_error if stage == "commit" else None,
    )

    result = _executor(
        store,
        FakeVerifier(events),
        FakeNative(events),
        clock,
    ).run_once()

    assert result.status == expected_status
    assert result.error_code == expected_code
    assert store.failures == [expected_code]


@pytest.mark.parametrize(
    "budget",
    [0.0, -1.0, 300.0, 301.0, math.inf],
)
def test_operation_budget_must_be_positive_and_strictly_shorter_than_lease(
    budget: float,
) -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()

    with pytest.raises(ValueError, match="operation budget"):
        _executor(
            FakeStore(events, [_job(SOURCE_1)]),
            FakeVerifier(events),
            FakeNative(events),
            clock,
            operation_budget_seconds=budget,
        )


def test_every_native_operation_receives_one_absolute_deadline() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)])

    result = _executor(
        store,
        FakeVerifier(events),
        FakeNative(events),
        clock,
        operation_budget_seconds=240.0,
    ).run_once()

    assert result.status == "visible"
    native_deadlines = [
        event[-1] for event in events if event[0] in {"create", "read", "rename"}
    ]
    assert native_deadlines == [340.0, 340.0, 340.0]
    assert native_deadlines[0] < 400.0


def test_budget_exhaustion_after_create_binds_then_settles_before_native_read() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)])
    native = FakeNative(
        events,
        after_create=lambda: clock.sleep(240.0),
    )

    result = _executor(
        store,
        FakeVerifier(events),
        native,
        clock,
        operation_budget_seconds=240.0,
    ).run_once()

    assert result.status == "retry"
    assert result.error_code == "broker_time_budget"
    assert [event[0] for event in events][-2:] == ["bind", "fail"]
    assert not any(event[0] in {"read", "rename", "commit"} for event in events)


def test_budget_exhaustion_after_marker_recovery_binds_exact_id_before_retry() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)])

    class SlowRecoveryVerifier(FakeVerifier):
        def find_by_marker(
            self, expected: BridgeMarkerPayload
        ) -> VerifiedSidebarThread | None:
            recovered = super().find_by_marker(expected)
            clock.sleep(240.0)
            return recovered

    verifier = SlowRecoveryVerifier(
        events,
        find_result=VerifiedSidebarThread(
            thread_id=THREAD_1,
            source_session_id=SOURCE_1,
            bridge_id=sidebar_bridge_id(SOURCE_1),
        ),
    )
    native = FakeNative(events)

    result = _executor(
        store,
        verifier,
        native,
        clock,
        operation_budget_seconds=240.0,
    ).run_once()

    assert result.status == "retry"
    assert result.error_code == "broker_time_budget"
    assert [event[0] for event in events][-2:] == ["bind", "fail"]
    assert native.create_calls == 0


def test_run_once_claims_and_processes_only_one_job_serially() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1), _job(SOURCE_2)])
    native = FakeNative(events, create_result=THREAD_1)
    executor = _executor(store, FakeVerifier(events), native, clock)

    result = executor.run_once()

    assert result.job_id == f"sidebar-job:{SOURCE_1}"
    assert [job["source_session_id"] for job in store.jobs] == [SOURCE_2]
    assert [event for event in events if event[0] == "claim"] == [
        ("claim", 1, 300, 100.0)
    ]
    assert native.create_calls == 1
    assert not any(SOURCE_2 in map(str, event) for event in events)


def test_two_executor_instances_share_one_process_wide_delivery_lock() -> None:
    entered_create = threading.Event()
    release_create = threading.Event()
    second_claimed = threading.Event()
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()

    class BlockingNative(FakeNative):
        def create_thread(
            self,
            *,
            prompt: str,
            candidate: SidebarCandidate,
            placement: SidebarPlacement,
            recovery_key: str,
            deadline: float,
        ) -> str:
            result = super().create_thread(
                prompt=prompt,
                candidate=candidate,
                placement=placement,
                recovery_key=recovery_key,
                deadline=deadline,
            )
            entered_create.set()
            assert release_create.wait(timeout=5)
            return result

    class SecondStore(FakeStore):
        def claim_sidebar_jobs(
            self, *, now: float, limit: int, lease_seconds: int
        ) -> list[dict[str, Any]]:
            second_claimed.set()
            return super().claim_sidebar_jobs(
                now=now,
                limit=limit,
                lease_seconds=lease_seconds,
            )

    first = _executor(
        FakeStore(events, [_job(SOURCE_1)]),
        FakeVerifier(events),
        BlockingNative(events, create_result=THREAD_1),
        clock,
    )
    second = _executor(
        SecondStore(events, [_job(SOURCE_2)]),
        FakeVerifier(events),
        FakeNative(events, create_result=THREAD_2),
        clock,
    )
    results: list[SidebarExecutionResult] = []
    first_thread = threading.Thread(target=lambda: results.append(first.run_once()))
    second_thread = threading.Thread(target=lambda: results.append(second.run_once()))

    first_thread.start()
    assert entered_create.wait(timeout=5)
    second_thread.start()
    assert not second_claimed.wait(timeout=0.2)
    release_create.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert [result.status for result in results] == ["visible", "visible"]
    assert second_claimed.is_set()
