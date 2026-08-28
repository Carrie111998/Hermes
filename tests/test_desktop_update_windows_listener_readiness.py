"""Source contract for the Windows Desktop update progress listener.

The UI self-test prints its loopback URL as soon as ``Start-UiServer`` returns.
``PowerShell.BeginInvoke`` only queues the server runspace, so a mere main-thread
return is not enough: on a busy Windows runner, clients can receive the URL while
no accept operation has been registered and the progress poll times out.

This check runs on every OS because it guards the PowerShell hand-off structure;
the executable proof remains the ``windows_only`` progress integration test.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WINDOWS_PS1 = REPO_ROOT / "scripts" / "desktop-update" / "windows.ps1"


def _start_ui_server_source() -> str:
    source = WINDOWS_PS1.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = re.search(
        r"function Start-UiServer\(\[string\]\$HtmlPath\) \{(?P<body>.*?)\n\}\n\nfunction Stop-UiServer",
        source,
        re.DOTALL,
    )
    assert match, "Expected Start-UiServer followed by Stop-UiServer in windows.ps1."
    return match.group("body")


def _show_progress_window_source() -> str:
    source = WINDOWS_PS1.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = re.search(
        r"function Show-ProgressWindow \{(?P<body>.*?)\n\}\n\nfunction ",
        source,
        re.DOTALL,
    )
    assert match, "Expected a complete Show-ProgressWindow function in windows.ps1."
    return match.group("body")


def test_listener_accept_is_armed_before_start_ui_server_reports_ready() -> None:
    source = _start_ui_server_source()

    begin = source.find("$Listener.BeginAcceptTcpClient(")
    ready = source.find("$Ready.Set()")
    end = source.find("$Listener.EndAcceptTcpClient($accept)")

    assert begin >= 0, (
        "The dedicated progress runspace must arm BeginAcceptTcpClient before "
        "Start-UiServer may publish its loopback URL."
    )
    assert ready >= 0, "The server runspace must signal listener readiness."
    assert end >= 0, "The armed accept must be completed with EndAcceptTcpClient."
    assert begin < ready < end, (
        "Signal readiness only after a pending accept has been registered and "
        "before waiting to complete that accept; BeginInvoke alone is not a "
        "readiness guarantee on a loaded Windows runner."
    )


def test_browser_launch_failure_uses_full_ui_server_teardown() -> None:
    source = _show_progress_window_source()
    primary = source[source.find("if ($server) {") : source.find("# fall through to WinForms")]

    published = primary.find("$script:UiServer = $server")
    launched = primary.find("$server.BrowserProc = Start-Process")
    assert published >= 0 and published < launched, (
        "Publish the server to Stop-UiServer before browser launch so a launch "
        "exception can dispose its listener, runspace, PowerShell pipeline, and Ready event."
    )
    assert re.search(r"catch\s*\{\s*Stop-UiServer\s*$", primary, re.MULTILINE), (
        "Browser-launch fallback must use Stop-UiServer rather than stopping only the listener."
    )
