"""Cron pre-agent policy contract for triage-only fallbacks."""
from unittest.mock import MagicMock, patch


def test_cron_auth_resolution_does_not_promote_triage_fallback_to_full_job(tmp_path):
    from cron.scheduler import run_job
    from hermes_cli.auth import AuthError

    (tmp_path / "config.yaml").write_text(
        "model:\n"
        "  default: gpt-5.6-terra\n"
        "  provider: openai-codex\n"
        "fallback_providers:\n"
        "  - provider: custom\n"
        "    model: qwen3:8b\n"
        "    base_url: http://127.0.0.1:11434/v1\n"
        "    failure_policy: triage_and_notify\n",
        encoding="utf-8",
    )
    job = {
        "id": "triage-only-auth-fallback",
        "name": "triage-only auth fallback",
        "prompt": "perform consequential work",
        "provider_snapshot": "openai-codex",
        "model_snapshot": "gpt-5.6-terra",
    }
    requested: list[str | None] = []

    def resolver(**kwargs):
        requested.append(kwargs.get("requested"))
        raise AuthError("No Codex credentials stored")

    with (
        patch("cron.scheduler._hermes_home", tmp_path),
        patch("cron.scheduler._resolve_origin", return_value=None),
        patch("hermes_cli.env_loader.load_hermes_dotenv"),
        patch("hermes_cli.env_loader.reset_secret_source_cache"),
        patch("hermes_state.SessionDB", return_value=MagicMock()),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=resolver),
        patch("tools.mcp_tool.discover_mcp_tools", return_value=[]),
        patch("run_agent.AIAgent") as agent_cls,
    ):
        success, _output, _final_response, error = run_job(job)

    assert success is False
    assert error is not None
    assert requested == [None]
    agent_cls.assert_not_called()
