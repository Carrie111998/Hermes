"""Tests for /v1/runs endpoints: start, status, events, and stop.

Covers:
- POST /v1/runs — start a run (202)
- GET /v1/runs/{run_id} — poll run status
- GET /v1/runs/{run_id}/events — SSE event stream
- POST /v1/runs/{run_id}/stop — interrupt a running agent
- Auth, error handling, and cleanup
"""

import asyncio
import sqlite3
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
    app.router.add_get("/v1/runs", adapter._handle_lookup_run)
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
    async def test_reservation_is_serialized_with_stale_snapshot_recovery(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter()
        app = _create_runs_app(adapter)
        from gateway import run_ledger

        real_recover = run_ledger.recover_interrupted_runs
        real_reserve = run_ledger.reserve_run
        recovery_calls = 0
        second_recovery_entered = threading.Event()
        allow_second_recovery = threading.Event()
        first_row_reserved = threading.Event()
        release_first_reservation = threading.Event()

        def controlled_recover(active_run_ids=None):
            nonlocal recovery_calls
            recovery_calls += 1
            if recovery_calls == 2:
                second_recovery_entered.set()
                allow_second_recovery.wait(timeout=2)
            return real_recover(active_run_ids)

        def controlled_reserve(**kwargs):
            result = real_reserve(**kwargs)
            if kwargs.get("idempotency_key") == "serialized-a":
                first_row_reserved.set()
                release_first_reservation.wait(timeout=2)
            return result

        monkeypatch.setattr(run_ledger, "recover_interrupted_runs", controlled_recover)
        monkeypatch.setattr(run_ledger, "reserve_run", controlled_reserve)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent
                first = asyncio.create_task(
                    cli.post(
                        "/v1/runs",
                        json={"input": "first"},
                        headers={"Idempotency-Key": "serialized-a"},
                    )
                )
                assert await asyncio.to_thread(first_row_reserved.wait, 1)
                second = asyncio.create_task(
                    cli.post(
                        "/v1/runs",
                        json={"input": "second"},
                        headers={"Idempotency-Key": "serialized-b"},
                    )
                )
                await asyncio.sleep(0.05)
                assert not second_recovery_entered.is_set()
                release_first_reservation.set()
                first_response = await first
                assert await asyncio.to_thread(second_recovery_entered.wait, 1)
                allow_second_recovery.set()
                second_response = await second
                assert first_response.status == 202
                assert second_response.status == 202
                first_run_id = (await first_response.json())["run_id"]
                lookup = await cli.get(f"/v1/runs/{first_run_id}")
                assert (await lookup.json())["status"] != "interrupted"

    @pytest.mark.asyncio
    async def test_durable_reservation_does_not_block_event_loop(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter()
        app = _create_runs_app(adapter)
        from gateway import run_ledger

        real_reserve = run_ledger.reserve_run

        reserve_entered = threading.Event()
        release_reserve = threading.Event()

        def slow_reserve(**kwargs):
            reserve_entered.set()
            release_reserve.wait(timeout=2)
            return real_reserve(**kwargs)

        monkeypatch.setattr(run_ledger, "reserve_run", slow_reserve)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent
                post_task = asyncio.create_task(
                    cli.post("/v1/runs", json={"input": "hello"})
                )
                assert await asyncio.to_thread(reserve_entered.wait, 1)
                started = time.monotonic()
                await asyncio.sleep(0.02)
                elapsed = time.monotonic() - started
                assert elapsed < 0.5
                release_reserve.set()
                response = await post_task
                assert response.status == 202

    @pytest.mark.asyncio
    async def test_idempotency_key_conflict_does_not_start_second_run(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter()
        app = _create_runs_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent
                headers = {"Idempotency-Key": "aic-conflict-398"}

                first = await cli.post(
                    "/v1/runs", json={"input": "first"}, headers=headers
                )
                conflict = await cli.post(
                    "/v1/runs", json={"input": "changed"}, headers=headers
                )

                assert first.status == 202
                assert conflict.status == 409
                data = await conflict.json()
                assert data["error"]["code"] == "idempotency_conflict"
                assert mock_create.call_count == 1

    @pytest.mark.asyncio
    async def test_same_idempotency_key_recovers_original_run(self, monkeypatch, tmp_path):
        """A lost 202 can be retried without dispatching a duplicate run."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter()
        app = _create_runs_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent
                headers = {"Idempotency-Key": "aic-submit-398"}
                payload = {"input": "hello", "session_id": "aic-session"}

                first = await cli.post("/v1/runs", json=payload, headers=headers)
                second = await cli.post("/v1/runs", json=payload, headers=headers)

                assert first.status == 202
                assert second.status == 202
                first_data = await first.json()
                second_data = await second.json()
                assert second_data["run_id"] == first_data["run_id"]
                assert second_data["session_id"] == "aic-session"
                assert second_data["idempotency_key"] == "aic-submit-398"
                assert mock_create.call_count == 1

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
    async def test_cross_profile_cannot_read_or_stop_cached_run(
        self, monkeypatch, tmp_path
    ):
        home_a = tmp_path / "profile-a"
        home_b = tmp_path / "profile-b"
        monkeypatch.setenv("HERMES_HOME", str(home_a))
        adapter = _make_adapter()
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent
                response = await cli.post(
                    "/v1/runs",
                    json={"input": "profile a"},
                    headers={"Idempotency-Key": "shared-profile-key"},
                )
                run_id = (await response.json())["run_id"]
                for _ in range(40):
                    if adapter._run_statuses[run_id]["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

            monkeypatch.setenv("HERMES_HOME", str(home_b))
            status_response = await cli.get(f"/v1/runs/{run_id}")
            stop_response = await cli.post(f"/v1/runs/{run_id}/stop")
            assert status_response.status == 404
            assert stop_response.status == 404

    @pytest.mark.asyncio
    async def test_missing_adapter_task_reconciles_live_owner_row(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from gateway.run_ledger import reserve_run, update_run

        reserve_run(
            run_id="run_no_task",
            idempotency_key="aic-no-task-398",
            request_fingerprint="fingerprint",
            data={"session_id": "no-task", "model": "test"},
        )
        update_run("run_no_task", "running")
        adapter = _make_adapter()
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            lookup = await cli.get(
                "/v1/runs", headers={"Idempotency-Key": "aic-no-task-398"}
            )
            assert lookup.status == 200
            assert (await lookup.json())["status"] == "interrupted"

    @pytest.mark.asyncio
    async def test_terminal_retention_purges_expired_correlation(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from gateway.run_ledger import RETENTION_SECONDS, reserve_run, update_run

        reserve_run(
            run_id="run_expired",
            idempotency_key="aic-expired-398",
            request_fingerprint="fingerprint",
            data={"session_id": "expired", "model": "test"},
        )
        update_run("run_expired", "completed")
        with sqlite3.connect(tmp_path / "state.db") as conn:
            conn.execute(
                "UPDATE api_runs SET updated_at = ? WHERE run_id = ?",
                (time.time() - RETENTION_SECONDS - 1, "run_expired"),
            )

        adapter = _make_adapter()
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            lookup = await cli.get(
                "/v1/runs", headers={"Idempotency-Key": "aic-expired-398"}
            )
            assert lookup.status == 404

    def test_terminal_status_cannot_regress(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from gateway.run_ledger import get_run, reserve_run, update_run

        reserve_run(
            run_id="run_terminal",
            idempotency_key="aic-terminal-398",
            request_fingerprint="fingerprint",
            data={"session_id": "terminal-session", "model": "test"},
        )
        update_run("run_terminal", "completed", output="done")
        update_run("run_terminal", "running", last_event="late.event")

        status = get_run("run_terminal")
        assert status["status"] == "completed"
        assert status["output"] == "done"
        assert status.get("last_event") != "late.event"

    def test_terminal_same_status_update_is_byte_immutable(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from gateway.run_ledger import get_run, reserve_run, update_run

        reserve_run(
            run_id="run_terminal_same",
            idempotency_key="aic-terminal-same-398",
            request_fingerprint="fingerprint",
            data={"session_id": "terminal-same", "model": "test"},
        )
        update_run("run_terminal_same", "completed", output="original")
        before = get_run("run_terminal_same")
        with sqlite3.connect(tmp_path / "state.db") as conn:
            durable_before = conn.execute(
                "SELECT * FROM api_runs WHERE run_id = ?", ("run_terminal_same",)
            ).fetchone()

        update_run(
            "run_terminal_same",
            "completed",
            output="changed",
            last_event="late.completed",
        )
        with sqlite3.connect(tmp_path / "state.db") as conn:
            durable_after = conn.execute(
                "SELECT * FROM api_runs WHERE run_id = ?", ("run_terminal_same",)
            ).fetchone()
        assert durable_after == durable_before
        assert get_run("run_terminal_same") == before

        adapter = _make_adapter()
        adapter._run_statuses["run_terminal_same"] = dict(before)
        adapter._set_run_status(
            "run_terminal_same",
            "completed",
            output="memory changed",
            last_event="late.memory.completed",
        )
        assert adapter._run_statuses["run_terminal_same"] == before

    def test_stopping_status_cannot_regress_to_active(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from gateway.run_ledger import get_run, reserve_run, update_run

        reserve_run(
            run_id="run_stopping",
            idempotency_key="aic-stopping-398",
            request_fingerprint="fingerprint",
            data={"session_id": "stopping-session", "model": "test"},
        )
        update_run("run_stopping", "running")
        update_run("run_stopping", "stopping")
        update_run("run_stopping", "waiting_for_approval")
        update_run("run_stopping", "running")

        assert get_run("run_stopping")["status"] == "stopping"

    @pytest.mark.asyncio
    async def test_restart_marks_orphaned_run_interrupted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from gateway.run_ledger import reserve_run, update_run

        reserve_run(
            run_id="run_orphaned",
            idempotency_key="aic-orphaned-398",
            request_fingerprint="fingerprint",
            data={"session_id": "orphaned-session", "model": "test"},
        )
        update_run("run_orphaned", "running")
        with sqlite3.connect(tmp_path / "state.db") as conn:
            conn.execute(
                "UPDATE api_runs SET owner_pid = ?, owner_started_at = ? WHERE run_id = ?",
                (999_999_999, 1, "run_orphaned"),
            )

        restarted_adapter = _make_adapter()
        restarted_app = _create_runs_app(restarted_adapter)
        async with TestClient(TestServer(restarted_app)) as cli:
            lookup = await cli.get(
                "/v1/runs", headers={"Idempotency-Key": "aic-orphaned-398"}
            )

            assert lookup.status == 200
            data = await lookup.json()
            assert data["run_id"] == "run_orphaned"
            assert data["status"] == "interrupted"
            assert data["last_event"] == "run.interrupted"

    @pytest.mark.asyncio
    async def test_correlation_lookup_survives_adapter_restart(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        headers = {"Idempotency-Key": "aic-restart-398"}
        first_adapter = _make_adapter()
        first_app = _create_runs_app(first_adapter)

        async with TestClient(TestServer(first_app)) as cli:
            with patch.object(first_adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent
                response = await cli.post(
                    "/v1/runs",
                    json={"input": "hello", "session_id": "restart-session"},
                    headers=headers,
                )
                run_id = (await response.json())["run_id"]
                for _ in range(40):
                    if first_adapter._run_statuses[run_id]["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

        restarted_adapter = _make_adapter()
        restarted_app = _create_runs_app(restarted_adapter)
        async with TestClient(TestServer(restarted_app)) as cli:
            status_response = await cli.get(f"/v1/runs/{run_id}")
            assert status_response.status == 200
            assert (await status_response.json())["status"] == "completed"

            lookup = await cli.get("/v1/runs", headers=headers)
            assert lookup.status == 200
            data = await lookup.json()
            assert data["run_id"] == run_id
            assert data["status"] == "completed"
            assert data["session_id"] == "restart-session"
            assert data["idempotency_key"] == "aic-restart-398"

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
    def test_interrupted_status_is_swept_from_memory(self, adapter):
        now = time.time()
        adapter._run_statuses["run_interrupted"] = {
            "run_id": "run_interrupted",
            "status": "interrupted",
            "updated_at": now - adapter._RUN_STATUS_TTL - 1,
        }

        adapter._sweep_orphaned_runs_once(now)

        assert "run_interrupted" not in adapter._run_statuses


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
                assert await asyncio.to_thread(agent_ready.wait, 3.0)

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
    async def test_storage_failure_cannot_prevent_active_interrupt(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter()
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent, ready, interrupted = _make_slow_agent()
                mock_create.return_value = mock_agent
                response = await cli.post("/v1/runs", json={"input": "hello"})
                run_id = (await response.json())["run_id"]
                assert await asyncio.to_thread(ready.wait, 3)

                monkeypatch.setattr(
                    "gateway.run_ledger.update_run",
                    MagicMock(side_effect=sqlite3.OperationalError("disk full")),
                )
                stop_response = await cli.post(f"/v1/runs/{run_id}/stop")

                assert stop_response.status == 200
                assert interrupted.wait(timeout=1)
                assert (await stop_response.json())["status"] == "stopping"

    @pytest.mark.asyncio
    async def test_stop_terminal_run_is_idempotent_after_restart(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        first_adapter = _make_adapter()
        first_app = _create_runs_app(first_adapter)
        async with TestClient(TestServer(first_app)) as cli:
            with patch.object(first_adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()
                mock_agent.run_conversation.return_value = {"final_response": "done"}
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent
                response = await cli.post(
                    "/v1/runs",
                    json={"input": "hello"},
                    headers={"Idempotency-Key": "aic-stop-terminal-398"},
                )
                run_id = (await response.json())["run_id"]
                for _ in range(40):
                    if first_adapter._run_statuses[run_id]["status"] == "completed":
                        break
                    await asyncio.sleep(0.05)

        restarted_adapter = _make_adapter()
        restarted_app = _create_runs_app(restarted_adapter)
        async with TestClient(TestServer(restarted_app)) as cli:
            first_stop = await cli.post(f"/v1/runs/{run_id}/stop")
            second_stop = await cli.post(f"/v1/runs/{run_id}/stop")

            assert first_stop.status == 200
            assert second_stop.status == 200
            assert (await first_stop.json())["status"] == "completed"
            assert (await second_stop.json())["run_id"] == run_id

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
                assert await asyncio.to_thread(started.wait, 3)

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
