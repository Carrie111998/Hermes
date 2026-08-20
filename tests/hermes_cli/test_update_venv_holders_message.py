"""Tests for the venv-holder message's per-process classification (#90778).

`hermes serve` (the headless backend the Electron app spawns) and
`hermes dashboard` (the standalone browser UI on port 9119) used to share
one classification branch, so a standalone dashboard was labeled "Hermes
Desktop backend (close the desktop app)" — advice that points at a
program that may not even be running, while the actual holder stays
locked. The remedy line also never mentioned `hermes dashboard --stop`.
"""

from hermes_cli.update_cmd import _format_venv_python_holders_message


def test_serve_holder_keeps_desktop_backend_hint():
    msg = _format_venv_python_holders_message(
        [(4242, "python.exe", r"C:\h\venv\Scripts\python.exe -m hermes_cli serve")]
    )
    assert "Hermes Desktop backend (close the desktop app)" in msg


def test_dashboard_holder_is_labeled_standalone_with_stop_hint():
    msg = _format_venv_python_holders_message(
        [(4243, "python.exe", r"C:\h\venv\Scripts\python.exe -m hermes_cli dashboard")]
    )
    # NOT the desktop backend — that program may not even be running.
    assert "Hermes Desktop backend" not in msg
    assert "standalone dashboard" in msg
    assert "hermes dashboard --stop" in msg


def test_gateway_holder_hint_unchanged():
    msg = _format_venv_python_holders_message(
        [(4244, "python.exe", r"C:\h\venv\Scripts\python.exe -m hermes_cli gateway run")]
    )
    assert "← gateway" in msg


def test_remedy_line_mentions_dashboard_stop():
    msg = _format_venv_python_holders_message(
        [(4243, "python.exe", r"C:\h\venv\Scripts\python.exe -m hermes_cli dashboard")]
    )
    # The remedy paragraph must name the command that clears this holder.
    assert "hermes dashboard --stop" in msg
    assert "hermes update --force-venv" in msg


def test_cmdline_that_merely_contains_serve_is_not_mislabeled():
    # `npm run serve` contains "serve" but is not the hermes backend —
    # bare substring matching mislabeled it (review feedback on #90791).
    msg = _format_venv_python_holders_message(
        [(5001, "node.exe", r"C:\Program Files\nodejs\node.exe npm run serve")]
    )
    assert "Desktop backend" not in msg
    assert "standalone dashboard" not in msg
    assert "← gateway" not in msg


def test_cmdline_that_merely_contains_dashboard_is_not_mislabeled():
    msg = _format_venv_python_holders_message(
        [(5002, "monitor.exe", r"C:\tools\my-dashboard-monitor.exe --watch")]
    )
    assert "standalone dashboard" not in msg


def test_hermes_cli_main_module_form_is_classified():
    # The desktop app spawns `-m hermes_cli.main serve|dashboard`.
    msg = _format_venv_python_holders_message(
        [(5003, "python.exe", r"C:\h\venv\Scripts\python.exe -m hermes_cli.main serve")]
    )
    assert "Hermes Desktop backend (close the desktop app)" in msg
    msg = _format_venv_python_holders_message(
        [(5004, "python.exe", r"C:\h\venv\Scripts\python.exe -m hermes_cli.main dashboard")]
    )
    assert "standalone dashboard" in msg


def test_console_script_entrypoint_form_is_classified():
    # `.../bin/hermes dashboard` (macOS/Linux entrypoint) — the token
    # before the subcommand ends with "hermes".
    msg = _format_venv_python_holders_message(
        [(5005, "hermes", "/home/u/.hermes/venv/bin/hermes dashboard")]
    )
    assert "standalone dashboard" in msg
