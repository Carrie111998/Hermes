"""Gateway routing and invariants for ``/refresh``."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import timedelta
from pathlib import Path

import pytest

from tests.gateway.test_gateway_command_dispatch_minimal import _make_event, _make_runner


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["missing", object(), 17])
async def test_gateway_refresh_global_fallback_uses_default_adapters_in_platform_scopes(
    tmp_path, monkeypatch, profile
):
    """Unresolvable profile metadata must retain the global adapter owner."""
    from agent import skill_commands
    from gateway.config import Platform
    from gateway.session_context import get_session_env

    global_home = tmp_path / ".hermes"
    global_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(global_home))

    runner, primary = _make_runner()
    event = _make_event("/refresh")
    event.source.profile = profile
    discord = MagicMock(name="default-discord")
    runner.adapters = {
        Platform.TELEGRAM: primary,
        Platform.DISCORD: discord,
    }
    runner._profile_adapters = {}
    observed = []

    def reload():
        observed.append(
            (
                "reload",
                Path(skill_commands.get_hermes_home()),
                get_session_env("HERMES_SESSION_PLATFORM"),
            )
        )
        return {"added": [], "removed": [], "unchanged": [], "total": 0, "commands": 0}

    def refresh(label):
        def run():
            observed.append(
                (
                    label,
                    Path(skill_commands.get_hermes_home()),
                    get_session_env("HERMES_SESSION_PLATFORM"),
                )
            )
        return run

    monkeypatch.setattr(skill_commands, "reload_skills", reload)
    primary.refresh_skill_group = MagicMock(side_effect=refresh("telegram"))
    discord.refresh_skill_group = MagicMock(side_effect=refresh("discord"))

    await runner._reload_skills_and_refresh_adapters(event.source)

    assert observed == [
        ("reload", global_home, "telegram"),
        ("telegram", global_home, "telegram"),
        ("discord", global_home, "discord"),
    ]


@pytest.mark.asyncio
async def test_gateway_refresh_resyncs_only_invoking_profile_adapters_in_their_platform_scopes(
    tmp_path, monkeypatch
):
    """A secondary Telegram refresh must not touch primary or leak into Discord."""
    from agent import skill_commands
    from gateway.config import Platform
    from gateway.session_context import get_session_env

    runner, primary = _make_runner()
    runner.config.multiplex_profiles = True
    secondary_home = tmp_path / "profiles" / "secondary"
    secondary_home.mkdir(parents=True)
    event = _make_event("/refresh")
    event.source.profile = "secondary"
    secondary_telegram = MagicMock(name="secondary-telegram")
    secondary_discord = MagicMock(name="secondary-discord")
    runner._profile_adapters = {
        "secondary": {
            Platform.TELEGRAM: secondary_telegram,
            Platform.DISCORD: secondary_discord,
        }
    }
    runner._resolve_profile_home_for_source = lambda source: secondary_home

    observed = []

    def reload():
        observed.append(("reload", Path(skill_commands.get_hermes_home()), get_session_env("HERMES_SESSION_PLATFORM")))
        return {"added": [], "removed": [], "unchanged": [], "total": 0, "commands": 0}

    def refresh(label):
        def run():
            observed.append((label, Path(skill_commands.get_hermes_home()), get_session_env("HERMES_SESSION_PLATFORM")))
        return run

    monkeypatch.setattr(skill_commands, "reload_skills", reload)
    primary.refresh_skill_group = MagicMock(side_effect=refresh("primary"))
    secondary_telegram.refresh_skill_group = MagicMock(side_effect=refresh("telegram"))
    secondary_discord.refresh_skill_group = MagicMock(side_effect=refresh("discord"))

    await runner._reload_skills_and_refresh_adapters(event.source)

    primary.refresh_skill_group.assert_not_called()
    secondary_telegram.refresh_skill_group.assert_called_once_with()
    secondary_discord.refresh_skill_group.assert_called_once_with()
    assert observed == [
        ("reload", secondary_home, "telegram"),
        ("telegram", secondary_home, "telegram"),
        ("discord", secondary_home, "discord"),
    ]


@pytest.mark.asyncio
async def test_gateway_soft_refresh_routes_and_queues_context_without_transcript_mutation():
    runner, _adapter = _make_runner()
    _adapter.refresh_skill_group = MagicMock(return_value=(1, []))
    runner.session_store.append_to_transcript = MagicMock()
    runner._evict_cached_agent = MagicMock()
    result = SimpleNamespace(
        context_note="[fresh profile context]",
        report="Refreshed skills and memory. Gateway not restarted.",
    )

    with patch("agent.session_refresh.build_soft_refresh", return_value=result):
        output = await runner._handle_message(_make_event("/refresh"))

    assert output == result.report
    session_key = runner._session_key_for_source(_make_event("/refresh").source)
    assert [r["note"] for r in runner._pending_refresh_notes[session_key]] == [
        result.context_note
    ]
    runner.session_store.append_to_transcript.assert_not_called()
    runner._evict_cached_agent.assert_not_called()
    _adapter.refresh_skill_group.assert_called_once_with()


@pytest.mark.asyncio
async def test_gateway_refresh_branch_delegates_to_existing_branch_handler():
    runner, _adapter = _make_runner()
    runner._reload_skills_and_refresh_adapters = AsyncMock(return_value={})
    runner._handle_branch_command = AsyncMock(return_value="branched")
    event = _make_event("/refresh --branch")

    output = await runner._handle_refresh_command(event)

    assert output == "branched"
    runner._reload_skills_and_refresh_adapters.assert_awaited_once_with(event.source)
    runner._handle_branch_command.assert_awaited_once_with(event)


def test_gateway_refresh_note_claim_rejects_old_synthetic_command_and_other_session():
    runner, _adapter = _make_runner()
    refresh_event = _make_event("/refresh")
    note = {
        "note": "[fresh context]",
        "after": refresh_event.timestamp,
        "generation": 7,
    }
    note["token"] = "token-a"
    note["reserved_by"] = None
    runner._pending_refresh_notes = {"session-a": [note]}

    older = _make_event("older queued prompt")
    older.internal = False
    older.timestamp = refresh_event.timestamp - timedelta(seconds=1)
    assert runner._claim_refresh_context_note("session-a", older, 7) is None

    synthetic = _make_event("automatic continuation")
    synthetic.timestamp = refresh_event.timestamp + timedelta(seconds=1)
    synthetic.internal = True
    assert runner._claim_refresh_context_note("session-a", synthetic, 7) is None

    command = _make_event("/status")
    command.timestamp = refresh_event.timestamp + timedelta(seconds=2)
    assert runner._claim_refresh_context_note("session-a", command, 7) is None

    genuine = _make_event("real next turn")
    genuine.internal = False
    genuine.timestamp = refresh_event.timestamp + timedelta(seconds=3)
    assert runner._claim_refresh_context_note("session-b", genuine, 7) is None
    assert runner._claim_refresh_context_note("session-a", genuine, 6) is None
    assert runner._claim_refresh_context_note("session-a", genuine, 7) == {
        "token": "token-a",
        "note": "[fresh context]",
    }
    assert runner._claim_refresh_context_note("session-a", genuine, 8) is None

    runner._finish_refresh_context_note("session-a", "token-a", 7, attempted=False)
    assert runner._claim_refresh_context_note("session-a", genuine, 8)["note"] == "[fresh context]"
    runner._finish_refresh_context_note("session-a", "token-a", 8, attempted=True)
    assert "session-a" not in runner._pending_refresh_notes


def test_gateway_second_refresh_survives_first_reservation_commit():
    runner, _adapter = _make_runner()
    event = _make_event("real next turn")
    event.internal = False
    records = [
        {"token": "one", "note": "NOTE-1", "after": event.timestamp - timedelta(seconds=2), "generation": 1, "reserved_by": None},
        {"token": "two", "note": "NOTE-2", "after": event.timestamp - timedelta(seconds=1), "generation": 2, "reserved_by": None},
    ]
    runner._pending_refresh_notes = {"session-a": records}

    first = runner._claim_refresh_context_note("session-a", event, 2)
    assert first == {"token": "one", "note": "NOTE-1"}
    runner._finish_refresh_context_note("session-a", "one", 2, attempted=True)

    second = runner._claim_refresh_context_note("session-a", event, 3)
    assert second == {"token": "two", "note": "NOTE-2"}
