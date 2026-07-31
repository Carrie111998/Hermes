"""Tests for /v1/runs endpoints: start, status, events, and stop.

Covers:
- POST /v1/runs — start a run (202)
- GET /v1/runs/{run_id} — poll run status
- GET /v1/runs/{run_id}/events — SSE event stream
- POST /v1/runs/{run_id}/stop — interrupt a running agent
- Auth, error handling, and cleanup
"""

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    _approval_event_choices,
    cors_middleware,
    security_headers_middleware,
)
from tools import approval as approval_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("smart_denied", "allow_permanent", "allow_session", "expected"),
    [
        (False, True, True, ["once", "session", "always", "deny"]),
        (False, False, True, ["once", "session", "deny"]),
        (False, False, False, ["once", "deny"]),
        (True, True, True, ["once", "deny"]),
        (True, False, False, ["once", "deny"]),
    ],
)
def test_approval_event_choices_follow_backend_capabilities(
    smart_denied, allow_permanent, allow_session, expected
):
    assert _approval_event_choices(
        smart_denied=smart_denied,
        allow_permanent=allow_permanent,
        allow_session=allow_session,
    ) == expected


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    """Create an adapter with optional API key."""
    extra = {}
    if api_key:
        extra["key"] = api_key
    config = PlatformConfig(enabled=True, extra=extra)
    adapter = APIServerAdapter(config)
    return adapter


def _create_runs_app(adapter: APIServerAdapter) -> web.Application:
    """Create an aiohttp app with /v1/runs routes registered."""
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
    app.router.add_post("/v1/runs/{run_id}/approval", adapter._handle_run_approval)
    app.router.add_post("/v1/runs/{run_id}/stop", adapter._handle_stop_run)
    return app


def _make_slow_agent(**kwargs):
    """Create a mock agent that blocks in run_conversation until interrupted.

    Returns (mock_agent, agent_ready_event, interrupt_event) where
    agent_ready_event is set once run_conversation starts, and
    interrupt_event is set when interrupt() is called.
    """
    ready = threading.Event()
    interrupted = threading.Event()

    mock_agent = MagicMock()

    def _do_interrupt(message=None):
        interrupted.set()

    mock_agent.interrupt = MagicMock(side_effect=_do_interrupt)

    def _slow_run(user_message=None, conversation_history=None, task_id=None):
        ready.set()
        # Block until interrupt() is called
        interrupted.wait(timeout=10)
        return {"final_response": "interrupted"}

    mock_agent.run_conversation.side_effect = _slow_run
    mock_agent.session_prompt_tokens = 0
    mock_agent.session_completion_tokens = 0
    mock_agent.session_total_tokens = 0

    return mock_agent, ready, interrupted


@pytest.fixture
def adapter():
    return _make_adapter()


@pytest.fixture
def auth_adapter():
    return _make_adapter(api_key="sk-secret")


# ---------------------------------------------------------------------------
# POST /v1/runs — start a run
# ---------------------------------------------------------------------------


class TestStartRun:
    @pytest.mark.asyncio
    async def test_start_returns_202(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 10
                mock_agent.session_completion_tokens = 5
                mock_agent.session_total_tokens = 15
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                assert data["status"] == "started"
                assert data["run_id"].startswith("run_")

                status_resp = await cli.get(f"/v1/runs/{data['run_id']}")
                assert status_resp.status == 200
                status = await status_resp.json()
                assert status["run_id"] == data["run_id"]
                assert status["status"] in {"queued", "running", "completed"}
                assert status["object"] == "hermes.run"

    @pytest.mark.asyncio
    async def test_start_binds_chat_id_for_delegation_wake_target(self, adapter):
        """/v1/runs must bind the raw session id as the api_server chat_id
        (like every other agent-entry route does via _run_agent): the async
        delegation dispatch reads HERMES_SESSION_CHAT_ID to pick its wake
        self-post target, and an empty binding forces background delegations
        on this route back to synchronous execution."""
        app = _create_runs_app(adapter)
        captured = {}

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()

                def _capture_run(user_message=None, conversation_history=None, task_id=None):
                    from tools.async_delegation import _current_origin_session_id

                    captured["origin_session_id"] = _current_origin_session_id()
                    return {"final_response": "done"}

                mock_agent.run_conversation.side_effect = _capture_run
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hello", "session_id": "runs-raw-sid"},
                )
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                for _ in range(40):
                    status_resp = await cli.get(f"/v1/runs/{run_id}")
                    status = await status_resp.json()
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

        assert captured.get("origin_session_id") == "runs-raw-sid", (
            "runs route must bind chat_id so delegation dispatch sees a wake target"
        )


    @pytest.mark.asyncio
    async def test_start_rejects_conflicting_route_and_request_provider(self):
        adapter = APIServerAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "model_routes": {
                        "alias": {
                            "model": "route/model",
                            "provider": "openrouter",
                        }
                    }
                },
            )
        )
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                resp = await cli.post(
                    "/v1/runs",
                    json={
                        "input": "hello",
                        "model": "alias",
                        "provider": "minimax",
                    },
                )
                data = await resp.json()

        assert resp.status == 400
        assert "provider" in data["error"]["message"].lower()
        assert adapter._run_streams == {}
        assert adapter._run_statuses == {}
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_passes_request_model_provider_options_to_create_agent(self, adapter):
        app = _create_runs_app(adapter)
        model_options = {"reasoning_effort": "medium", "service_tier": "priority"}
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post(
                    "/v1/runs",
                    json={
                        "input": "hello",
                        "model": "MiniMax-M3",
                        "provider": "minimax",
                        "model_options": model_options,
                    },
                )
                assert resp.status == 202
                for _ in range(20):
                    if mock_create.call_args is not None:
                        break
                    await asyncio.sleep(0.05)

        kwargs = mock_create.call_args.kwargs
        assert kwargs["requested_model"] == "MiniMax-M3"
        assert kwargs["requested_provider"] == "minimax"
        assert kwargs["model_options"] == model_options


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id} — poll run status
# ---------------------------------------------------------------------------


class TestRunStatus:

    @pytest.mark.asyncio
    async def test_status_reflects_explicit_session_id(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "hello", "session_id": "space-session"},
                )
                data = await resp.json()
                run_id = data["run_id"]

                for _ in range(20):
                    status_resp = await cli.get(f"/v1/runs/{run_id}")
                    status = await status_resp.json()
                    if status["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

                mock_agent.run_conversation.assert_called_once()
                assert mock_agent.run_conversation.call_args.kwargs["task_id"] == "space-session"
                assert status["session_id"] == "space-session"


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id}/events — SSE event stream
# ---------------------------------------------------------------------------


class TestRunEvents:
    @pytest.mark.asyncio
    async def test_events_stream_returns_completed(self, adapter):
        """Events stream should receive run.completed when agent finishes."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "Hello!"}
                mock_agent.session_prompt_tokens = 10
                mock_agent.session_completion_tokens = 5
                mock_agent.session_total_tokens = 15
                mock_create.return_value = mock_agent

                # Start run
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                # Subscribe to events
                events_resp = await cli.get(f"/v1/runs/{run_id}/events")
                assert events_resp.status == 200
                body = await events_resp.text()

                # Should contain run.completed
                assert "run.completed" in body
                assert "Hello!" in body


    @pytest.mark.asyncio
    async def test_approval_is_scoped_to_target_run(self, auth_adapter):
        """Same client session_id must not let one run approve another run's queue."""
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(auth_adapter, "_create_agent") as mock_create:
                victim_agent, victim_ready, victim_interrupted = _make_slow_agent()
                attacker_agent, attacker_ready, attacker_interrupted = _make_slow_agent()
                mock_create.side_effect = [victim_agent, attacker_agent]

                victim_resp = await cli.post(
                    "/v1/runs",
                    json={"input": "victim", "session_id": "shared-project"},
                    headers={"Authorization": "Bearer sk-secret"},
                )
                attacker_resp = await cli.post(
                    "/v1/runs",
                    json={"input": "attacker", "session_id": "shared-project"},
                    headers={"Authorization": "Bearer sk-secret"},
                )
                assert victim_resp.status == 202
                assert attacker_resp.status == 202
                victim_run = (await victim_resp.json())["run_id"]
                attacker_run = (await attacker_resp.json())["run_id"]

                victim_ready.wait(timeout=3.0)
                attacker_ready.wait(timeout=3.0)
                assert auth_adapter._run_approval_sessions[victim_run] == victim_run
                assert auth_adapter._run_approval_sessions[attacker_run] == attacker_run
                assert auth_adapter._run_approval_sessions[victim_run] != auth_adapter._run_approval_sessions[attacker_run]

                victim_entry = approval_mod._ApprovalEntry({
                    "command": "bash -c victim-danger",
                    "description": "victim approval",
                    "pattern_keys": ["shell-c"],
                })
                attacker_entry = approval_mod._ApprovalEntry({
                    "command": "bash -c attacker-danger",
                    "description": "attacker approval",
                    "pattern_keys": ["shell-c"],
                })
                with approval_mod._lock:
                    approval_mod._gateway_queues[victim_run] = [victim_entry]
                    approval_mod._gateway_queues[attacker_run] = [attacker_entry]

                approval_resp = await cli.post(
                    f"/v1/runs/{attacker_run}/approval",
                    json={
                        "approval_id": attacker_entry.approval_id,
                        "choice": "always",
                    },
                    headers={"Authorization": "Bearer sk-secret"},
                )
                approval_data = await approval_resp.json()

                assert approval_resp.status == 200
                assert approval_data["approval_id"] == attacker_entry.approval_id
                assert approval_data["resolved"] == 1
                assert attacker_entry.result == "always"
                assert attacker_entry.event.is_set()
                assert victim_entry.result is None
                assert not victim_entry.event.is_set()
                with approval_mod._lock:
                    assert approval_mod._gateway_queues[victim_run] == [victim_entry]
                    assert victim_run in approval_mod._gateway_queues
                    assert attacker_run not in approval_mod._gateway_queues

                # Clean up the synthetic pending victim approval and unblock the
                # slow test agents so their background run tasks can finish.
                with approval_mod._lock:
                    approval_mod._gateway_queues.pop(victim_run, None)
                victim_interrupted.set()
                attacker_interrupted.set()

    @pytest.mark.asyncio
    async def test_delayed_approval_response_cannot_resolve_a_newer_request(self, adapter):
        """A stale response must not approve the next request in the same run."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, interrupted = _make_slow_agent()
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert start_resp.status == 202
                run_id = (await start_resp.json())["run_id"]
                assert agent_ready.wait(timeout=3.0)

                expired = approval_mod._ApprovalEntry({
                    "approval_id": "approval-expired",
                    "command": "first command",
                })
                current = approval_mod._ApprovalEntry({
                    "approval_id": "approval-current",
                    "command": "second command",
                })
                with approval_mod._lock:
                    approval_mod._gateway_queues[run_id] = [current]

                approval_resp = await cli.post(
                    f"/v1/runs/{run_id}/approval",
                    json={"approval_id": expired.approval_id, "choice": "once"},
                )
                approval_data = await approval_resp.json()

                assert approval_resp.status == 409
                assert approval_data["error"]["code"] == "approval_not_pending"
                assert current.result is None
                assert not current.event.is_set()
                with approval_mod._lock:
                    assert approval_mod._gateway_queues[run_id] == [current]

                interrupted.set()

    @pytest.mark.asyncio
    async def test_resolving_one_approval_keeps_next_pollable_without_reemitting_it(
        self, adapter
    ):
        """A queued request stays pollable without a duplicate SSE notification."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, interrupted = _make_slow_agent()
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await start_resp.json())["run_id"]
                assert agent_ready.wait(timeout=3.0)

                first = approval_mod._ApprovalEntry({
                    "description": "first command",
                    "smart_denied": False,
                    "allow_permanent": False,
                })
                second = approval_mod._ApprovalEntry({
                    "description": "second command",
                    "smart_denied": False,
                    "allow_permanent": False,
                })
                with approval_mod._lock:
                    approval_mod._gateway_queues[run_id] = [first, second]
                adapter._set_run_status(
                    run_id,
                    "waiting_for_approval",
                    pending_approval={
                        "approval_id": first.approval_id,
                        "description": "first command",
                        "choices": ["once", "deny"],
                    },
                )

                approval_resp = await cli.post(
                    f"/v1/runs/{run_id}/approval",
                    json={"approval_id": first.approval_id, "choice": "deny"},
                )
                assert approval_resp.status == 200

                responded = adapter._run_streams[run_id].get_nowait()
                assert responded["event"] == "approval.responded"
                assert responded["approval_id"] == first.approval_id
                assert adapter._run_streams[run_id].empty()

                status = await (await cli.get(f"/v1/runs/{run_id}")).json()
                assert status["status"] == "waiting_for_approval"
                assert status["pending_approval"] == {
                    "approval_id": second.approval_id,
                    "description": "second command",
                    "choices": ["once", "session", "deny"],
                }
                assert second.result is None
                assert not second.event.is_set()

                interrupted.set()

    @pytest.mark.asyncio
    async def test_approval_request_id_is_pollable_and_echoed_on_response(self, adapter):
        """Clients can recover and settle the exact pending request by ID."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                notified = threading.Event()

                def _run_with_approval(*_args, **_kwargs):
                    approval_session_key = approval_mod.get_current_session_key()

                    def _notify_seen(_approval_data):
                        notified.set()

                    # The API callback remains registered; this wrapper only
                    # records when the approval reaches that public boundary.
                    with approval_mod._lock:
                        api_notify = approval_mod._gateway_notify_cbs[
                            approval_session_key
                        ]

                    def _recording_notify(approval_data):
                        api_notify(approval_data)
                        _notify_seen(approval_data)

                    with approval_mod._lock:
                        approval_mod._gateway_notify_cbs[
                            approval_session_key
                        ] = _recording_notify
                    return approval_mod._run_approval_gate(
                        pattern_key="shell-c",
                        description="Run a command?",
                        display_target="bash -c true",
                        cron_deny_message="blocked",
                        autoapprove_log_prefix="test",
                    )

                mock_agent = MagicMock()
                mock_agent.run_conversation.side_effect = _run_with_approval
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await start_resp.json())["run_id"]
                assert notified.wait(timeout=3.0)

                status_resp = await cli.get(f"/v1/runs/{run_id}")
                status = await status_resp.json()
                pending = status["pending_approval"]

                assert status["status"] == "waiting_for_approval"
                assert pending["approval_id"].startswith("approval_")
                assert pending["description"] == "Run a command?"
                assert pending["command"] == "bash -c true"
                assert pending["choices"] == ["once", "session", "always", "deny"]

                approval_resp = await cli.post(
                    f"/v1/runs/{run_id}/approval",
                    json={"approval_id": pending["approval_id"], "choice": "deny"},
                )
                approval_data = await approval_resp.json()

                assert approval_resp.status == 200
                assert approval_data["approval_id"] == pending["approval_id"]

                for _ in range(40):
                    settled = await (await cli.get(f"/v1/runs/{run_id}")).json()
                    if settled["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)
                assert "pending_approval" not in settled

    @pytest.mark.asyncio
    async def test_all_concurrent_approval_requests_are_pollable(self, adapter):
        """Polling must not hide a queued approval behind the newest request."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, interrupted = _make_slow_agent()
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await start_resp.json())["run_id"]
                assert agent_ready.wait(timeout=3.0)

                first = approval_mod._ApprovalEntry({
                    "command": "first command",
                    "description": "Run the first command?",
                    "allow_permanent": False,
                    "allow_session": False,
                })
                second = approval_mod._ApprovalEntry({
                    "command": "second command",
                    "description": "Run the second command?",
                    "allow_permanent": False,
                    "allow_session": False,
                })
                with approval_mod._lock:
                    approval_mod._gateway_queues[run_id] = [first, second]

                status_resp = await cli.get(f"/v1/runs/{run_id}")
                status = await status_resp.json()

                assert status["status"] == "waiting_for_approval"
                assert status["pending_approvals"] == [
                    {
                        "approval_id": first.approval_id,
                        "description": "Run the first command?",
                        "command": "first command",
                        "choices": ["once", "deny"],
                    },
                    {
                        "approval_id": second.approval_id,
                        "description": "Run the second command?",
                        "command": "second command",
                        "choices": ["once", "deny"],
                    },
                ]
                assert status["pending_approval"] == status["pending_approvals"][0]

                interrupted.set()

    @pytest.mark.asyncio
    async def test_expired_approval_returns_to_running_status(self, adapter, monkeypatch):
        """A timed-out queue entry must not leave reconnecting clients waiting."""
        app = _create_runs_app(adapter)
        timed_out = threading.Event()
        release_agent = threading.Event()
        monkeypatch.setattr(approval_mod, "_get_approval_timeout", lambda: 0)

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                def _run_with_timeout(*_args, **_kwargs):
                    result = approval_mod._run_approval_gate(
                        pattern_key="shell-c",
                        description="Run a command?",
                        display_target="bash -c true",
                        cron_deny_message="blocked",
                        autoapprove_log_prefix="test",
                    )
                    assert result["approved"] is False
                    timed_out.set()
                    release_agent.wait(timeout=3.0)
                    return {"final_response": "done"}

                mock_agent = MagicMock()
                mock_agent.run_conversation.side_effect = _run_with_timeout
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await start_resp.json())["run_id"]
                assert timed_out.wait(timeout=3.0)

                status = await (await cli.get(f"/v1/runs/{run_id}")).json()

                assert status["status"] == "running"
                assert "pending_approval" not in status
                assert "pending_approvals" not in status
                release_agent.set()

    @pytest.mark.asyncio
    async def test_approval_response_requires_an_approval_id(self, adapter):
        """The runs API never falls back to resolving a request by position."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, interrupted = _make_slow_agent()
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await start_resp.json())["run_id"]
                assert agent_ready.wait(timeout=3.0)

                pending = approval_mod._ApprovalEntry({"command": "dangerous"})
                with approval_mod._lock:
                    approval_mod._gateway_queues[run_id] = [pending]

                approval_resp = await cli.post(
                    f"/v1/runs/{run_id}/approval",
                    json={"choice": "once"},
                )
                approval_data = await approval_resp.json()

                assert approval_resp.status == 400
                assert approval_data["error"]["code"] == "missing_approval_id"
                assert pending.result is None
                assert not pending.event.is_set()

                interrupted.set()

    @pytest.mark.asyncio
    async def test_approval_response_rejects_choice_not_offered_for_request(
        self, adapter
    ):
        """A response cannot grant broader scope than the queued request offers."""
        app = _create_runs_app(adapter)
        run_id = "run-limited-approval"
        pending = approval_mod._ApprovalEntry(
            {
                "command": "dangerous",
                "allow_session": False,
                "allow_permanent": False,
            }
        )
        adapter._run_approval_sessions[run_id] = run_id
        adapter._set_run_status(run_id, "waiting_for_approval")
        with approval_mod._lock:
            approval_mod._gateway_queues[run_id] = [pending]

        try:
            async with TestClient(TestServer(app)) as cli:
                approval_resp = await cli.post(
                    f"/v1/runs/{run_id}/approval",
                    json={"approval_id": pending.approval_id, "choice": "always"},
                )
                approval_data = await approval_resp.json()

            assert approval_resp.status == 400
            assert approval_data["error"]["code"] == "invalid_approval_choice"
            assert pending.result is None
            assert not pending.event.is_set()
            with approval_mod._lock:
                assert approval_mod._gateway_queues[run_id] == [pending]
        finally:
            with approval_mod._lock:
                approval_mod._gateway_queues.pop(run_id, None)
            adapter._run_approval_sessions.pop(run_id, None)
            adapter._run_statuses.pop(run_id, None)


# ---------------------------------------------------------------------------
# Run lifecycle TTL sweeping
# ---------------------------------------------------------------------------


class TestRunLifecycleSweep:

    @pytest.mark.asyncio
    async def test_expired_live_run_drops_transport_but_keeps_control_state(self, adapter):
        """Stream TTL bounds buffering without detaching a live run."""
        app = _create_runs_app(adapter)
        adapter._max_concurrent_runs = 1

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, _ = _make_slow_agent()
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert start_resp.status == 202
                run_id = (await start_resp.json())["run_id"]
                assert agent_ready.wait(timeout=3.0)

                task = adapter._active_run_tasks[run_id]
                assert isinstance(task, asyncio.Task)
                assert not task.done()

                pending = approval_mod._ApprovalEntry({
                    "command": "bash -c long-running",
                    "description": "approval after stream TTL",
                    "pattern_keys": ["shell-c"],
                })
                with approval_mod._lock:
                    approval_mod._gateway_queues[run_id] = [pending]

                adapter._run_streams_created[run_id] -= adapter._RUN_STREAM_TTL + 1
                # Exercise one real sweeper iteration without waiting 60 seconds.
                with patch(
                    "gateway.platforms.api_server.asyncio.sleep",
                    side_effect=[None, asyncio.CancelledError()],
                ):
                    with pytest.raises(asyncio.CancelledError):
                        await adapter._sweep_orphaned_runs()

                assert adapter._active_run_tasks[run_id] is task
                assert adapter._active_run_agents[run_id] is mock_agent
                assert run_id not in adapter._run_streams
                assert run_id not in adapter._run_streams_created
                assert adapter._run_approval_sessions[run_id] == run_id

                limited = adapter._concurrency_limited_response()
                assert limited is not None
                assert limited.status == 429

                approval_resp = await cli.post(
                    f"/v1/runs/{run_id}/approval",
                    json={"approval_id": pending.approval_id, "choice": "once"},
                )
                assert approval_resp.status == 200
                assert (await approval_resp.json())["approval_id"] == pending.approval_id
                assert pending.event.is_set()
                assert pending.result == "once"

                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200
                mock_agent.interrupt.assert_called_once_with("Stop requested via API")


# ---------------------------------------------------------------------------
# POST /v1/runs/{run_id}/stop — interrupt a running agent
# ---------------------------------------------------------------------------


class TestStopRun:

    @pytest.mark.asyncio
    async def test_stop_rejects_a_later_approval_response(self, adapter):
        """Once Stop wins, a delayed approval cannot release gated work."""
        app = _create_runs_app(adapter)
        run_id = "run-stop-approval"
        pending = approval_mod._ApprovalEntry({"command": "dangerous"})
        agent = MagicMock()
        adapter._active_run_agents[run_id] = agent
        adapter._run_approval_sessions[run_id] = run_id
        adapter._set_run_status(run_id, "waiting_for_approval")
        with approval_mod._lock:
            approval_mod._gateway_queues[run_id] = [pending]

        try:
            async with TestClient(TestServer(app)) as cli:
                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                approval_resp = await cli.post(
                    f"/v1/runs/{run_id}/approval",
                    json={"approval_id": pending.approval_id, "choice": "once"},
                )
                approval_data = await approval_resp.json()

            assert stop_resp.status == 200
            assert approval_resp.status == 409
            assert approval_data["error"]["code"] == "approval_not_active"
            assert pending.result is None
            assert not pending.event.is_set()
            agent.interrupt.assert_called_once_with("Stop requested via API")
        finally:
            with approval_mod._lock:
                approval_mod._gateway_queues.pop(run_id, None)
            adapter._active_run_agents.pop(run_id, None)
            adapter._run_approval_sessions.pop(run_id, None)
            adapter._run_statuses.pop(run_id, None)
            adapter._stopping_run_ids.discard(run_id)

    @pytest.mark.asyncio
    async def test_stop_keeps_uncooperative_executor_tracked_until_exit(self, adapter):
        """Cancelling an asyncio wrapper must not hide its live executor thread."""
        app = _create_runs_app(adapter)
        run_can_finish = threading.Event()
        run_finished = threading.Event()

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                started = threading.Event()

                def _run_conversation(*_args, **_kwargs):
                    started.set()
                    run_can_finish.wait(timeout=5)
                    run_finished.set()
                    return {"final_response": "late result"}

                mock_agent.run_conversation.side_effect = _run_conversation
                mock_create.return_value = mock_agent

                resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await resp.json())["run_id"]
                assert started.wait(timeout=3)

                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200
                await asyncio.sleep(0.1)

                assert not run_finished.is_set()
                assert run_id in adapter._active_run_agents
                assert run_id in adapter._active_run_tasks
                assert adapter._run_statuses[run_id]["status"] == "stopping"

                run_can_finish.set()
                for _ in range(40):
                    if run_id not in adapter._active_run_tasks:
                        break
                    await asyncio.sleep(0.05)

                assert run_id not in adapter._active_run_agents
                assert run_id not in adapter._active_run_tasks
                assert adapter._run_statuses[run_id]["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_stop_running_agent(self, adapter):
        """Stop should interrupt the agent and cancel the task."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, _ = _make_slow_agent()
                mock_create.return_value = mock_agent

                # Start run
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                # Wait for agent to start running in the thread
                agent_ready.wait(timeout=3.0)
                await asyncio.sleep(0.1)

                # Verify agent ref is stored
                assert run_id in adapter._active_run_agents

                # Stop the run
                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200
                stop_data = await stop_resp.json()
                assert stop_data["run_id"] == run_id
                assert stop_data["status"] == "stopping"

                # Agent interrupt should have been called
                mock_agent.interrupt.assert_called_once_with("Stop requested via API")

                status_resp = await cli.get(f"/v1/runs/{run_id}")
                assert status_resp.status == 200
                status_data = await status_resp.json()
                assert status_data["status"] in {"stopping", "cancelled"}

                # Refs should be cleaned up
                await asyncio.sleep(0.2)
                assert run_id not in adapter._active_run_agents
                assert run_id not in adapter._active_run_tasks


    @pytest.mark.asyncio
    async def test_stop_sends_sentinel_to_events_stream(self, adapter):
        """After stop, the events stream should close."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, agent_ready, _ = _make_slow_agent()
                mock_create.return_value = mock_agent

                # Start run
                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                agent_ready.wait(timeout=3.0)
                await asyncio.sleep(0.1)

                # Subscribe to events in background
                events_task = asyncio.ensure_future(
                    cli.get(f"/v1/runs/{run_id}/events")
                )

                await asyncio.sleep(0.1)

                # Stop the run
                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200

                # Events stream should close
                events_resp = await asyncio.wait_for(events_task, timeout=5.0)
                assert events_resp.status == 200
                body = await events_resp.text()
                # Stream should have received run.failed and closed
                assert "run.failed" in body or "stream closed" in body


class TestRunsProviderAuthFailure:
    @pytest.mark.asyncio
    async def test_status_reports_provider_auth_failure_distinctly(self, adapter):
        """/v1/runs builds its own agent via _create_agent() and does not
        route through _run_agent(), so the controlled "Provider
        authentication failed" message added there does not cover this
        endpoint. _handle_runs()'s own _ProviderAuthResolutionError branch
        must give the same distinguished message instead of the generic
        except-Exception "run failed" text."""
        from gateway.platforms.api_server import _ProviderAuthResolutionError

        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_create.side_effect = _ProviderAuthResolutionError(
                    "No credentials found for provider 'nous'"
                )

                resp = await cli.post("/v1/runs", json={"input": "hello"})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                for _ in range(40):
                    status_resp = await cli.get(f"/v1/runs/{run_id}")
                    status = await status_resp.json()
                    if status["status"] == "failed":
                        break
                    await asyncio.sleep(0.05)

                assert status["status"] == "failed"
                assert status["error"] == "⚠️ Provider authentication failed: No credentials found for provider 'nous'"
                assert status["last_event"] == "run.failed"
