"""Tests for /v1/runs endpoints: start, status, events, steer, and stop.

Covers:
- POST /v1/runs — start a run (202)
- GET /v1/runs/{run_id} — poll run status
- GET /v1/runs/{run_id}/events — SSE event stream
- POST /v1/runs/{run_id}/steer — inject guidance into a running agent
- POST /v1/runs/{run_id}/stop — interrupt a running agent
- Auth, error handling, and cleanup
"""

import asyncio
import json
import logging
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
    ("smart_denied", "allow_session", "allow_permanent", "expected"),
    [
        (False, True, True, ["once", "session", "always", "deny"]),
        (False, True, False, ["once", "session", "deny"]),
        (False, False, True, ["once", "deny"]),
        (False, False, False, ["once", "deny"]),
        (True, True, True, ["once", "deny"]),
        (True, False, False, ["once", "deny"]),
    ],
)
def test_approval_event_choices_follow_backend_capabilities(
    smart_denied, allow_session, allow_permanent, expected
):
    assert (
        _approval_event_choices(
            smart_denied=smart_denied,
            allow_session=allow_session,
            allow_permanent=allow_permanent,
        )
        == expected
    )


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
    app.router.add_post("/v1/runs/{run_id}/steer", adapter._handle_steer_run)
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
    async def test_approval_resolve_all_is_scoped_to_target_run(self, auth_adapter):
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
                    json={"choice": "always", "resolve_all": True},
                    headers={"Authorization": "Bearer sk-secret"},
                )
                approval_data = await approval_resp.json()

                assert approval_resp.status == 200
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


# ---------------------------------------------------------------------------
# POST /v1/runs/{run_id}/steer — steer a running agent
# ---------------------------------------------------------------------------


class TestSteerRun:
    @pytest.mark.asyncio
    async def test_steer_running_agent(self, adapter):
        app = _create_runs_app(adapter)
        agent = MagicMock()
        agent.steer.return_value = True
        queue = asyncio.Queue()
        adapter._active_run_agents["run_123"] = agent
        adapter._run_streams["run_123"] = queue
        adapter._set_run_status("run_123", "running")

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_123/steer", json={"input": "tighten the ending"})
            payload = await resp.json()

        assert resp.status == 200
        assert payload == {
            "object": "hermes.run.steer",
            "run_id": "run_123",
            "accepted": True,
        }
        agent.steer.assert_called_once_with("tighten the ending")
        assert adapter._run_statuses["run_123"]["last_event"] == "run.steered"
        event = queue.get_nowait()
        assert event["event"] == "run.steered"
        assert event["run_id"] == "run_123"
        assert event["accepted"] is True

    @pytest.mark.asyncio
    async def test_steer_nonexistent_run_returns_404(self, adapter):
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_missing/steer", json={"input": "hello"})
            payload = await resp.json()

        assert resp.status == 404
        assert payload["error"]["code"] == "run_not_found"

    @pytest.mark.asyncio
    async def test_steer_inactive_run_returns_409(self, adapter):
        app = _create_runs_app(adapter)
        adapter._set_run_status("run_done", "completed")

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_done/steer", json={"input": "hello"})
            payload = await resp.json()

        assert resp.status == 409
        assert payload["error"]["code"] == "run_not_accepting_steer"

    @pytest.mark.asyncio
    async def test_steer_missing_input_returns_400(self, adapter):
        app = _create_runs_app(adapter)
        agent = MagicMock()
        agent.steer.return_value = True
        adapter._active_run_agents["run_123"] = agent
        adapter._set_run_status("run_123", "running")

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_123/steer", json={"input": ""})
            payload = await resp.json()

        assert resp.status == 400
        assert payload["error"]["code"] == "invalid_steer_input"
        agent.steer.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_then_steer_rejects_retained_agent_ref(self, adapter):
        """Steer must reject a stopping run even if the executor thread is still live."""
        app = _create_runs_app(adapter)
        run_can_finish = threading.Event()
        run_started = threading.Event()

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_agent.steer = MagicMock(return_value=True)

                def _interrupt(_message=None):
                    return None

                def _run_conversation(*_args, **_kwargs):
                    run_started.set()
                    run_can_finish.wait(timeout=5)
                    return {"final_response": "late result"}

                mock_agent.interrupt = MagicMock(side_effect=_interrupt)
                mock_agent.run_conversation.side_effect = _run_conversation
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await start_resp.json())["run_id"]
                assert run_started.wait(timeout=3.0)

                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200
                assert run_id in adapter._active_run_agents

                steer_resp = await cli.post(
                    f"/v1/runs/{run_id}/steer",
                    json={"input": "tighten the ending"},
                )
                steer_data = await steer_resp.json()

                assert steer_resp.status == 409
                assert steer_data["error"]["code"] == "run_not_accepting_steer"
                mock_agent.steer.assert_not_called()

                run_can_finish.set()
                for _ in range(40):
                    if run_id not in adapter._active_run_tasks:
                        break
                    await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_pending_steer_preserved_on_run_completed(self, adapter):
        """A steer drained by the turn finalizer (accepted after the final
        response) must surface as pending_steer on the terminal run status
        instead of being silently dropped."""
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_agent.run_conversation.return_value = {
                    "final_response": "done",
                    "pending_steer": "tighten the ending",
                }
                mock_create.return_value = mock_agent

                start_resp = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await start_resp.json())["run_id"]

                for _ in range(40):
                    status = adapter._run_statuses.get(run_id, {})
                    if status.get("status") == "completed":
                        break
                    await asyncio.sleep(0.05)

        assert adapter._run_statuses[run_id]["status"] == "completed"
        assert adapter._run_statuses[run_id]["pending_steer"] == "tighten the ending"

    @pytest.mark.asyncio
    async def test_steer_requires_auth(self, auth_adapter):
        app = _create_runs_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/runs/run_any/steer", json={"input": "hello"})

        assert resp.status == 401


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
                    json={"choice": "once"},
                )
                assert approval_resp.status == 200
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


# ---------------------------------------------------------------------------
# Cross-profile run isolation (#93689)
# ---------------------------------------------------------------------------

class TestRunProfileScoping:
    """Run-scoped routes authenticate the caller but must also scope to it.

    Run state is keyed by run_id alone. Under ``gateway.multiplex_profiles``
    every served profile holds a valid API key, so ``_check_auth`` returning
    None proved only "some valid key" — any profile could then read another
    profile's run output and, worse, stop/steer/approve it, executing under
    the target's tool permissions rather than the caller's.
    """

    @staticmethod
    def _seed_run(adapter, run_id: str, owner: str, *, status: str = "running"):
        """Register a run as if *owner* had created it through the API."""
        from gateway.platforms.api_server import _api_request_profile

        token = _api_request_profile.set(owner)
        try:
            adapter._claim_run_owner(run_id, _api_request_profile.get())
            adapter._set_run_status(run_id, status, session_id="sess")
        finally:
            _api_request_profile.reset(token)

    @staticmethod
    async def _as_profile(adapter, profile, handler, run_id, **kwargs):
        """Invoke *handler* with the request-profile ContextVar set."""
        from gateway.platforms.api_server import _api_request_profile

        request = MagicMock()
        request.match_info = {"run_id": run_id}
        for key, value in kwargs.items():
            setattr(request, key, value)
        token = _api_request_profile.set(profile)
        try:
            return await handler(request)
        finally:
            _api_request_profile.reset(token)

    @pytest.fixture
    def adapter(self):
        adapter = _make_adapter()
        # Authentication is not the gap — every profile legitimately holds a
        # valid key. Pin it open so the test measures scoping alone.
        adapter._check_auth = lambda request: None
        return adapter

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caller", ["beta", "default", None])
    async def test_foreign_profile_cannot_read_a_run(self, adapter, caller):
        self._seed_run(adapter, "run_alpha", "alpha")
        response = await self._as_profile(
            adapter, caller, adapter._handle_get_run, "run_alpha"
        )
        assert response.status == 404

    @pytest.mark.asyncio
    async def test_owning_profile_can_read_its_own_run(self, adapter):
        self._seed_run(adapter, "run_alpha", "alpha")
        response = await self._as_profile(
            adapter, "alpha", adapter._handle_get_run, "run_alpha"
        )
        assert response.status == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caller", ["beta", "default"])
    async def test_foreign_profile_cannot_stop_a_run(self, adapter, caller):
        """The critical case: stopping executes against the target's agent."""
        self._seed_run(adapter, "run_alpha", "alpha")
        agent = MagicMock()
        adapter._active_run_agents["run_alpha"] = agent

        response = await self._as_profile(
            adapter, caller, adapter._handle_stop_run, "run_alpha"
        )
        assert response.status == 404
        # Not merely refused — the run must be untouched.
        assert "run_alpha" not in adapter._stopping_run_ids
        assert adapter._run_statuses["run_alpha"]["status"] == "running"

    @pytest.mark.asyncio
    async def test_owning_profile_can_stop_its_own_run(self, adapter):
        self._seed_run(adapter, "run_alpha", "alpha")
        adapter._active_run_agents["run_alpha"] = MagicMock()

        response = await self._as_profile(
            adapter, "alpha", adapter._handle_stop_run, "run_alpha"
        )
        assert response.status == 200
        assert "run_alpha" in adapter._stopping_run_ids

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler_name", [
        "_handle_get_run", "_handle_run_events",
        "_handle_run_approval", "_handle_steer_run", "_handle_stop_run",
    ])
    async def test_every_run_scoped_route_is_scoped(self, adapter, handler_name):
        """All five routes, not just the ones with an obvious blast radius."""
        self._seed_run(adapter, "run_alpha", "alpha")
        adapter._active_run_agents["run_alpha"] = MagicMock()
        adapter._run_streams["run_alpha"] = []

        response = await self._as_profile(
            adapter, "beta", getattr(adapter, handler_name), "run_alpha"
        )
        assert response.status == 404

    @pytest.mark.asyncio
    async def test_refusal_is_404_not_403(self, adapter):
        """403 would confirm the run exists. Run ids are the only thing
        keeping runs unenumerable — there is no collection route."""
        self._seed_run(adapter, "run_alpha", "alpha")

        foreign = await self._as_profile(
            adapter, "beta", adapter._handle_get_run, "run_alpha"
        )
        absent = await self._as_profile(
            adapter, "beta", adapter._handle_get_run, "run_does_not_exist"
        )
        assert foreign.status == absent.status == 404
        # Same error code too — only the echoed run id differs, which the
        # caller already supplied.
        assert (
            json.loads(foreign.body)["error"]["code"]
            == json.loads(absent.body)["error"]["code"]
            == "run_not_found"
        )

    @pytest.mark.asyncio
    async def test_unnamed_listener_and_explicit_default_are_one_profile(
        self, adapter
    ):
        """The default listener reaches handlers with the ContextVar unset;
        /p/default/ sets it to "default". They are the same owner."""
        self._seed_run(adapter, "run_d", None)
        assert adapter._run_owner_profiles["run_d"] == "default"

        for caller in (None, "default"):
            response = await self._as_profile(
                adapter, caller, adapter._handle_get_run, "run_d"
            )
            assert response.status == 200

    @pytest.mark.asyncio
    async def test_ownership_is_write_once(self, adapter):
        """A later status update — including one a foreign caller triggers —
        must not be able to re-stamp the owner."""
        self._seed_run(adapter, "run_alpha", "alpha")
        self._seed_run(adapter, "run_alpha", "beta", status="running")
        assert adapter._run_owner_profiles["run_alpha"] == "alpha"

    @pytest.mark.asyncio
    async def test_a_bare_status_write_still_gets_an_owner(self, adapter):
        """A run record cannot exist without an owner.

        This is the shape the vulnerability took: registering a run through
        the ordinary status write and then reading it from another profile.
        On the unfixed code the foreign read returns 200.
        """
        from gateway.platforms.api_server import _api_request_profile

        token = _api_request_profile.set("alpha")
        try:
            adapter._set_run_status("run_bare", "running", session_id="s")
        finally:
            _api_request_profile.reset(token)

        assert adapter._run_owner_profiles.get("run_bare") == "alpha"

        foreign = await self._as_profile(
            adapter, "beta", adapter._handle_get_run, "run_bare"
        )
        assert foreign.status == 404

        owner = await self._as_profile(
            adapter, "alpha", adapter._handle_get_run, "run_bare"
        )
        assert owner.status == 200

    @pytest.mark.asyncio
    async def test_a_transition_cannot_re_stamp_the_owner(self, adapter):
        """Executor threads run transitions with the ContextVar unset, and a
        foreign caller can trigger one. Neither may take ownership."""
        from gateway.platforms.api_server import _api_request_profile

        token = _api_request_profile.set("alpha")
        try:
            adapter._set_run_status("run_t", "queued", session_id="s")
        finally:
            _api_request_profile.reset(token)

        adapter._set_run_status("run_t", "running")          # no profile in scope
        token = _api_request_profile.set("beta")
        try:
            adapter._set_run_status("run_t", "completed")    # foreign profile
        finally:
            _api_request_profile.reset(token)

        assert adapter._run_owner_profiles["run_t"] == "alpha"

    def test_owner_is_reclaimed_with_the_rest_of_the_run_state(self, adapter):
        """The owner map must not outlive the run it describes."""
        # Status records are only reclaimed once terminal and past their TTL.
        self._seed_run(adapter, "run_alpha", "alpha", status="completed")
        adapter._run_streams_created["run_alpha"] = 0.0
        adapter._run_streams["run_alpha"] = []

        adapter._sweep_orphaned_runs_once(
            now=time.time() + adapter._RUN_STATUS_TTL + 1
        )

        assert "run_alpha" not in adapter._run_statuses
        assert "run_alpha" not in adapter._run_owner_profiles

    def test_owner_survives_a_sweep_while_the_run_is_still_controllable(
        self, adapter
    ):
        """Ownership must outlive every surface it protects.

        The status record and the agent ref retire on different clocks, so a
        terminal-but-stale status can be reclaimed while a live agent ref
        remains. /stop gates on the agent ref, not on the status — so dropping
        the owner here would hand a still-live run to any caller.
        """
        self._seed_run(adapter, "run_alpha", "alpha", status="cancelled")
        adapter._active_run_agents["run_alpha"] = MagicMock()

        adapter._sweep_orphaned_runs_once(
            now=time.time() + adapter._RUN_STATUS_TTL + 1
        )

        assert "run_alpha" not in adapter._run_statuses
        assert adapter._run_owner_profiles.get("run_alpha") == "alpha"

    def test_the_owner_goes_when_the_last_surface_does(self, adapter):
        """The other end of that rule: ownership must not be immortal either.

        If the sweeper already took the status while the run was live, the
        run-completion teardown drops the last surface — and nothing would
        revisit the id afterwards, so the entry has to be released there.
        """
        self._seed_run(adapter, "run_alpha", "alpha", status="cancelled")
        adapter._active_run_agents["run_alpha"] = MagicMock()
        adapter._sweep_orphaned_runs_once(
            now=time.time() + adapter._RUN_STATUS_TTL + 1
        )
        assert adapter._run_owner_profiles.get("run_alpha") == "alpha"

        adapter._active_run_agents.pop("run_alpha", None)   # teardown
        adapter._release_run_owner_if_forgotten("run_alpha")

        assert "run_alpha" not in adapter._run_owner_profiles

    def test_the_creation_site_claim_is_what_names_the_right_profile(
        self, adapter
    ):
        """Pins why the creation sites claim at all, rather than leaving it to
        _set_run_status.

        Ownership would exist either way — but a run hops to an executor
        thread the ContextVar does not follow, and its first status write can
        land there. Claimed only at that point, the run would be stamped
        "default" instead of its creator.
        """
        from gateway.platforms.api_server import _api_request_profile

        token = _api_request_profile.set("alpha")
        try:                                            # creation site, in scope
            adapter._claim_run_owner("run_x", _api_request_profile.get())
        finally:
            _api_request_profile.reset(token)

        adapter._set_run_status("run_x", "queued", session_id="s")   # executor

        assert adapter._run_owner_profiles["run_x"] == "alpha"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler_name", [
        "_handle_get_run", "_handle_run_events",
        "_handle_run_approval", "_handle_steer_run", "_handle_stop_run",
    ])
    @pytest.mark.parametrize("caller", ["beta", "alpha", "default", None])
    async def test_an_existing_run_without_an_owner_stamp_is_refused(
        self, adapter, handler_name, caller
    ):
        """Missing provenance on a real run is an unanswered authorization
        question, not permission.

        Serving it would turn the boundary allow-all for every served profile
        exactly when the metadata that decides it is absent — so it is refused
        for *every* caller, not merely a foreign one. Nothing on the ordinary
        creation paths loses the stamp today; this pins the invariant rather
        than a reachable path.
        """
        adapter._run_statuses["run_ghost"] = {"status": "running", "session_id": "s"}
        adapter._active_run_agents["run_ghost"] = MagicMock()
        adapter._run_streams["run_ghost"] = asyncio.Queue()
        assert "run_ghost" not in adapter._run_owner_profiles

        response = await self._as_profile(
            adapter, caller, getattr(adapter, handler_name), "run_ghost"
        )

        assert response.status == 404
        assert "run_ghost" not in adapter._stopping_run_ids
        assert adapter._run_statuses["run_ghost"]["status"] == "running"

    @pytest.mark.parametrize("surface", [
        "_run_statuses", "_active_run_agents", "_active_run_tasks",
        "_run_streams", "_run_approval_sessions",
    ])
    def test_any_surviving_surface_closes_an_unstamped_run(
        self, adapter, surface
    ):
        """Each surface independently, so the rule cannot be satisfied by the
        status dict alone while a live agent ref stays reachable."""
        getattr(adapter, surface)["run_ghost"] = MagicMock()

        assert adapter._run_visible_to_caller("run_ghost") is False

    def test_an_id_that_names_nothing_is_not_refused(self, adapter):
        """The complement, and the reason the rule is about state rather than
        about ownership alone: /events deliberately admits a caller in the
        moment before its run is registered."""
        assert adapter._run_state_exists("run_unknown") is False
        assert adapter._run_visible_to_caller("run_unknown") is True

    @pytest.mark.asyncio
    async def test_an_unknown_id_still_enters_the_events_wait(
        self, adapter, monkeypatch
    ):
        """Fail-closed must not collapse the subscribe-early window: an id
        with no state waits, it is not refused up front."""
        slept = []

        async def _fake_sleep(delay):
            slept.append(delay)

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

        response = await self._as_profile(
            adapter, "beta", adapter._handle_run_events, "run_unknown"
        )

        assert response.status == 404
        assert len(slept) > 0

    @pytest.mark.parametrize(
        "raw,expected",
        [(None, "default"), ("", "default"), ("   ", "default"),
         ("  coder  ", "coder"), ("default", "default")],
    )
    def test_profile_names_are_normalized(self, adapter, raw, expected):
        assert adapter._normalized_profile(raw) == expected

    @pytest.mark.asyncio
    async def test_the_run_body_never_names_the_owning_profile(self, adapter):
        """Ownership lives in a side table precisely so the response body does
        not disclose it to a caller with no other way to learn it."""
        # A profile name that is not a substring of the run id, so the
        # assertion cannot pass on an echoed id alone.
        self._seed_run(adapter, "run_1", "zephyr")

        response = await self._as_profile(
            adapter, "zephyr", adapter._handle_get_run, "run_1"
        )

        assert response.status == 200
        assert "zephyr" not in response.body.decode()

    @pytest.mark.asyncio
    async def test_events_cannot_be_timed_to_learn_that_a_run_exists(
        self, adapter, monkeypatch
    ):
        """A foreign run must be indistinguishable from one that never existed.

        Refusing before the subscribe-early wait returned in ~0.1ms against
        ~1s for an unknown id — an existence oracle as plain as the 403 this
        route deliberately avoids.
        """
        slept = []

        async def _fake_sleep(delay):
            slept.append(delay)

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

        self._seed_run(adapter, "run_alpha", "alpha")
        adapter._run_streams["run_alpha"] = asyncio.Queue()

        foreign = await self._as_profile(
            adapter, "beta", adapter._handle_run_events, "run_alpha"
        )
        foreign_waits = len(slept)
        slept.clear()
        absent = await self._as_profile(
            adapter, "beta", adapter._handle_run_events, "run_absent"
        )
        absent_waits = len(slept)

        assert foreign.status == absent.status == 404
        assert foreign_waits == absent_waits > 0
        assert (
            json.loads(foreign.body)["error"]["code"]
            == json.loads(absent.body)["error"]["code"]
        )

    @pytest.mark.asyncio
    async def test_a_cross_profile_attempt_is_logged(self, adapter, caplog):
        """Refusals leave the same operator-visible trace an auth failure does
        — a silent 404 gives an operator nothing to investigate."""
        self._seed_run(adapter, "run_alpha", "alpha")

        with caplog.at_level(logging.WARNING):
            await self._as_profile(
                adapter, "beta", adapter._handle_get_run, "run_alpha"
            )

        assert any(
            "run_alpha" in r.getMessage() and "another" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler_name", ["_handle_steer_run", "_handle_run_approval"])
    async def test_the_worst_case_routes_leave_the_run_untouched(
        self, adapter, handler_name
    ):
        """Refusing the response is not enough for the routes that execute
        against the target's agent — nothing may reach it."""
        self._seed_run(adapter, "run_alpha", "alpha")
        agent = MagicMock()
        adapter._active_run_agents["run_alpha"] = agent

        response = await self._as_profile(
            adapter, "beta", getattr(adapter, handler_name), "run_alpha"
        )

        assert response.status == 404
        agent.steer.assert_not_called()
        assert adapter._run_statuses["run_alpha"]["status"] == "running"
        assert "run_alpha" not in adapter._stopping_run_ids

    @pytest.mark.asyncio
    async def test_a_swept_status_does_not_expose_a_live_run_to_stop(
        self, adapter
    ):
        """The end the previous test guards: without an owner, /stop would
        reach the agent and the "stopping" write would re-stamp the run to
        the caller — write-once, so permanently."""
        self._seed_run(adapter, "run_alpha", "alpha", status="cancelled")
        adapter._active_run_agents["run_alpha"] = MagicMock()
        adapter._sweep_orphaned_runs_once(
            now=time.time() + adapter._RUN_STATUS_TTL + 1
        )

        response = await self._as_profile(
            adapter, "beta", adapter._handle_stop_run, "run_alpha"
        )

        assert response.status == 404
        assert "run_alpha" not in adapter._stopping_run_ids
        assert adapter._run_owner_profiles["run_alpha"] == "alpha"

    @pytest.mark.asyncio
    async def test_subscribing_before_a_run_exists_cannot_capture_it(
        self, adapter
    ):
        """/events is the one route that admits a caller before the run is
        registered. An unowned run is visible by design, so the entry check
        passes — the run must be re-checked once it appears."""

        async def _create_run_mid_wait():
            await asyncio.sleep(0.1)
            self._seed_run(adapter, "run_late", "alpha")
            adapter._run_streams["run_late"] = asyncio.Queue()

        creator = asyncio.create_task(_create_run_mid_wait())
        try:
            response = await self._as_profile(
                adapter, "beta", adapter._handle_run_events, "run_late"
            )
        finally:
            await creator

        assert response.status == 404
        assert "run_late" not in adapter._run_stream_subscribers
