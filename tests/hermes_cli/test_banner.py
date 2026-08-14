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


def test_defer_update_notice_routes_through_cprint_not_raw_console():
    """Deferred update notice must render via prompt_toolkit (cprint), never
    console.print — raw ANSI to stdout gets mangled by patch_stdout's
    StdoutProxy into visible "[1;33m..." artifacts (#83969, cf. #2262/#2448)."""
    import threading
    import time
    from unittest.mock import Mock, patch

    import hermes_cli.banner as banner

    console = Mock()
    done = threading.Event()
    done.set()

    with (
        patch.object(banner, "_update_check_done", done),
        patch.object(banner, "_update_result", 5),
        patch.object(banner, "_deferred_update_notice_started", False),
        patch.object(banner, "cprint") as mock_cprint,
    ):
        banner._defer_update_notice(console, max_wait=1.0)
        deadline = time.time() + 2.0
        while time.time() < deadline and mock_cprint.call_count == 0:
            time.sleep(0.01)

    console.print.assert_not_called()
    assert mock_cprint.call_count >= 1, "deferred notice never printed via cprint"
    for call in mock_cprint.call_args_list:
        line = call.args[0]
        assert "\x1b[" in line, f"expected ANSI-rendered line, got: {line!r}"
        assert "[bold yellow]" not in line, f"rich markup leaked through: {line!r}"
