"""Tests for CodexAppServerSession — drive turns through a mock client.

The session adapter has the most complex behavior of the three new modules:
notification draining, server-request handling (approvals), interrupt,
deadline timeouts. These tests pin all of that without spawning real codex.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch
from typing import Any, Callable, Optional

import pytest

import agent.transports.codex_app_server_session as session_mod
from agent.transports.codex_app_server import CodexAppServerError
from agent.transports.codex_app_server_session import (
    CodexAppServerSession,
    _INITIAL_USER_ECHO_MARKER,
    _ServerRequestRouting,
    _approval_choice_to_codex_decision,
    _coerce_turn_input_text,
)


class FakeClient:
    """Stand-in for CodexAppServerClient that records calls and lets the test
    drive the notification / server-request streams synchronously."""

    def __init__(self, *, codex_bin: str = "codex", codex_home=None) -> None:
        self.codex_bin = codex_bin
        self.codex_home = codex_home
        self.requests: list[tuple[str, dict]] = []
        self.notifications_responses: list[dict] = []
        self.responses: list[tuple[Any, dict]] = []
        self.error_responses: list[tuple[Any, int, str]] = []
        self._initialized = False
        self._closed = False
        self._notifications: list[dict] = []
        self._server_requests: list[dict] = []
        self._request_handler: Optional[Callable[[str, dict], dict]] = None

    # API matching CodexAppServerClient
    def initialize(self, **kwargs):
        self._initialized = True
        return {"userAgent": "fake/0.0.0", "codexHome": "/tmp",
                "platformOs": "linux", "platformFamily": "unix"}

    def request(self, method: str, params: Optional[dict] = None, timeout: float = 30.0):
        self.requests.append((method, params or {}))
        if self._request_handler is not None:
            return self._request_handler(method, params or {})
        # Sensible defaults for protocol methods used by the session
        if method == "thread/start":
            return {"thread": {"id": "thread-fake-001"},
                    "activePermissionProfile": {"id": "workspace-write"}}
        if method == "thread/resume":
            return {"thread": {"id": (params or {}).get("threadId")},
                    "activePermissionProfile": {"id": "workspace-write"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-fake-001"}}
        if method == "turn/interrupt":
            return {}
        if method == "turn/steer":
            return {"turnId": (params or {}).get("expectedTurnId")}
        return {}

    def notify(self, method: str, params=None):
        pass

    def respond(self, request_id, result, *, timeout=5.0):
        self.responses.append((request_id, result))

    def respond_error(
        self, request_id, code, message, data=None, *, timeout=5.0
    ):
        self.error_responses.append((request_id, code, message))

    def take_notification(self, timeout: float = 0.0):
        if self._notifications:
            return self._notifications.pop(0)
        # Honor a tiny sleep so the loop doesn't hot-spin; the real client
        # blocks on a queue. For tests we want determinism.
        if timeout > 0:
            time.sleep(min(timeout, 0.001))
        return None

    def take_server_request(self, timeout: float = 0.0):
        if self._server_requests:
            return self._server_requests.pop(0)
        return None

    def close(self):
        self._closed = True

    def is_alive(self) -> bool:
        # Fake is "alive" until close() is called; tests that want a dead
        # subprocess can patch this attribute or call close() directly.
        return not self._closed

    def stderr_tail(self, n: int = 20):
        return list(getattr(self, "_stderr_tail", []))[-n:]

    # Test helpers
    def queue_notification(self, method: str, **params):
        # Keep legacy fixture shorthand aligned with the IDs returned by the
        # fake thread/start and turn/start responses.
        if params.get("threadId") in {"t", "th"}:
            params["threadId"] = "thread-fake-001"
        if params.get("turnId") == "tu1":
            params["turnId"] = "turn-fake-001"
        turn = params.get("turn")
        if isinstance(turn, dict) and turn.get("id") == "tu1":
            turn = dict(turn)
            turn["id"] = "turn-fake-001"
            params["turn"] = turn
        self._notifications.append({"method": method, "params": params})

    def queue_server_request(self, method: str, request_id: Any = "srv-1", **params):
        self._server_requests.append({"id": request_id, "method": method, "params": params})

    def set_stderr_tail(self, lines):
        """Test helper: seed stderr_tail() output for OAuth-refresh classifier tests."""
        self._stderr_tail = list(lines)


def make_session(client: FakeClient, **kwargs) -> CodexAppServerSession:
    return CodexAppServerSession(
        cwd="/tmp",
        client_factory=lambda **kw: client,
        **kwargs,
    )


# ---- choice mapping ----

class TestApprovalChoiceMapping:
    @pytest.mark.parametrize("choice,expected", [
        ("once", "accept"),
        ("session", "acceptForSession"),
        ("always", "acceptForSession"),
        ("deny", "decline"),
        ("anything-else", "decline"),
    ])
    def test_mapping(self, choice, expected):
        assert _approval_choice_to_codex_decision(choice) == expected


class TestTurnInputCoercion:
    def test_list_content_keeps_text_and_marks_images(self):
        text = _coerce_turn_input_text([
            {"type": "text", "text": "caption"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ])
        assert text == "caption\n\n[image attached]"


# ---- lifecycle ----

class TestLifecycle:
    def test_ensure_started_is_idempotent(self):
        client = FakeClient()
        s = make_session(client)
        tid_a = s.ensure_started()
        tid_b = s.ensure_started()
        assert tid_a == tid_b == "thread-fake-001"
        # thread/start should be called exactly once
        method_calls = [m for (m, _) in client.requests if m == "thread/start"]
        assert len(method_calls) == 1

    def test_thread_start_passes_cwd_only(self):
        """thread/start carries cwd. We intentionally do NOT pass `permissions`
        on this codex version (experimentalApi-gated + requires matching
        config.toml [permissions] table). Letting codex use its default
        (read-only unless user configures otherwise) is the documented path."""
        client = FakeClient()
        s = make_session(client, permission_profile="workspace-write")
        s.ensure_started()
        method, params = next(r for r in client.requests if r[0] == "thread/start")
        assert params["cwd"] == "/tmp"
        assert "permissions" not in params  # see session.ensure_started() comment

    def test_prior_thread_uses_resume_with_current_cwd(self):
        client = FakeClient()
        s = make_session(client, prior_thread_id="opaque-prior")

        s.ensure_started()

        method, params = client.requests[-1]
        assert method == "thread/resume"
        assert params["cwd"] == "/tmp"
        assert "threadId" in params
        assert "permissions" not in params

    def test_stale_resume_starts_fresh_once_without_logging_id(
        self, caplog
    ):
        from agent.transports.codex_app_server import CodexAppServerError

        client = FakeClient()
        prior = "opaque-prior-not-for-logs"

        def handle(method, params):
            if method == "thread/resume":
                raise CodexAppServerError(
                    code=-32600,
                    message=f"thread not found: {prior}",
                )
            if method == "thread/start":
                return {"thread": {"id": "opaque-replacement"}}
            return {}

        client._request_handler = handle
        s = make_session(client, prior_thread_id=prior)

        s.ensure_started()

        methods = [method for method, _ in client.requests]
        assert methods == ["thread/resume", "thread/start"]
        assert prior not in caplog.text

    def test_explicit_no_rollout_found_starts_fresh_once(self):
        client = FakeClient()

        def handle(method, params):
            if method == "thread/resume":
                raise CodexAppServerError(
                    code=-32600,
                    message="no rollout found for thread id opaque-prior",
                )
            if method == "thread/start":
                return {"thread": {"id": "opaque-replacement"}}
            return {}

        client._request_handler = handle
        session = make_session(client, prior_thread_id="opaque-prior")

        assert session.ensure_started() == "opaque-replacement"
        assert [method for method, _ in client.requests] == [
            "thread/resume",
            "thread/start",
        ]

    def test_non_stale_resume_error_does_not_start_fresh(self):
        from agent.transports.codex_app_server import CodexAppServerError

        client = FakeClient()

        def handle(method, params):
            if method == "thread/resume":
                raise CodexAppServerError(
                    code=-32600,
                    message="another process currently owns this thread",
                )
            return {"thread": {"id": "unexpected"}}

        client._request_handler = handle
        s = make_session(client, prior_thread_id="opaque-prior")

        with pytest.raises(CodexAppServerError):
            s.ensure_started()

        assert [method for method, _ in client.requests] == ["thread/resume"]

    @pytest.mark.parametrize(
        "code,message",
        [
            (-32602, "Invalid params: required MCP field missing"),
            (-32603, "Internal error while loading runtime configuration"),
            (-32601, "Method not found: thread/resume"),
            (-32600, "Authentication session expired while resuming thread"),
            (-32600, "Required MCP server config not found for thread/resume"),
            (-32600, "Session not found for the active authentication profile"),
            (-32600, "No rollout directory configured for this profile"),
        ],
    )
    def test_generic_json_rpc_errors_do_not_discard_continuity(
        self, code, message
    ):
        from agent.transports.codex_app_server import CodexAppServerError

        client = FakeClient()

        def handle(method, params):
            if method == "thread/resume":
                raise CodexAppServerError(code=code, message=message)
            return {"thread": {"id": "unexpected"}}

        client._request_handler = handle
        session = make_session(client, prior_thread_id="opaque-prior")

        with pytest.raises(CodexAppServerError):
            session.ensure_started()

        assert [method for method, _ in client.requests] == ["thread/resume"]

    def test_close_idempotent(self):
        client = FakeClient()
        s = make_session(client)
        s.ensure_started()
        s.close()
        s.close()
        assert client._closed is True


# ---- turn loop ----

class TestRunTurn:
    def test_simple_text_turn_returns_final_message(self):
        client = FakeClient()
        client.queue_notification("turn/started", threadId="t", turn={"id": "tu1"})
        client.queue_notification(
            "item/completed",
            item={"type": "agentMessage", "id": "m1", "text": "hello world"},
            threadId="t", turnId="tu1",
        )
        client.queue_notification(
            "turn/completed",
            threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )
        s = make_session(client)
        r = s.run_turn("hi", turn_timeout=2.0)
        assert r.final_text == "hello world"
        assert r.interrupted is False
        assert r.timed_out is False
        assert r.error is None
        assert r.should_retire is False
        assert any(m["role"] == "assistant" and m.get("content") == "hello world"
                   for m in r.projected_messages)
        # turn_id propagated for downstream session-DB linkage
        assert r.turn_id == "turn-fake-001"

    def test_unscoped_and_foreign_completions_cannot_finish_parent_turn(self):
        client = FakeClient()
        client.queue_notification(
            "turn/completed",
            turn={"id": "tu1", "status": "completed", "error": None},
        )
        client.queue_notification(
            "turn/completed",
            threadId="thread-child-001",
            turn={"id": "turn-child-001", "status": "completed", "error": None},
        )
        client.queue_notification(
            "item/completed",
            threadId="t",
            turnId="tu1",
            item={"type": "agentMessage", "id": "m-final", "text": "real final"},
        )
        client.queue_notification(
            "turn/completed",
            threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )

        result = make_session(client).run_turn("hi", turn_timeout=2.0)

        assert result.error is None
        assert result.final_text == "real final"
        # A permissive implementation would stop on the first unscoped event
        # and leave the real parent completion queued.
        assert not client._notifications

    @pytest.mark.parametrize(
        "status,expect_interrupted",
        [("interrupted", True), ("failed", False), (None, False)],
    )
    def test_non_completed_terminal_status_is_never_success(
        self, status, expect_interrupted
    ):
        client = FakeClient()
        client.queue_notification(
            "item/completed",
            threadId="t",
            turnId="tu1",
            item={"type": "agentMessage", "id": "m1", "text": "interim text"},
        )
        client.queue_notification(
            "turn/completed",
            threadId="t",
            turn={"id": "tu1", "status": status, "error": None},
        )

        result = make_session(client).run_turn("hi", turn_timeout=2.0)

        assert result.final_text == "interim text"
        assert result.error and "turn ended status=" in result.error
        assert result.interrupted is expect_interrupted
        assert result.timed_out is False

    def test_initial_user_projection_is_explicitly_marked(self):
        client = FakeClient()
        client.queue_notification(
            "item/completed",
            threadId="t",
            turnId="tu1",
            item={
                "type": "userMessage",
                "id": "u-initial",
                "content": [{"type": "text", "text": "same text"}],
            },
        )
        client.queue_notification(
            "turn/completed",
            threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )

        result = make_session(client).run_turn("same text", turn_timeout=2.0)

        assert result.projected_messages == [
            {
                "role": "user",
                "content": "same text",
                _INITIAL_USER_ECHO_MARKER: True,
            }
        ]

    def test_same_text_steer_after_model_output_is_not_marked_as_echo(self):
        class SteeringClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.session: Optional[CodexAppServerSession] = None
                self.notification_count = 0

            def take_notification(self, timeout: float = 0.0):
                self.notification_count += 1
                if self.notification_count == 2:
                    assert self.session is not None
                    assert self.session.request_steer("same text") is True
                return super().take_notification(timeout)

        client = SteeringClient()
        client.queue_notification(
            "item/completed",
            threadId="t",
            turnId="tu1",
            item={"type": "agentMessage", "id": "m1", "text": "working"},
        )
        client.queue_notification(
            "item/completed",
            threadId="t",
            turnId="tu1",
            item={
                "type": "userMessage",
                "id": "u-steer",
                "content": [{"type": "text", "text": "same text"}],
            },
        )
        client.queue_notification(
            "turn/completed",
            threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )

        session = make_session(client)
        client.session = session
        result = session.run_turn("same text", turn_timeout=2.0)

        steer = next(
            message
            for message in result.projected_messages
            if message.get("role") == "user"
        )
        assert _INITIAL_USER_ECHO_MARKER not in steer

    def test_immediate_same_text_steer_without_initial_echo_is_preserved(self):
        class SteeringClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.session: Optional[CodexAppServerSession] = None
                self.steered = False

            def take_notification(self, timeout: float = 0.0):
                if not self.steered:
                    self.steered = True
                    assert self.session is not None
                    assert self.session.request_steer("same text") is True
                return super().take_notification(timeout)

        client = SteeringClient()
        client.queue_notification(
            "item/completed",
            threadId="t",
            turnId="tu1",
            item={
                "type": "userMessage",
                "id": "u-immediate-steer",
                "content": [{"type": "text", "text": "same text"}],
            },
        )
        client.queue_notification(
            "turn/completed",
            threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )
        session = make_session(client)
        client.session = session

        result = session.run_turn("same text", turn_timeout=2.0)

        assert result.projected_messages == [
            {"role": "user", "content": "same text"}
        ]

    def test_initial_echo_is_suppressed_when_same_text_steer_also_exists(self):
        class SteeringClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.session: Optional[CodexAppServerSession] = None
                self.steered = False

            def take_notification(self, timeout: float = 0.0):
                if not self.steered:
                    self.steered = True
                    assert self.session is not None
                    assert self.session.request_steer("same text") is True
                return super().take_notification(timeout)

        client = SteeringClient()
        for item_id in ("u-initial", "u-steer"):
            client.queue_notification(
                "item/completed",
                threadId="t",
                turnId="tu1",
                item={
                    "type": "userMessage",
                    "id": item_id,
                    "content": [{"type": "text", "text": "same text"}],
                },
            )
        client.queue_notification(
            "turn/completed",
            threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )
        session = make_session(client)
        client.session = session

        result = session.run_turn("same text", turn_timeout=2.0)

        assert result.projected_messages[0][_INITIAL_USER_ECHO_MARKER] is True
        assert _INITIAL_USER_ECHO_MARKER not in result.projected_messages[1]

    def test_rejected_same_text_steer_after_completion_does_not_hide_echo(self):
        class RaceClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.notification_count = 0
                self.ready_for_steer = threading.Event()
                self.steer_started = threading.Event()
                self.release_steer = threading.Event()
                self.completion_returned = threading.Event()

            def request(self, method, params=None, timeout=30.0):
                if method == "turn/steer":
                    self.steer_started.set()
                    assert self.release_steer.wait(timeout=1.0)
                    return {"turnId": "different-turn"}
                return super().request(method, params, timeout)

            def take_notification(self, timeout: float = 0.0):
                self.notification_count += 1
                if self.notification_count == 2:
                    self.ready_for_steer.set()
                    assert self.steer_started.wait(timeout=1.0)
                    self.completion_returned.set()
                return super().take_notification(timeout)

        client = RaceClient()
        client.queue_notification(
            "item/completed",
            threadId="t",
            turnId="tu1",
            item={
                "type": "userMessage",
                "id": "u-initial",
                "content": [{"type": "text", "text": "same text"}],
            },
        )
        client.queue_notification(
            "turn/completed",
            threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )
        session = make_session(client)
        turn_result = {}
        steer_result = {}

        turn_thread = threading.Thread(
            target=lambda: turn_result.setdefault(
                "value", session.run_turn("same text", turn_timeout=2.0)
            )
        )
        turn_thread.start()
        assert client.ready_for_steer.wait(timeout=1.0)
        steer_thread = threading.Thread(
            target=lambda: steer_result.setdefault(
                "value", session.request_steer("same text")
            )
        )
        steer_thread.start()
        assert client.completion_returned.wait(timeout=1.0)
        client.release_steer.set()
        steer_thread.join(timeout=1.0)
        turn_thread.join(timeout=1.0)

        assert not steer_thread.is_alive()
        assert not turn_thread.is_alive()
        assert steer_result["value"] is False
        result = turn_result["value"]
        assert result.projected_messages[0][_INITIAL_USER_ECHO_MARKER] is True

    def test_poisoned_steer_transport_retires_completed_session(self):
        class PoisonedSteerClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.session: Optional[CodexAppServerSession] = None
                self.poisoned = False
                self.steered = False

            def is_alive(self):
                return not self.poisoned and not self._closed

            def request(self, method, params=None, timeout=30.0):
                if method == "turn/steer":
                    self.poisoned = True
                    raise TimeoutError("stdin write timed out")
                return super().request(method, params, timeout)

            def take_notification(self, timeout: float = 0.0):
                if not self.steered:
                    self.steered = True
                    assert self.session is not None
                    assert self.session.request_steer("change") is False
                return super().take_notification(timeout)

        client = PoisonedSteerClient()
        client.queue_notification(
            "turn/completed",
            threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )
        session = make_session(client)
        client.session = session

        result = session.run_turn("initial", turn_timeout=2.0)

        assert result.error is None
        assert result.should_retire is True
        with pytest.raises(RuntimeError, match="closed or poisoned"):
            session.ensure_started()

    def test_poison_error_keeps_same_text_steer_indeterminate(self):
        class PoisonWakeClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.session: Optional[CodexAppServerSession] = None
                self.poisoned = False
                self.injected = False

            def is_alive(self):
                return not self.poisoned and not self._closed

            def request(self, method, params=None, timeout=30.0):
                if method == "turn/steer":
                    self.poisoned = True
                    raise CodexAppServerError(
                        code=-32098,
                        message="transport closed after indeterminate stdin write",
                    )
                return super().request(method, params, timeout)

            def take_notification(self, timeout: float = 0.0):
                if not self.injected:
                    self.injected = True
                    assert self.session is not None
                    assert self.session.request_steer("same text") is False
                return super().take_notification(timeout)

        client = PoisonWakeClient()
        client.queue_notification(
            "item/completed",
            threadId="t",
            turnId="tu1",
            item={
                "type": "userMessage",
                "id": "u-accepted-before-poison",
                "content": [{"type": "text", "text": "same text"}],
            },
        )
        client.queue_notification(
            "turn/completed",
            threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )
        session = make_session(client)
        client.session = session

        result = session.run_turn("same text", turn_timeout=2.0)

        assert result.should_retire is True
        assert _INITIAL_USER_ECHO_MARKER not in result.projected_messages[0]



    def test_foreign_completion_in_server_request_drain_is_ignored(self):
        """Approval draining must not project a child result into the parent."""
        client = FakeClient()
        client.queue_server_request(
            "item/commandExecution/requestApproval",
            request_id="approval-1",
            command="pwd",
            cwd="/tmp",
        )
        client.queue_notification(
            "item/completed",
            threadId="thread-child-001",
            turnId="turn-child-001",
            item={
                "type": "agentMessage",
                "id": "child-message",
                "text": "child drain summary",
            },
        )
        client.queue_notification(
            "turn/completed",
            threadId="thread-child-001",
            turn={
                "id": "turn-child-001",
                "status": "completed",
                "error": None,
            },
        )

        original_respond = client.respond

        def respond_and_release_parent(request_id, result, *, timeout=5.0):
            original_respond(request_id, result, timeout=timeout)
            client.queue_notification(
                "item/completed",
                threadId="thread-fake-001",
                turnId="turn-fake-001",
                item={
                    "type": "agentMessage",
                    "id": "parent-message",
                    "text": "parent after approval",
                },
            )
            client.queue_notification(
                "turn/completed",
                threadId="thread-fake-001",
                turn={
                    "id": "turn-fake-001",
                    "status": "completed",
                    "error": None,
                },
            )

        client.respond = respond_and_release_parent
        session = make_session(
            client,
            request_routing=_ServerRequestRouting(auto_approve_exec=True),
        )

        result = session.run_turn("delegate then continue", turn_timeout=2.0)

        assert client.responses == [("approval-1", {"decision": "accept"})]
        assert result.final_text == "parent after approval"
        assert result.projected_messages == [
            {"role": "assistant", "content": "parent after approval"}
        ]



    def test_tool_iteration_counter_ticks(self):
        client = FakeClient()
        # Two completed exec items + one final agent message
        for i, item_id in enumerate(("ex1", "ex2"), start=1):
            client.queue_notification(
                "item/completed",
                item={
                    "type": "commandExecution", "id": item_id,
                    "command": f"cmd{i}", "cwd": "/tmp",
                    "status": "completed", "aggregatedOutput": "ok",
                    "exitCode": 0, "commandActions": [],
                },
                threadId="t", turnId="tu1",
            )
        client.queue_notification(
            "item/completed",
            item={"type": "agentMessage", "id": "m1", "text": "done"},
            threadId="t", turnId="tu1",
        )
        client.queue_notification(
            "turn/completed", threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )
        s = make_session(client)
        r = s.run_turn("do stuff", turn_timeout=2.0)
        assert r.tool_iterations == 2
        # Each tool item produces (assistant, tool) — 2*2 + final assistant = 5 msgs
        assert len(r.projected_messages) == 5


    def test_turn_start_failure_attaches_redacted_stderr_tail(self):
        """When codex stderr has content (non-OAuth), the tail gets attached
        to the user-facing error so config/provider problems are debuggable
        instead of just 'Internal error'. Credential-shaped values in stderr
        are redacted via agent.redact(force=True); web-URL query params pass
        through (see fix(redact): pass web URLs through unchanged)."""
        client = FakeClient()
        client.set_stderr_tail([
            "ERROR: provider auth failed",
            "Authorization: Bearer sk-live-deadbeefdeadbeef",
            "url=https://api.example.com/v1?token=querysecret12345",
        ])
        from agent.transports.codex_app_server import CodexAppServerError

        def boom(method, params):
            if method == "turn/start":
                raise CodexAppServerError(code=-32603, message="Internal error")
            return {"thread": {"id": "t"}, "activePermissionProfile": {"id": "x"}}

        client._request_handler = boom
        s = make_session(client)
        r = s.run_turn("hi", turn_timeout=2.0)
        assert r.error is not None
        assert "turn/start failed" in r.error
        assert "Internal error" in r.error
        # Stderr tail attached
        assert "codex stderr" in r.error
        assert "provider auth failed" in r.error
        # Credential-shaped values still redacted (sk- prefix + Bearer header)
        assert "sk-live-deadbeefdeadbeef" not in r.error
        # Non-OAuth → should NOT retire (subprocess JSON-RPC is still healthy).
        assert r.should_retire is False

    def test_turn_start_timeout_attaches_redacted_stderr_tail(self):
        """A non-OAuth TimeoutError on turn/start surfaces with codex stderr
        context attached and marks the session for retirement."""
        client = FakeClient()
        client.set_stderr_tail([
            "WARN: provider request stalled",
            "Authorization: Bearer sk-stalled-secret-abc123",
        ])

        def stall(method, params):
            if method == "turn/start":
                raise TimeoutError("codex method 'turn/start' timed out after 10s")
            return {"thread": {"id": "t"}, "activePermissionProfile": {"id": "x"}}

        client._request_handler = stall
        s = make_session(client)
        r = s.run_turn("hi", turn_timeout=2.0)
        assert r.error is not None
        assert "turn/start timed out" in r.error
        assert "provider request stalled" in r.error
        assert "sk-stalled-secret-abc123" not in r.error
        assert r.should_retire is True




    def test_steer_appends_input_to_active_turn(self):
        client = FakeClient()
        s = make_session(client)
        s.ensure_started()
        with s._active_turn_lock:
            s._active_turn_id = "turn-live-123"
            s._active_turn_deadline = time.monotonic() + 30.0
            s._active_turn_generation = 1

        assert s.request_steer("Use Postgres instead") is True
        method, params = client.requests[-1]
        assert method == "turn/steer"
        assert params == {
            "threadId": "thread-fake-001",
            "input": [{"type": "text", "text": "Use Postgres instead"}],
            "expectedTurnId": "turn-live-123",
        }

    def test_steer_rejected_after_active_turn_deadline(self):
        client = FakeClient()
        s = make_session(client)
        s.ensure_started()
        with s._active_turn_lock:
            s._active_turn_id = "turn-live-123"
            s._active_turn_deadline = time.monotonic() - 0.01

        assert s.request_steer("too late") is False
        assert all(method != "turn/steer" for method, _ in client.requests)

    def test_steer_timeout_is_bounded_by_active_turn_deadline(self):
        class TimeoutCaptureClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.steer_timeout: Optional[float] = None

            def request(self, method, params=None, timeout=30.0):
                if method == "turn/steer":
                    self.steer_timeout = timeout
                return super().request(method, params, timeout)

        client = TimeoutCaptureClient()
        s = make_session(client)
        s.ensure_started()
        with s._active_turn_lock:
            s._active_turn_id = "turn-live-123"
            s._active_turn_deadline = time.monotonic() + 0.2
            s._active_turn_generation = 1

        assert s.request_steer("bounded") is True
        assert client.steer_timeout is not None
        assert 0 < client.steer_timeout <= 0.2

    def test_late_steer_response_cannot_mutate_next_turn_reservation(self):
        class LateResponseClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.started = threading.Event()
                self.release = threading.Event()

            def request(self, method, params=None, timeout=30.0):
                if method == "turn/steer":
                    self.started.set()
                    assert self.release.wait(timeout=1.0)
                    return {"turnId": "different-turn"}
                return super().request(method, params, timeout)

        client = LateResponseClient()
        session = make_session(client)
        session.ensure_started()
        with session._active_turn_condition:
            session._active_turn_id = "turn-a"
            session._active_turn_deadline = time.monotonic() + 1.0
            session._active_turn_generation = 1

        outcome = {}
        worker = threading.Thread(
            target=lambda: outcome.setdefault(
                "value", session.request_steer("same text")
            )
        )
        worker.start()
        assert client.started.wait(timeout=1.0)

        with session._active_turn_condition:
            session._steer_reservations = {
                token: reservation
                for token, reservation in session._steer_reservations.items()
                if reservation.get("generation") != 1
            }
            session._active_turn_id = "turn-b"
            session._active_turn_deadline = time.monotonic() + 1.0
            session._active_turn_generation = 2
            session._next_steer_token += 1
            next_token = session._next_steer_token
            session._steer_reservations[next_token] = {
                "generation": 2,
                "text": "same text",
                "status": "confirmed",
                "expires_at": time.monotonic() + 1.0,
            }

        client.release.set()
        worker.join(timeout=1.0)

        assert not worker.is_alive()
        assert outcome["value"] is False
        assert session._steer_reservations[next_token]["status"] == "confirmed"
        assert session._steer_reservations[next_token]["generation"] == 2







class TestCompactThread:
    def test_compact_thread_sends_rpc_and_waits_for_completion(self):
        client = FakeClient()
        client.queue_notification(
            "turn/started",
            threadId="thread-fake-001",
            turn={"id": "compact-turn-1"},
        )
        client.queue_notification(
            "item/completed",
            threadId="thread-fake-001",
            turnId="compact-turn-1",
            item={"type": "contextCompaction", "id": "compact-item-1"},
        )
        client.queue_notification(
            "item/completed",
            threadId="thread-fake-001",
            turnId="compact-turn-1",
            item={"type": "agentMessage", "id": "m1", "text": "compacted"},
        )
        client.queue_notification(
            "thread/tokenUsage/updated",
            threadId="thread-fake-001",
            turnId="compact-turn-1",
            tokenUsage={
                "last": {"inputTokens": 10, "outputTokens": 2, "totalTokens": 12},
                "total": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
                "modelContextWindow": 200000,
            },
        )
        client.queue_notification(
            "turn/completed",
            threadId="thread-fake-001",
            turn={"id": "compact-turn-1", "status": "completed", "error": None},
        )

        r = make_session(client).compact_thread(turn_timeout=2.0)

        assert ("thread/compact/start", {"threadId": "thread-fake-001"}) in client.requests
        assert r.error is None
        assert r.thread_id == "thread-fake-001"
        assert r.turn_id == "compact-turn-1"
        assert r.compacted is True
        assert r.final_text == "compacted"
        assert r.token_usage_last["totalTokens"] == 12
        assert r.model_context_window == 200000

    def test_compact_thread_ignores_foreign_child_completion(self):
        client = FakeClient()
        client.queue_notification(
            "turn/started",
            threadId="thread-child-001",
            turn={"id": "child-compact-turn"},
        )
        client.queue_notification(
            "item/completed",
            threadId="thread-child-001",
            turnId="child-compact-turn",
            item={
                "type": "agentMessage",
                "id": "child-compact-message",
                "text": "child compact summary",
            },
        )
        client.queue_notification(
            "turn/completed",
            threadId="thread-child-001",
            turn={
                "id": "child-compact-turn",
                "status": "completed",
                "error": None,
            },
        )
        client.queue_notification(
            "turn/started",
            threadId="thread-fake-001",
            turn={"id": "compact-turn-1"},
        )
        client.queue_notification(
            "item/completed",
            threadId="thread-fake-001",
            turnId="compact-turn-1",
            item={
                "type": "agentMessage",
                "id": "parent-compact-message",
                "text": "parent compacted",
            },
        )
        client.queue_notification(
            "turn/completed",
            threadId="thread-fake-001",
            turn={
                "id": "compact-turn-1",
                "status": "completed",
                "error": None,
            },
        )

        result = make_session(client).compact_thread(turn_timeout=2.0)

        assert result.error is None
        assert result.turn_id == "compact-turn-1"
        assert result.final_text == "parent compacted"
        assert result.projected_messages == [
            {"role": "assistant", "content": "parent compacted"}
        ]





# ---- approval bridge ----

class TestServerRequestRouting:



    def test_unknown_server_request_replied_with_error(self):
        client = FakeClient()
        client.queue_server_request("totally/unknown", request_id="req-3")
        client.queue_notification(
            "turn/completed", threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )
        s = make_session(client)
        s.run_turn("hi", turn_timeout=1.0)
        assert any(
            rid == "req-3" and code == -32601
            for (rid, code, _msg) in client.error_responses
        )

    def test_on_event_fires_during_approval_drain(self):
        """When a server-initiated approval request arrives, the session
        drains up to 8 pending notifications first so per-turn state
        (e.g. _pending_file_changes for fileChange approvals) is current.
        Those drained notifications must also reach the on_event display
        hook — otherwise tool bubbles around approvals silently disappear.

        Regression for the issue where item/started events that landed
        in the queue alongside (or just before) an approval request got
        projected into messages but never displayed.
        """
        client = FakeClient()
        # An item/started notification is queued first, then a server
        # request — the session sees both during a single drain loop.
        client.queue_notification(
            "item/started",
            item={
                "type": "commandExecution",
                "id": "exec-1",
                "command": "echo drained",
                "cwd": "/tmp",
            },
        )
        client.queue_server_request(
            "item/commandExecution/requestApproval", request_id="req-d",
            command="echo drained",
            cwd="/tmp",
        )
        client.queue_notification(
            "turn/completed", threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )

        events: list[dict] = []

        def cb(command, description, *, allow_permanent=True):
            return "once"

        s = make_session(
            client,
            approval_callback=cb,
            on_event=events.append,
        )
        s.run_turn("hi", turn_timeout=1.0)

        # The on_event hook must have seen the item/started even though
        # it was drained as part of the approval roundtrip — not just
        # events that arrive on the main notification path.
        item_started_events = [
            e for e in events
            if e.get("method") == "item/started"
        ]
        assert item_started_events, (
            "item/started drained alongside the approval was not "
            "forwarded to on_event — display will miss tool bubbles "
            "around approvals"
        )



    def test_routing_auto_approve_bypass(self):
        client = FakeClient()
        client.queue_server_request("item/commandExecution/requestApproval", request_id="r1",
                                    command="ls", cwd="/")
        client.queue_notification(
            "turn/completed", threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )
        # No callback, but routing says auto-approve. Should approve.
        s = make_session(client, request_routing=_ServerRequestRouting(
            auto_approve_exec=True))
        s.run_turn("hi", turn_timeout=1.0)
        assert ("r1", {"decision": "accept"}) in client.responses



# ---- enriched approval prompts ----

class TestApprovalPromptEnrichment:
    """Quirk #4: apply_patch prompt should show what's changing.
    Quirk #10: exec prompt should never show empty cwd."""

    def test_exec_falls_back_to_session_cwd(self):
        """When codex omits cwd from the approval params, the prompt shows
        the session cwd, not an empty string."""
        client = FakeClient()
        client.queue_server_request(
            "item/commandExecution/requestApproval", request_id="r1",
            command="ls",  # no cwd
        )
        client.queue_notification(
            "turn/completed", threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )
        captured = {}
        def cb(command, description, *, allow_permanent=True):
            captured["description"] = description
            return "once"
        s = make_session(client, approval_callback=cb)
        s.run_turn("hi", turn_timeout=1.0)
        # Session cwd is /tmp by default in make_session()
        assert "/tmp" in captured["description"]
        assert "Codex requests exec in <unknown>" not in captured["description"]

    def test_apply_patch_prompt_summarizes_pending_changes(self):
        """When the projector has cached the fileChange item from item/started,
        the approval prompt surfaces the change summary."""
        client = FakeClient()
        # item/started fires first (carries the changes), then approval request
        client.queue_notification(
            "item/started",
            item={"type": "fileChange", "id": "fc-1",
                  "changes": [
                      {"kind": {"type": "add"}, "path": "/tmp/new.py"},
                      {"kind": {"type": "update"}, "path": "/tmp/old.py"},
                  ]},
            threadId="t", turnId="tu1",
        )
        client.queue_server_request(
            "item/fileChange/requestApproval", request_id="req-2",
            itemId="fc-1", turnId="tu1", threadId="t",
            startedAtMs=1234567890,
            reason="add and update files",
        )
        client.queue_notification(
            "turn/completed", threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )
        captured = {}
        def cb(command, description, *, allow_permanent=True):
            captured["command"] = command
            captured["description"] = description
            return "once"
        s = make_session(client, approval_callback=cb)
        s.run_turn("hi", turn_timeout=1.0)
        # Both add and update kinds should be in the summary
        assert "1 add" in captured["command"] or "1 add" in captured["description"]
        assert "1 update" in captured["command"] or "1 update" in captured["description"]
        # And at least one of the paths
        joined = captured["command"] + " " + captured["description"]
        assert "/tmp/new.py" in joined or "/tmp/old.py" in joined

    def test_apply_patch_prompt_works_without_cached_summary(self):
        """When approval arrives before item/started (or without changes
        info), prompt falls back to whatever codex provided."""
        client = FakeClient()
        client.queue_server_request(
            "item/fileChange/requestApproval", request_id="req-2",
            itemId="fc-orphan", turnId="tu1", threadId="t",
            startedAtMs=1234567890,
            reason="apply some changes",
        )
        client.queue_notification(
            "turn/completed", threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )
        captured = {}
        def cb(command, description, *, allow_permanent=True):
            captured["command"] = command
            return "once"
        s = make_session(client, approval_callback=cb)
        s.run_turn("hi", turn_timeout=1.0)
        # Falls back to the reason
        assert "apply some changes" in captured["command"]


# ---- openclaw beta.8 parity: retire/wedge/oauth/abort marker ----

class TestSessionRetirement:
    """Mirrors openclaw beta.8's resilience fixes:
      - retire timed-out app-server clients (should_retire on deadline)
      - post-tool completion watchdog (don't burn the full deadline after a
        tool result if codex goes silent)
      - <turn_aborted> raw marker as terminal (don't wait for turn/completed
        that never comes)
      - OAuth refresh failure classification (suggest `codex login` instead
        of raw RPC error strings)
      - dead subprocess detection between iterations
    """



    def test_final_agent_message_without_turn_completed_times_out(self):
        """An agentMessage alone is intermediate until turn/completed arrives."""
        client = FakeClient()
        client.queue_notification(
            "item/completed",
            item={"type": "agentMessage", "id": "m1", "text": "done"},
            threadId="t",
            turnId="tu1",
        )
        s = make_session(client)
        r = s.run_turn(
            "hi",
            turn_timeout=0.05,
            notification_poll_timeout=0.01,
        )
        assert r.final_text == "done"
        assert r.interrupted is True
        assert r.timed_out is True
        assert r.error and "turn timed out after 0.05s" in r.error
        assert r.should_retire is True
        assert any(
            msg["role"] == "assistant" and msg.get("content") == "done"
            for msg in r.projected_messages
        )
        # Once the absolute deadline has expired, do not spend another fixed
        # timeout trying to write turn/interrupt; retiring the process is the
        # fail-closed cleanup path.
        assert not any(
            method == "turn/interrupt" for method, _ in client.requests
        )


    def test_post_tool_watchdog_uses_monotonic_clock(self):
        client = FakeClient()
        client.queue_notification(
            "item/completed",
            item={
                "type": "commandExecution", "id": "ex1",
                "command": "echo hi", "cwd": "/tmp",
                "status": "completed", "aggregatedOutput": "hi",
                "exitCode": 0, "commandActions": [],
            },
            threadId="t", turnId="tu1",
        )
        s = make_session(client)
        monotonic_values = iter([1000.0] + [999.0] * 8 + [1000.2])
        last_monotonic = [1000.2]

        def fake_monotonic():
            try:
                last_monotonic[0] = next(monotonic_values)
            except StopIteration:
                pass
            return last_monotonic[0]

        with patch.object(
            session_mod.time,
            "monotonic",
            side_effect=fake_monotonic,
        ):
            r = s.run_turn(
                "tool then silence",
                turn_timeout=5.0,
                notification_poll_timeout=0.0,
                post_tool_quiet_timeout=0.15,
            )
        assert r.interrupted is True
        assert r.should_retire is True
        assert r.error and "silent" in r.error

    def test_post_tool_watchdog_resets_on_further_activity(self):
        """A tool completion followed by an agent message should NOT trip
        the watchdog — further activity = codex still alive."""
        client = FakeClient()
        client.queue_notification(
            "item/completed",
            item={
                "type": "commandExecution", "id": "ex1",
                "command": "echo hi", "cwd": "/tmp",
                "status": "completed", "aggregatedOutput": "hi",
                "exitCode": 0, "commandActions": [],
            },
            threadId="t", turnId="tu1",
        )
        # Non-tool activity immediately after — resets watchdog.
        client.queue_notification(
            "item/completed",
            item={"type": "agentMessage", "id": "m1", "text": "tool finished"},
            threadId="t", turnId="tu1",
        )
        client.queue_notification(
            "turn/completed", threadId="t",
            turn={"id": "tu1", "status": "completed", "error": None},
        )
        s = make_session(client)
        r = s.run_turn(
            "tool then talk", turn_timeout=2.0,
            notification_poll_timeout=0.01,
            post_tool_quiet_timeout=0.05,
        )
        # Tool ran, then text reset the watchdog, then turn/completed.
        # Should NOT be a retirement case.
        assert r.tool_iterations == 1
        assert r.final_text == "tool finished"
        assert r.should_retire is False
        assert r.interrupted is False







    def test_dead_subprocess_detected_between_iterations(self):
        """If codex dies (segfault, OOM, killed by its auth refresh
        thread), the inter-iteration is_alive check breaks the loop
        instead of waiting on a queue that will never fill."""
        client = FakeClient()
        s = make_session(client)
        s.ensure_started()
        # Simulate subprocess death by setting _closed (FakeClient's
        # is_alive returns False when closed).
        client._closed = True
        client.set_stderr_tail([
            "thread 'tokio-runtime-worker' panicked at 'oauth: invalid_grant'",
        ])
        r = s.run_turn("x", turn_timeout=2.0,
                       notification_poll_timeout=0.01)
        assert r.should_retire is True
        # Stderr-derived auth hint takes precedence over generic message
        assert r.error and "codex login" in r.error


# ---- thread/start cross-fill ----

class TestThreadStartCrossFill:
    """Mirrors openclaw beta.8's tolerance for thread.id/sessionId aliasing."""

    def test_thread_id_under_thread_key(self):
        client = FakeClient()
        s = make_session(client)
        tid = s.ensure_started()
        assert tid == "thread-fake-001"



    def test_missing_thread_id_raises(self):
        from agent.transports.codex_app_server import CodexAppServerError

        client = FakeClient()
        client._request_handler = lambda method, params: (
            {"thread": {}, "activePermissionProfile": {"id": "x"}}
            if method == "thread/start" else
            {"turn": {"id": "tu1"}}
        )
        s = make_session(client)
        with pytest.raises(CodexAppServerError, match="no thread id"):
            s.ensure_started()


class TestHasTurnAbortedMarker:
    """Unit coverage for the marker matcher itself."""

    def test_empty_string(self):
        from agent.transports.codex_app_server_session import (
            _has_turn_aborted_marker,
        )
        assert _has_turn_aborted_marker("") is False
        assert _has_turn_aborted_marker(None) is False  # type: ignore[arg-type]

    def test_plain_text_no_marker(self):
        from agent.transports.codex_app_server_session import (
            _has_turn_aborted_marker,
        )
        assert _has_turn_aborted_marker("normal response with no markers") is False

    def test_open_marker(self):
        from agent.transports.codex_app_server_session import (
            _has_turn_aborted_marker,
        )
        assert _has_turn_aborted_marker("blah <turn_aborted> blah") is True



class TestClassifyOAuthFailure:
    """Unit coverage for the OAuth classifier; conservative on purpose."""



    def test_401_classified(self):
        from agent.transports.codex_app_server_session import (
            _classify_oauth_failure,
        )
        hint = _classify_oauth_failure("HTTP 401 Unauthorized")
        assert hint is not None


    def test_empty_inputs(self):
        from agent.transports.codex_app_server_session import (
            _classify_oauth_failure,
        )
        assert _classify_oauth_failure() is None
        assert _classify_oauth_failure("") is None
        assert _classify_oauth_failure("", None) is None  # type: ignore[arg-type]


class TestAbsoluteTurnDeadline:
    def test_startup_and_turn_start_share_one_deadline(self):
        class SlowClient(FakeClient):
            @staticmethod
            def _wait(delay: float, timeout: float) -> None:
                wait_for = min(delay, timeout)
                time.sleep(wait_for)
                if timeout < delay:
                    raise TimeoutError("request budget expired")

            def initialize(self, **kwargs):
                self._wait(0.02, float(kwargs.get("timeout", 10.0)))
                return super().initialize(**kwargs)

            def request(
                self,
                method: str,
                params: Optional[dict] = None,
                timeout: float = 30.0,
            ):
                if method in {"thread/start", "turn/start"}:
                    self._wait(0.02 if method == "thread/start" else 0.03, timeout)
                return super().request(method, params, timeout)

        client = SlowClient()
        session = make_session(client)
        started = time.monotonic()
        result = session.run_turn("x", turn_timeout=0.05)
        elapsed = time.monotonic() - started

        assert elapsed < 0.10
        assert result.timed_out is True
        assert result.interrupted is True
        assert result.should_retire is True
        assert result.error and "turn/start timed out before" in result.error

    def test_blocking_approval_is_declined_at_turn_deadline(self):
        client = FakeClient()
        client.queue_server_request(
            "item/commandExecution/requestApproval",
            request_id="approval-timeout",
            command="pwd",
            cwd="/tmp",
        )

        def blocking_callback(*_args, **_kwargs):
            time.sleep(0.30)
            return "once"

        session = make_session(client, approval_callback=blocking_callback)
        started = time.monotonic()
        result = session.run_turn(
            "x",
            turn_timeout=0.05,
            notification_poll_timeout=0.01,
        )
        elapsed = time.monotonic() - started

        assert elapsed < 0.15
        assert result.timed_out is True
        assert result.should_retire is True
        # The callback consumed the whole turn budget. Do not exceed the
        # deadline by attempting a best-effort response write; the session is
        # retired instead.
        assert client.responses == []

    def test_notification_poll_is_capped_by_remaining_turn_budget(self):
        class PollClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.poll_timeouts = []

            def take_notification(self, timeout: float = 0.0):
                self.poll_timeouts.append(timeout)
                time.sleep(timeout)
                return None

        client = PollClient()
        session = make_session(client)
        started = time.monotonic()
        result = session.run_turn(
            "x", turn_timeout=0.04, notification_poll_timeout=5.0
        )
        elapsed = time.monotonic() - started

        assert elapsed < 0.12
        assert result.timed_out is True
        assert result.should_retire is True
        assert client.poll_timeouts
        assert max(client.poll_timeouts) <= 0.04

    def test_blocking_approval_response_is_bounded_by_turn_deadline(self):
        class BlockingResponseClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.response_timeout = None

            def respond(self, request_id, result, *, timeout=5.0):
                self.response_timeout = timeout
                time.sleep(timeout)
                raise TimeoutError("response write blocked")

        client = BlockingResponseClient()
        client.queue_server_request(
            "item/commandExecution/requestApproval",
            request_id="blocked-response",
            command="pwd",
            cwd="/tmp",
        )
        session = make_session(
            client,
            request_routing=_ServerRequestRouting(auto_approve_exec=True),
        )
        started = time.monotonic()
        result = session.run_turn(
            "x", turn_timeout=0.05, notification_poll_timeout=0.01
        )
        elapsed = time.monotonic() - started

        assert elapsed < 0.15
        assert result.timed_out is True
        assert result.interrupted is True
        assert result.should_retire is True
        assert result.error and "approval response failed" in result.error
        assert client.response_timeout is not None
        assert client.response_timeout <= 0.05
