"""Regression coverage for task-local cron approval identity."""

import os
from unittest.mock import MagicMock, patch

from cron.scheduler import run_job

_RUNTIME = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}


def test_run_job_scopes_cron_identity_without_process_leak(tmp_path, monkeypatch):
    """Cron policy is active during the run and absent from later gateway turns."""
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    observed = {}

    def run_conversation(_prompt):
        from gateway.session_context import get_session_env
        from tools.approval import _is_cron_approval_context

        observed["context_value"] = get_session_env("HERMES_CRON_SESSION", "")
        observed["approval_context"] = _is_cron_approval_context()
        observed["process_value"] = os.environ.get("HERMES_CRON_SESSION")
        return {"final_response": "ok"}

    job = {"id": "cron-context", "name": "test", "prompt": "hello"}
    fake_db = MagicMock()

    with (
        patch("cron.scheduler._hermes_home", tmp_path),
        patch("cron.scheduler._resolve_origin", return_value=None),
        patch("hermes_cli.env_loader.load_hermes_dotenv"),
        patch("hermes_cli.env_loader.reset_secret_source_cache"),
        patch("hermes_state.SessionDB", return_value=fake_db),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=_RUNTIME,
        ),
        patch("tools.mcp_tool.discover_mcp_tools", return_value=[]),
        patch("run_agent.AIAgent") as mock_agent_cls,
    ):
        mock_agent = MagicMock()
        mock_agent.run_conversation.side_effect = run_conversation
        mock_agent_cls.return_value = mock_agent

        success, _output, final_response, error = run_job(job)

    assert success is True
    assert final_response == "ok"
    assert error is None
    assert observed == {
        "context_value": "1",
        "approval_context": True,
        "process_value": None,
    }
    assert os.environ.get("HERMES_CRON_SESSION") is None
