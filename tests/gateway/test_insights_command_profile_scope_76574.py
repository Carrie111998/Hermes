"""Regression test for issue #76574 — /insights executor scope hop.

Slash-command dispatch does not install the per-profile secret/home scope on
its own (only the agent turn's ``_run_agent`` wrapper does). The unwrapped
/insights handler opened ``SessionDB()`` against the DEFAULT profile's
``get_hermes_home()`` regardless of which profile the turn belonged to, and
used a bare ``run_in_executor`` that does not propagate contextvars into the
worker thread even when the caller *was* scoped. Mirrors the precedent fix
and its test for /compress (test_compress_command.py).
"""
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str = "/insights") -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


class _FakeSessionDB:
    """Captures the HERMES_HOME visible at construction time inside the
    executor thread, so the test can prove which profile's DB was opened."""

    seen_home = None

    def __init__(self):
        from hermes_constants import get_hermes_home
        type(self).seen_home = get_hermes_home()

    def close(self):
        pass


@pytest.mark.asyncio
async def test_insights_command_multiplexed_opens_profile_scoped_db(tmp_path):
    """/insights for profile B must never open profile A's / default state.db."""
    from gateway.run import GatewayRunner
    from agent import secret_scope as ss

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
        multiplex_profiles=True,
    )
    profile_home = tmp_path / "profiles" / "milo"
    profile_home.mkdir(parents=True)
    runner._resolve_profile_home_for_source = MagicMock(return_value=profile_home)

    fake_engine = MagicMock()
    fake_engine.generate.return_value = "report"
    fake_engine.format_gateway.return_value = "formatted insights"

    ss.set_multiplex_active(True)
    try:
        with (
            patch("hermes_state.SessionDB", _FakeSessionDB),
            patch("agent.insights.InsightsEngine", return_value=fake_engine),
        ):
            result = await runner._handle_insights_command(_make_event())
    finally:
        ss.set_multiplex_active(False)
        runner._shutdown_executor()

    assert result == "formatted insights"
    assert _FakeSessionDB.seen_home == profile_home
    runner._resolve_profile_home_for_source.assert_called_once()


@pytest.mark.asyncio
async def test_insights_command_single_profile_skips_profile_resolution():
    """Multiplexing off -> the scope wrapper is a transparent pass-through."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
    )
    runner._resolve_profile_home_for_source = MagicMock()

    fake_engine = MagicMock()
    fake_engine.generate.return_value = "report"
    fake_engine.format_gateway.return_value = "formatted insights"

    with (
        patch("hermes_state.SessionDB", _FakeSessionDB),
        patch("agent.insights.InsightsEngine", return_value=fake_engine),
    ):
        result = await runner._handle_insights_command(_make_event())

    assert result == "formatted insights"
    runner._resolve_profile_home_for_source.assert_not_called()
    runner._shutdown_executor()
