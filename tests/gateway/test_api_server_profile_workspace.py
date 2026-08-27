"""Tests: the API-server surface anchors a per-profile workspace cwd.

The api_server multiplexes profiles through one process (X-Hermes-Profile /
``/p/<profile>/`` scope), but TERMINAL_CWD is a single process-global — every
profile shared one working directory and could overwrite each other's files.
These tests pin the contract that closes that gap:

- ``_profile_workspace_dir()`` resolves (and creates) ``<HERMES_HOME>/workspace``
  and degrades to "" instead of failing the turn when it cannot;
- ``_bind_api_server_session(cwd=...)`` drives the existing session-cwd
  machinery, so system-prompt context discovery sees the workspace;
- a completed /v1/runs turn registers the mechanical terminal/file override
  under both the tool task id and the run-scoped key — same infrastructure
  TUI/ACP entries drive via ``register_task_env_overrides``.
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True))


@pytest.fixture
def adapter():
    return _make_adapter()


def _create_runs_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    return app


def _make_instant_agent():
    mock_agent = MagicMock()
    mock_agent.run_conversation.return_value = {"final_response": "done"}
    mock_agent.session_prompt_tokens = 0
    mock_agent.session_completion_tokens = 0
    mock_agent.session_total_tokens = 0
    return mock_agent


async def _wait_completed(cli, run_id: str) -> dict:
    status = {}
    for _ in range(40):
        status_resp = await cli.get(f"/v1/runs/{run_id}")
        status = await status_resp.json()
        if status["status"] in {"completed", "failed"}:
            break
        await asyncio.sleep(0.05)
    return status


class TestProfileWorkspaceDir:
    def test_resolves_home_workspace_and_creates_it(self):
        from hermes_constants import get_hermes_home

        ws = _make_adapter()._profile_workspace_dir()

        assert Path(ws) == get_hermes_home() / "workspace"
        assert Path(ws).is_dir(), "the anchor directory must exist before first use"

    def test_degrades_to_empty_string_on_resolution_failure(self):
        # The helper imports get_hermes_home at call time; make it raise to
        # simulate an unresolvable home. Callers must treat "" as "skip
        # registration", never as a hard failure of the request.
        with patch("hermes_constants.get_hermes_home", side_effect=RuntimeError("no home")):
            assert _make_adapter()._profile_workspace_dir() == ""


class TestBindForwardsWorkspaceCwd:
    def test_logical_cwd_flows_into_session_cwd_machinery(self, tmp_path):
        from agent.runtime_cwd import resolve_agent_cwd
        from gateway.session_context import clear_session_vars

        tokens = _make_adapter()._bind_api_server_session(
            chat_id="c1",
            session_key="k1",
            session_id="s1",
            cwd=str(tmp_path),
        )
        try:
            assert resolve_agent_cwd() == tmp_path, (
                "bind must pin the logical working directory for this context, "
                "not leave agents on the process-global TERMINAL_CWD"
            )
        finally:
            clear_session_vars(tokens)


class TestRunRegistersTerminalCwdAnchor:
    @pytest.mark.asyncio
    async def test_completed_run_anchors_task_and_run_keys(self, adapter):
        from hermes_constants import get_hermes_home
        from tools.terminal_tool import clear_task_env_overrides, get_session_cwd

        expected_ws = str(Path(get_hermes_home()) / "workspace")
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_create.return_value = _make_instant_agent()

                resp = await cli.post("/v1/runs", json={
                    "input": "hi",
                    "session_id": "ws-sess-1",
                })
                assert resp.status == 202
                run_id = (await resp.json())["run_id"]

                status = await _wait_completed(cli, run_id)
                assert status["status"] == "completed"

                try:
                    # effective task id = session_id: later commands in the run
                    # resolve their cwd through this key.
                    assert get_session_cwd("ws-sess-1") == expected_ws
                    # approval key = run id: resolves the FIRST command's lookup.
                    assert get_session_cwd(run_id) == expected_ws
                finally:
                    clear_task_env_overrides("ws-sess-1")
                    clear_task_env_overrides(run_id)
