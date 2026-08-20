"""Tests for venv-holder hint classification in `hermes update` preflight.

Covers #90778: `_format_venv_python_holders_message` mislabeled `hermes
dashboard` (standalone browser UI) as "Hermes Desktop backend (close the
desktop app)". `hermes serve` (the Electron backend) and `hermes dashboard`
are different things and must get different, actionable hints.

Also guards the substring-matching regression: a cmdline that merely *contains*
"serve"/"dashboard" as a substring (not a hermes_cli.main invocation) must not
be labeled at all.
"""

from hermes_cli.update_cmd import _format_venv_python_holders_message


def _msg(cmdline: str) -> str:
    return _format_venv_python_holders_message([(12345, "python.exe", cmdline)])


def test_dashboard_gets_stop_hint():
    """`hermes dashboard` must point at `hermes dashboard --stop`, not the desktop app."""
    out = _msg(r"C:\venv\Scripts\python.exe -m hermes_cli.main dashboard")
    assert "hermes dashboard --stop" in out
    assert "close the desktop app" not in out
    assert "Hermes Desktop backend" not in out


def test_serve_gets_desktop_hint():
    """`hermes serve` (Electron backend) must still say close the desktop app."""
    out = _msg(r"C:\venv\Scripts\python.exe -m hermes_cli.main serve")
    assert "Hermes Desktop backend" in out
    assert "close the desktop app" in out


def test_gateway_gets_gateway_hint():
    """`hermes_cli.main gateway` keeps its gateway hint."""
    out = _msg(r"C:\venv\Scripts\python.exe -m hermes_cli.main gateway run")
    assert "← gateway" in out


def test_substring_serve_is_not_mislabeled():
    """A non-hermes cmdline containing 'serve' must not get the desktop hint."""
    out = _msg(r"C:\projects\myserver\bin\npm run serve")
    assert "Hermes Desktop backend" not in out
    assert "close the desktop app" not in out


def test_substring_dashboard_is_not_mislabeled():
    """A cmdline containing 'dashboard' outside hermes_cli.main must not be mislabeled."""
    out = _msg(r"C:\tools\my-dashboard-monitor\app.exe --serve")
    assert "Hermes Desktop backend" not in out
    # No hint arrow at all for this cmdline (the remedy block below always
    # mentions dashboard --stop, so only the per-process hint line is checked).
    assert "←" not in out


def test_remedy_text_mentions_dashboard_stop():
    """The bottom remedy block must mention `hermes dashboard --stop`."""
    out = _msg(r"C:\venv\Scripts\python.exe -m hermes_cli.main serve")
    assert "hermes dashboard --stop" in out
