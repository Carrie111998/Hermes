"""Desktop/TUI slash-command parity for the live-dispatch family.

Three registration layers must agree for commands that run against the live
session agent: ``_LIVE_SESSION_DIRECT_COMMANDS`` (server routing), the
``slash.exec`` if-ladder (per-command handler), and the desktop TS registry
(``DESKTOP_COMMAND_SPECS``). The first two are pinned by
``test_refine_command.py``; this file pins the cross-language half — every
live-dispatch command that exists server-side must also exist in the desktop
palette, or it silently never shows up there.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture()
def server(hermes_home, monkeypatch):
    with patch.dict(
        "sys.modules",
        {
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
    yield mod
    mod._sessions.clear()
    mod._pending.clear()
    mod._answers.clear()


def _desktop_spec_names():
    """Parse the desktop registry's command names without executing TS."""
    repo_root = Path(__file__).resolve().parents[2]
    ts_path = (
        repo_root
        / "apps"
        / "desktop"
        / "src"
        / "lib"
        / "desktop-slash-commands.ts"
    )
    text = ts_path.read_text(encoding="utf-8")
    return set(re.findall(r"name:\s*'(/[a-z0-9-]+)'", text))


def test_live_dispatch_commands_exist_in_desktop_registry(server):
    """Commands that dispatch to the live session agent must be listed in the
    desktop palette. A missing row means the desktop classifies the command
    as an unknown extension — exactly how /refine's bug stayed invisible.

    Pinned family: /review + /refine (the two live-agent fork commands).
    Other _LIVE_SESSION_DIRECT_COMMANDS members are deliberately excluded:
    several map to different desktop affordances by design (/clear is a
    client action, /history a client surface, /effort//models//prompt//
    /rename route through other controls), so set-equality would be false.
    """
    live_agent_family = {"/review", "/refine"}
    desktop = _desktop_spec_names()
    missing = sorted(live_agent_family - desktop)
    assert not missing, f"live-agent commands missing from desktop registry: {missing}"
