"""Tests for banner toolset name normalization and skin color usage."""

from unittest.mock import patch

from rich.console import Console

import hermes_cli.banner as banner
import model_tools
import tools.mcp_tool


def test_cprint_falls_back_to_plain_print_when_prompt_toolkit_has_no_console(capsys):
    with patch(
        "prompt_toolkit.print_formatted_text",
        side_effect=RuntimeError("no console screen buffer"),
    ):
        banner.cprint("fallback text")

    assert capsys.readouterr().out == "fallback text\n"


def test_deferred_update_notice_goes_through_prompt_toolkit_channel(monkeypatch):
    """#95968: the deferred notice must not write Rich SGR bytes straight to
    stdout from its background thread — while prompt_toolkit owns the terminal
    those ESC bytes mangle into literal '?' on screen. The notice must render
    to an ANSI string and emit through cprint (print_formatted_text), never
    through console.print."""
    import threading
    from types import SimpleNamespace

    done = threading.Event()
    done.set()
    monkeypatch.setattr(banner, "_update_check_done", done)
    monkeypatch.setattr(banner, "_update_result", 449)
    monkeypatch.setattr(banner, "_deferred_update_notice_started", False)

    captured: list[str] = []
    monkeypatch.setattr(banner, "cprint", lambda text: captured.append(text))

    printed: list[str] = []
    mock_console = SimpleNamespace(
        width=80, print=lambda *a, **k: printed.append(str(a)),
    )

    banner._defer_update_notice(mock_console, max_wait=1.0)

    # The notice thread runs synchronously enough here: the event is already
    # set, so wait for the worker to finish via the capture.
    import time
    deadline = time.monotonic() + 5.0
    while not captured and time.monotonic() < deadline:
        time.sleep(0.01)

    assert len(captured) == 1
    text = captured[0]
    # Rich styles each token span separately, so strip the SGR sequences
    # before asserting on the human-readable content.
    import re
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    assert "449 commits behind" in plain
    assert "\x1b[" in text  # real ANSI escapes, not Rich markup brackets
    assert "[bold" not in text
    # The unsafe channel (background Rich console.print) must stay unused.
    assert printed == []








def test_build_welcome_banner_title_falls_back_when_no_tag():
    """Without a resolvable tag, the panel title renders as plain text (no hyperlink escape)."""
    import io
    from unittest.mock import patch as _patch
    import hermes_cli.banner as _banner
    import model_tools as _mt
    import tools.mcp_tool as _mcp

    _banner._latest_release_cache = None
    buf = io.StringIO()
    with (
        _patch.object(_mt, "check_tool_availability", return_value=(["web"], [])),
        _patch.object(_banner, "get_available_skills", return_value={}),
        _patch.object(_banner, "get_update_result", return_value=None),
        _patch.object(_mcp, "get_mcp_status", return_value=[]),
        _patch.object(_banner, "get_latest_release_tag", return_value=None),
    ):
        console = Console(file=buf, force_terminal=True, color_system="truecolor", width=160)
        _banner.build_welcome_banner(
            console=console, model="x", cwd="/tmp",
            session_id="abc123",
            tools=[{"function": {"name": "read_file"}}],
            get_toolset_for_tool=lambda n: "file",
        )

    raw = buf.getvalue()
    assert "Hermes Agent v" in raw, "Version label missing from title"
    assert "\x1b]8;" not in raw, "OSC-8 hyperlink should not be emitted without a tag"






def test_build_welcome_banner_non_moa_unchanged(tmp_path, monkeypatch):
    """A normal provider still renders the bare model slug, no MoA prefix."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir()

    with (
        patch.object(model_tools, "check_tool_availability", return_value=([], [])),
        patch.object(banner, "get_available_skills", return_value={}),
        patch.object(banner, "get_update_result", return_value=None),
        patch.object(tools.mcp_tool, "get_mcp_status", return_value=[]),
    ):
        console = Console(record=True, force_terminal=False, color_system=None, width=160)
        banner.build_welcome_banner(
            console=console,
            model="anthropic/claude-opus-4.8",
            cwd="/tmp/project",
            tools=[],
            enabled_toolsets=[],
            provider="openrouter",
        )

    out = console.export_text()
    assert "claude-opus-4.8" in out
    assert "MoA:" not in out
