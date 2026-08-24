"""Upgrade regressions for the Bot Chat native goal bridge."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tools import bot_mode_goal, bot_mode_probe


@pytest.fixture(autouse=True)
def _clear_bot_probe_cache():
    bot_mode_probe._reset_cache_for_tests()
    yield
    bot_mode_probe._reset_cache_for_tests()


class _FakeDB:
    def __init__(self, home: Path):
        self.db_path = str(home / "state.db")

    def get_session_title(self, _sid):
        return "Bot Chat"


class _FakeAgent:
    def __init__(self, home: Path):
        self._session_db = _FakeDB(home)
        self.session_id = "legacy-managed-goal"
        self._session_title_hint = None
        self._bot_mode_protocol = True
        self.tools: list = []
        self.valid_tool_names: set = set()


def _legacy_managed_home(tmp_path: Path) -> Path:
    """Managed install whose old SOUL already contains the protocol text."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "SOUL.md").write_text(
        "# Existing profile\n\n## Messaging other agents\nLegacy appended protocol.\n",
        encoding="utf-8",
    )

    teammate = home / "profiles" / "researcher"
    teammate.mkdir(parents=True)
    (teammate / "profile.yaml").write_text(
        textwrap.dedent(
            """\
            description: teammate for upgrade regression
            ui_meta:
              hermes-bots:
                shape: cloud
            """
        ),
        encoding="utf-8",
    )
    return home


def test_legacy_protocol_dedupe_does_not_remove_goal_manage(tmp_path):
    home = _legacy_managed_home(tmp_path)
    agent = _FakeAgent(home)

    # Prompt rendering correctly stays silent because an older Bot Mode build
    # already appended the protocol text into SOUL.md. Tool eligibility must
    # use managed-install state instead of mistaking that empty section for an
    # unmanaged install.
    assert bot_mode_probe.get_bot_mode_protocol_section(home, force_refresh=True) == ""
    assert bot_mode_probe.is_bot_mode_managed(home) is True

    assert bot_mode_goal.ensure_goal_manage_tool(agent) is True
    assert [tool["function"]["name"] for tool in agent.tools] == ["goal_manage"]
    assert "goal_manage" in agent.valid_tool_names
