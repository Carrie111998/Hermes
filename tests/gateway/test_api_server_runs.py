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
    _run_session_continuation_revision,
    cors_middleware,
    security_headers_middleware,
)
from hermes_state import SessionDB
from tools import approval as approval_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("smart_denied", "allow_permanent", "expected"),
    [
        (False, True, ["once", "session", "always", "deny"]),
        (False, False, ["once", "session", "deny"]),
        (True, True, ["once", "deny"]),
        (True, False, ["once", "deny"]),
    ],
)
def test_approval_event_choices_follow_backend_capabilities(
    smart_denied, allow_permanent, expected
):
    assert _approval_event_choices(
        smart_denied=smart_denied,
        allow_permanent=allow_permanent,
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


def _continuation_descriptor(db: SessionDB, session_id: str) -> dict:
    snapshot = db.get_continuation_snapshot(session_id)
    assert snapshot is not None
    resolved_id, history, message_ids = snapshot
    return {
        "version": 1,
        "session_id": resolved_id,
        "revision": _run_session_continuation_revision(
            resolved_id,
            history,
            message_ids,
        ),
    }


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


class TestExactSessionContinuation:
    @pytest.fixture(autouse=True)
    def _configured_key_for_continuation_logic(self, adapter):
        """Exercise continuation state logic with its required key configured.

        The HTTP bearer gate itself has dedicated coverage below; these tests
        focus on descriptor binding, races, and lifecycle behavior.
        """
        adapter._api_key = "sk-secret"
        adapter._check_auth = lambda _request: None

    @pytest.mark.asyncio
    async def test_verified_continuation_loads_history_into_stoppable_run(
        self,
        adapter,
        tmp_path,
    ):
        db = SessionDB(tmp_path / "continuation.db")
        adapter._session_db = db
        try:
            session_id = db.create_session("continued-session", "api_server")
            db.append_message(session_id, "user", "earlier question")
            db.append_message(session_id, "assistant", "earlier answer")
            descriptor = _continuation_descriptor(db, session_id)

            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "continued"}
            mock_agent.session_prompt_tokens = 3
            mock_agent.session_completion_tokens = 2
            mock_agent.session_total_tokens = 5

            app = _create_runs_app(adapter)
            with patch.object(adapter, "_create_agent", return_value=mock_agent):
                async with TestClient(TestServer(app)) as cli:
                    resp = await cli.post(
                        "/v1/runs",
                        json={"input": "next step", "continuation": descriptor},
                    )
                    assert resp.status == 202, await resp.text()
                    run_id = (await resp.json())["run_id"]

                    for _ in range(40):
                        status_resp = await cli.get(f"/v1/runs/{run_id}")
                        status = await status_resp.json()
                        if status["status"] == "completed":
                            break
                        await asyncio.sleep(0.025)

            assert status["status"] == "completed"
            assert status["session_id"] == session_id
            assert status["continuation_version"] == 1
            assert status["output"] == "continued"
            call = mock_agent.run_conversation.call_args.kwargs
            assert call["task_id"] == session_id
            assert call["user_message"] == "next step"
            assert [
                (message["role"], message["content"])
                for message in call["conversation_history"]
            ] == [
                ("user", "earlier question"),
                ("assistant", "earlier answer"),
            ]
            assert adapter._active_continuation_sessions == {}
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_continuation_submission_refuses_an_unkeyed_api_server(
        self, tmp_path
    ):
        adapter = _make_adapter()
        db = SessionDB(tmp_path / "unkeyed-continuation.db")
        adapter._session_db = db
        try:
            session_id = db.create_session("private-session", "api_server")
            db.append_message(session_id, "user", "private history")
            descriptor = _continuation_descriptor(db, session_id)
            app = _create_runs_app(adapter)
            async with TestClient(TestServer(app)) as cli:
                response = await cli.post(
                    "/v1/runs",
                    json={"input": "continue", "continuation": descriptor},
                )
                payload = await response.json()

            assert response.status == 403
            assert payload["error"]["code"] == "session_continuation_auth_required"
            assert "private" not in str(payload)
            assert adapter._run_statuses == {}
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_continuation_requires_configured_bearer_before_allocation(
        self,
        tmp_path,
    ):
        adapter = _make_adapter(api_key="sk-secret")
        db = SessionDB(tmp_path / "authenticated-continuation.db")
        adapter._session_db = db
        try:
            session_id = db.create_session("private-session", "api_server")
            db.append_message(session_id, "user", "private history")
            descriptor = _continuation_descriptor(db, session_id)
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {
                "final_response": "continued"
            }
            mock_agent.session_prompt_tokens = 0
            mock_agent.session_completion_tokens = 0
            mock_agent.session_total_tokens = 0

            app = _create_runs_app(adapter)
            with patch.object(
                adapter,
                "_create_agent",
                return_value=mock_agent,
            ) as create_agent:
                async with TestClient(TestServer(app)) as cli:
                    unauthorized = await cli.post(
                        "/v1/runs",
                        json={"input": "continue", "continuation": descriptor},
                    )
                    unauthorized_payload = await unauthorized.json()

                    assert unauthorized.status == 401
                    assert (
                        unauthorized_payload["error"]["code"]
                        == "gateway_auth_failed"
                    )
                    create_agent.assert_not_called()
                    assert adapter._run_statuses == {}
                    assert adapter._run_streams == {}
                    assert adapter._active_run_tasks == {}
                    assert adapter._active_continuation_sessions == {}

                    authorized = await cli.post(
                        "/v1/runs",
                        json={"input": "continue", "continuation": descriptor},
                        headers={"Authorization": "Bearer sk-secret"},
                    )
                    assert authorized.status == 202, await authorized.text()

            create_agent.assert_called_once()
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_route_conflict_releases_verified_continuation_claim(
        self,
        adapter,
        tmp_path,
    ):
        db = SessionDB(tmp_path / "route-conflict-continuation.db")
        adapter._session_db = db
        adapter._model_routes = {
            "locked-route": {
                "model": "route/model",
                "provider": "openrouter",
            }
        }
        try:
            session_id = db.create_session("route-session", "api_server")
            db.append_message(session_id, "user", "private history")
            descriptor = _continuation_descriptor(db, session_id)
            app = _create_runs_app(adapter)
            async with TestClient(TestServer(app)) as cli:
                response = await cli.post(
                    "/v1/runs",
                    json={
                        "input": "continue",
                        "continuation": descriptor,
                        "model": "locked-route",
                        "provider": "anthropic",
                    },
                )
                payload = await response.json()

            assert response.status == 400
            assert payload["error"]["type"] == "invalid_request_error"
            assert adapter._run_statuses == {}
            assert adapter._run_streams == {}
            assert adapter._active_continuation_sessions == {}
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_changed_revision_fails_before_run_allocation(self, adapter, tmp_path):
        db = SessionDB(tmp_path / "changed.db")
        adapter._session_db = db
        try:
            session_id = db.create_session("changed-session", "api_server")
            db.append_message(session_id, "user", "original")
            descriptor = _continuation_descriptor(db, session_id)
            db.append_message(session_id, "assistant", "new state")

            app = _create_runs_app(adapter)
            with patch.object(adapter, "_create_agent") as create_agent:
                async with TestClient(TestServer(app)) as cli:
                    resp = await cli.post(
                        "/v1/runs",
                        json={"input": "continue", "continuation": descriptor},
                    )
                    payload = await resp.json()

            assert resp.status == 409
            assert payload["error"]["code"] == "session_continuation_changed"
            assert "original" not in str(payload)
            assert "new state" not in str(payload)
            create_agent.assert_not_called()
            assert adapter._run_streams == {}
            assert adapter._run_statuses == {}
            assert adapter._active_continuation_sessions == {}
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_cross_session_revision_fails_closed(self, adapter, tmp_path):
        db = SessionDB(tmp_path / "cross-session.db")
        adapter._session_db = db
        try:
            first_id = db.create_session("first-session", "api_server")
            second_id = db.create_session("second-session", "api_server")
            db.append_message(first_id, "user", "first history")
            db.append_message(second_id, "user", "second history")
            descriptor = _continuation_descriptor(db, first_id)
            descriptor["session_id"] = second_id

            app = _create_runs_app(adapter)
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "continue", "continuation": descriptor},
                )
                payload = await resp.json()

            assert resp.status == 409
            assert payload["error"]["code"] == "session_continuation_changed"
            assert adapter._run_statuses == {}
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_compression_rotation_makes_tip_descriptor_stale(
        self,
        adapter,
        tmp_path,
    ):
        db = SessionDB(tmp_path / "compressed.db")
        adapter._session_db = db
        try:
            tip_id = db.create_session("old-tip", "api_server")
            db.append_message(tip_id, "user", "old tip history")
            descriptor = _continuation_descriptor(db, tip_id)
            db.end_session(tip_id, "compression")
            new_tip = db.create_session(
                "new-tip",
                "api_server",
                parent_session_id=tip_id,
            )
            db.append_message(new_tip, "user", "new tip history")

            app = _create_runs_app(adapter)
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/v1/runs",
                    json={"input": "continue", "continuation": descriptor},
                )
                payload = await resp.json()

            assert resp.status == 409
            assert payload["error"]["code"] == "session_continuation_changed"
            assert adapter._run_statuses == {}
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_continuation_rejects_ambiguous_or_malformed_input(
        self,
        adapter,
        tmp_path,
    ):
        db = SessionDB(tmp_path / "invalid.db")
        adapter._session_db = db
        try:
            session_id = db.create_session("invalid-session", "api_server")
            db.append_message(session_id, "user", "history")
            descriptor = _continuation_descriptor(db, session_id)
            cases = [
                ({"input": "continue", "continuation": "invalid"}, 400),
                (
                    {
                        "input": "continue",
                        "continuation": {**descriptor, "extra": True},
                    },
                    400,
                ),
                (
                    {
                        "input": "continue",
                        "session_id": session_id,
                        "continuation": descriptor,
                    },
                    400,
                ),
                (
                    {
                        "input": "continue",
                        "conversation_history": [],
                        "continuation": descriptor,
                    },
                    400,
                ),
                (
                    {
                        "input": [
                            {"role": "user", "content": "old"},
                            {"role": "user", "content": "new"},
                        ],
                        "continuation": descriptor,
                    },
                    400,
                ),
                (
                    {
                        "input": "continue",
                        "continuation": {**descriptor, "version": True},
                    },
                    400,
                ),
                (
                    {
                        "input": "continue",
                        "continuation": {**descriptor, "revision": "sessionrev_bad"},
                    },
                    400,
                ),
            ]

            app = _create_runs_app(adapter)
            async with TestClient(TestServer(app)) as cli:
                for body, expected_status in cases:
                    resp = await cli.post("/v1/runs", json=body)
                    assert resp.status == expected_status, await resp.text()

            assert adapter._run_statuses == {}
            assert adapter._run_streams == {}
            assert adapter._active_continuation_sessions == {}
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_only_one_continuation_run_can_own_a_session_and_stop_releases_it(
        self,
        adapter,
        tmp_path,
    ):
        db = SessionDB(tmp_path / "active.db")
        adapter._session_db = db
        try:
            session_id = db.create_session("active-session", "api_server")
            db.append_message(session_id, "user", "history")
            descriptor = _continuation_descriptor(db, session_id)
            slow_agent, ready, interrupted = _make_slow_agent()

            app = _create_runs_app(adapter)
            with patch.object(adapter, "_create_agent", return_value=slow_agent):
                async with TestClient(TestServer(app)) as cli:
                    first = await cli.post(
                        "/v1/runs",
                        json={"input": "first", "continuation": descriptor},
                    )
                    assert first.status == 202, await first.text()
                    first_id = (await first.json())["run_id"]
                    assert await asyncio.to_thread(ready.wait, 2)

                    second = await cli.post(
                        "/v1/runs",
                        json={"input": "second", "continuation": descriptor},
                    )
                    second_payload = await second.json()
                    assert second.status == 409
                    assert (
                        second_payload["error"]["code"]
                        == "session_continuation_active"
                    )

                    stopped = await cli.post(f"/v1/runs/{first_id}/stop")
                    assert stopped.status == 200
                    assert interrupted.wait(timeout=2)
                    for _ in range(40):
                        if not adapter._active_continuation_sessions:
                            break
                        await asyncio.sleep(0.025)

            assert adapter._active_continuation_sessions == {}
            assert len(adapter._run_statuses) == 1
            assert adapter._run_statuses[first_id]["status"] == "cancelled"
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_verification_reservation_closes_concurrent_snapshot_race(
        self,
        adapter,
        tmp_path,
    ):
        db = SessionDB(tmp_path / "verification-race.db")
        try:
            session_id = db.create_session("verification-race", "api_server")
            db.append_message(session_id, "user", "history")
            descriptor = _continuation_descriptor(db, session_id)
            snapshot_started = threading.Event()
            release_snapshot = threading.Event()

            class BlockingSnapshotDB:
                calls = 0

                def get_continuation_snapshot(self, requested_id):
                    self.calls += 1
                    snapshot_started.set()
                    release_snapshot.wait(timeout=5)
                    return db.get_continuation_snapshot(requested_id)

            blocking_db = BlockingSnapshotDB()
            adapter._session_db = blocking_db
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "done"}
            mock_agent.session_prompt_tokens = 0
            mock_agent.session_completion_tokens = 0
            mock_agent.session_total_tokens = 0

            app = _create_runs_app(adapter)
            with patch.object(adapter, "_create_agent", return_value=mock_agent):
                async with TestClient(TestServer(app)) as cli:
                    first_task = asyncio.create_task(
                        cli.post(
                            "/v1/runs",
                            json={"input": "first", "continuation": descriptor},
                        )
                    )
                    assert await asyncio.to_thread(snapshot_started.wait, 2)

                    second = await cli.post(
                        "/v1/runs",
                        json={"input": "second", "continuation": descriptor},
                    )
                    second_payload = await second.json()
                    assert second.status == 409
                    assert (
                        second_payload["error"]["code"]
                        == "session_continuation_active"
                    )
                    assert blocking_db.calls == 1

                    release_snapshot.set()
                    first = await first_task
                    assert first.status == 202, await first.text()
                    for _ in range(40):
                        if not adapter._active_continuation_sessions:
                            break
                        await asyncio.sleep(0.025)

            assert adapter._active_continuation_sessions == {}
            assert len(adapter._run_statuses) == 1
        finally:
            if "release_snapshot" in locals():
                release_snapshot.set()
            db.close()


class TestStartRunValidation:

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
