from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from agent.secret_scope import reset_secret_scope, set_secret_scope
from cron.scheduler import (
    _now_in_cron_timezone,
    _resolve_cron_iteration_limits,
    _resolve_cron_max_iterations,
    run_job,
)
from tools.cronjob_tools import CRONJOB_SCHEMA


TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def test_agent_cron_tool_cannot_set_user_owned_iteration_budget():
    assert "max_iterations" not in CRONJOB_SCHEMA["parameters"]["properties"]


def test_weekly_policy_timezone_is_resolved_per_effective_profile_config():
    """Sequential profiles must not inherit a process-global timezone cache."""
    instant = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)

    buenos_aires = _now_in_cron_timezone(
        {"timezone": "America/Argentina/Buenos_Aires"},
        utc_now=instant,
    )
    tokyo = _now_in_cron_timezone(
        {"timezone": "Asia/Tokyo"},
        utc_now=instant,
    )

    assert buenos_aires.isoformat() == "2026-08-15T09:00:00-03:00"
    assert tokyo.isoformat() == "2026-08-15T21:00:00+09:00"


def test_profile_scoped_timezone_env_takes_precedence_without_global_cache():
    instant = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)

    buenos_aires_scope = set_secret_scope({
        "HERMES_TIMEZONE": "America/Argentina/Buenos_Aires"
    })
    try:
        buenos_aires = _now_in_cron_timezone(
            {"timezone": "UTC"},
            utc_now=instant,
        )
    finally:
        reset_secret_scope(buenos_aires_scope)

    tokyo_scope = set_secret_scope({"HERMES_TIMEZONE": "Asia/Tokyo"})
    try:
        tokyo = _now_in_cron_timezone(
            {"timezone": "UTC"},
            utc_now=instant,
        )
    finally:
        reset_secret_scope(tokyo_scope)

    assert buenos_aires.isoformat() == "2026-08-15T09:00:00-03:00"
    assert tokyo.isoformat() == "2026-08-15T21:00:00+09:00"


def test_per_job_limit_wins_over_cron_and_interactive_limits():
    cfg = {
        "agent": {"max_turns": 60},
        "cron": {"max_iterations": 20},
    }

    assert _resolve_cron_max_iterations({"max_iterations": 11}, cfg) == 11


def test_cron_limit_is_used_when_job_has_no_valid_limit():
    cfg = {
        "agent": {"max_turns": 60},
        "cron": {"max_iterations": 20},
    }

    for invalid in (0, True, 1.5, "not-an-int"):
        assert _resolve_cron_max_iterations({"max_iterations": invalid}, cfg) == 20


def test_final_day_limit_applies_only_to_matching_provider_before_reset():
    cfg = {
        "agent": {"max_turns": 60},
        "cron": {
            "weekly_final_day": {
                "enabled": True,
                "provider": "zai",
                "reset_weekday": 5,
                "reset_time": "10:06:40",
                "window_hours": 24,
                "max_iterations": 15,
            }
        },
    }
    friday = datetime(2026, 8, 7, 11, 10, tzinfo=TZ)

    assert _resolve_cron_iteration_limits(
        {"max_iterations": 11}, cfg, provider="zai", now=friday
    ) == (11, 15, "zai")
    assert (
        _resolve_cron_max_iterations(
            {"max_iterations": 11}, cfg, provider="zai", now=friday
        )
        == 15
    )
    assert (
        _resolve_cron_max_iterations(
            {"max_iterations": 11}, cfg, provider="custom", now=friday
        )
        == 11
    )


def test_final_day_limit_stops_after_reset():
    cfg = {
        "cron": {
            "weekly_final_day": {
                "enabled": True,
                "provider": "zai",
                "reset_weekday": 5,
                "reset_time": "10:06:40",
                "window_hours": 24,
                "max_iterations": 15,
            }
        }
    }
    after_reset = datetime(2026, 8, 8, 10, 10, tzinfo=TZ)

    assert (
        _resolve_cron_max_iterations(
            {"max_iterations": 11}, cfg, provider="zai", now=after_reset
        )
        == 11
    )


def test_invalid_weekly_policy_never_changes_the_base_limit():
    now = datetime(2026, 8, 7, 11, 10, tzinfo=TZ)
    invalid_policies = (
        {"enabled": True, "reset_weekday": True, "max_iterations": 15},
        {"enabled": True, "reset_weekday": 7, "max_iterations": 15},
        {"enabled": True, "reset_time": "25:00", "max_iterations": 15},
        {"enabled": True, "window_hours": 168, "max_iterations": 15},
        {"enabled": True, "window_hours": float("inf"), "max_iterations": 15},
    )

    for policy in invalid_policies:
        cfg = {"cron": {"weekly_final_day": policy}}
        assert (
            _resolve_cron_max_iterations(
                {"max_iterations": 11}, cfg, provider="zai", now=now
            )
            == 11
        )


def test_run_job_passes_the_resolved_cron_limit_to_the_agent(tmp_path):
    job = {
        "id": "bounded-cron-job",
        "name": "bounded cron job",
        "prompt": "hello",
        "max_iterations": 11,
    }
    fake_db = MagicMock()
    mock_agent = MagicMock()
    mock_agent.run_conversation.return_value = {"final_response": "ok"}

    with (
        patch("cron.scheduler._hermes_home", tmp_path),
        patch("cron.scheduler._resolve_origin", return_value=None),
        patch(
            "cron.scheduler.load_config",
            return_value={
                "agent": {"max_turns": 60},
                "cron": {"max_iterations": 20},
            },
        ),
        patch("hermes_cli.env_loader.load_hermes_dotenv"),
        patch("hermes_cli.env_loader.reset_secret_source_cache"),
        patch("hermes_state.SessionDB", return_value=fake_db),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "test-key",
                "base_url": "https://example.invalid/v1",
                "provider": "openrouter",
                "api_mode": "chat_completions",
            },
        ),
        patch("run_agent.AIAgent", return_value=mock_agent) as mock_agent_cls,
    ):
        success, _output, final_response, error = run_job(job)

    assert success is True
    assert final_response == "ok"
    assert error is None
    assert mock_agent_cls.call_args.kwargs["max_iterations"] == 11
