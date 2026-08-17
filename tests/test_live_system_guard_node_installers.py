"""The live-system guard must block real package installers in a test run.

This is the tripwire for a defect class that cost the nightly gate a whole
file. Production install call sites were converted from ``subprocess.run`` to
``hermes_cli._subprocess_compat.run_text_capture`` — correctly, because ``npm``
is ``npm.cmd`` on Windows, so the direct child is ``cmd.exe`` and the install
itself runs in a node grandchild that inherits the capture pipes, which
defeats ``subprocess.run``'s ``timeout`` outright. But the tests still patched
``subprocess.run``, so they stopped intercepting anything and reached a REAL
``npm install``. Some merely failed an assertion; in
``tests/gateway/test_whatsapp_connect.py`` the run wedged in
``run_text_capture -> proc.wait -> WaitForSingleObject`` until pytest-timeout
killed the process — which the gate reads as "no tests ran".

Re-pointing the mocks fixes today's instance. These tests are what stops the
next one: the guard sits at ``subprocess.Popen``, *below* whichever helper a
call site happens to use, so a future conversion cannot silently reintroduce
this. A mock that stops intercepting now fails loudly and instantly instead of
quietly shelling out to the network.
"""

import subprocess
import sys

import pytest

from hermes_cli._subprocess_compat import run_text_capture

_GUARD = "live-system guard"


def test_bare_npm_argv_is_blocked():
    with pytest.raises(RuntimeError, match=_GUARD):
        run_text_capture(["npm", "install", "--silent"], timeout=5)


def test_windows_npm_cmd_path_is_blocked():
    """The real argv is an absolute ``npm.cmd`` path, not the bare word.

    ``find_node_executable("npm")`` resolves to the Hermes-managed portable
    Node, so the guard has to match on the basename with the ``.cmd`` suffix
    stripped. Tokenising this argv with ``shlex`` in posix mode eats the
    backslashes and defeats the check — the first draft of the guard failed
    exactly here, so this case is pinned.
    """
    with pytest.raises(RuntimeError, match=_GUARD):
        run_text_capture(
            [r"C:\Users\somebody\.hermes\node\npm.cmd", "install", "--silent"],
            timeout=5,
        )


def test_npm_ci_is_blocked():
    with pytest.raises(RuntimeError, match=_GUARD):
        run_text_capture(["npm", "ci"], timeout=5)


def test_shell_wrapped_npm_is_blocked():
    with pytest.raises(RuntimeError, match=_GUARD):
        run_text_capture("cmd /c npm install", timeout=5, shell=True)


def test_curl_pipe_sh_is_blocked():
    """``hermes_cli.web_server._run_setup_command`` runs provider-supplied
    shell snippets; one of them pipes a downloaded script into a shell."""
    with pytest.raises(RuntimeError, match=_GUARD):
        run_text_capture(
            "curl -fsSL https://byterover.dev/install.sh | sh",
            timeout=5,
            shell=True,
        )


def test_guard_also_covers_plain_subprocess_run():
    """The guard is helper-agnostic — it sits under both spawn paths."""
    with pytest.raises(RuntimeError, match=_GUARD):
        subprocess.run(["npm", "ci"], capture_output=True, timeout=5)


def test_unrelated_command_still_runs():
    """The guard must be surgical: only installers, not all subprocesses."""
    result = run_text_capture(["cmd", "/c", "echo", "ok"], timeout=30)
    assert result.returncode == 0


def test_command_merely_mentioning_npm_is_not_blocked():
    """A command that names npm in an argument is not an npm invocation."""
    result = run_text_capture(
        ["cmd", "/c", "echo", "run npm install to continue"], timeout=30
    )
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# PowerShell download-and-execute
# ---------------------------------------------------------------------------
#
# Same hazard as ``curl … | sh``, but invisible to _is_remote_installer_pipe
# for two independent reasons, both real:
#
#   1. ``irm`` (Invoke-RestMethod) was simply absent from _REMOTE_FETCH_HEADS,
#      which listed only curl/wget/iwr/invoke-webrequest.
#   2. Even with it listed, the detector requires the fetch verb to HEAD a
#      pipeline segment. In ``powershell -Command "irm … | iex"`` the whole
#      pipeline lives inside one argv token, so splitting on "|" leaves
#      ``powershell`` heading segment 0 and ``irm`` never heads anything.
#
# This is not hypothetical. On 2026-08-16, writing the RED test for the
# cua-driver capture-pipe fix, a stub pointed at a not-yet-called seam let
# `hermes_cli/tools_config.py`'s real
# ``powershell -Command "irm …/install.ps1 | iex"`` execute against the
# network and hang the session. The guard sat right underneath and said
# nothing.
#
# Every URL below uses a `.invalid` host (RFC 2606 — guaranteed not to
# resolve), so if the guard ever stops firing these tests fail fast instead of
# fetching and executing a remote script the way the incident did.

_PS_INSTALL_URL = (
    "https://raw.githubusercontent.invalid/trycua/cua/main/"
    "libs/cua-driver/scripts/install.ps1"
)


def test_powershell_irm_pipe_iex_is_blocked():
    """The exact shape hermes_cli/tools_config.py hands the cua-driver."""
    with pytest.raises(RuntimeError, match=_GUARD):
        run_text_capture(
            [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-Command", f"irm {_PS_INSTALL_URL} | iex",
            ],
            timeout=5,
        )


def test_powershell_iex_wrapping_irm_is_blocked():
    """``iex (irm …)`` has no pipe at all, so a "|"-based check cannot see it."""
    with pytest.raises(RuntimeError, match=_GUARD):
        run_text_capture(
            ["pwsh", "-NoProfile", "-Command", f"iex (irm {_PS_INSTALL_URL})"],
            timeout=5,
        )


def test_powershell_downloadstring_iex_is_blocked():
    """The pre-``irm`` idiom, still all over the internet."""
    with pytest.raises(RuntimeError, match=_GUARD):
        run_text_capture(
            [
                "powershell", "-Command",
                f"iex ((New-Object Net.WebClient).DownloadString('{_PS_INSTALL_URL}'))",
            ],
            timeout=5,
        )


def test_powershell_encoded_command_is_blocked():
    """``-EncodedCommand`` is base64 UTF-16LE — the obvious way past a
    substring check on the argv. Decode before deciding, or the guard is
    one flag away from being bypassed."""
    import base64

    script = f"iex (irm {_PS_INSTALL_URL})"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    with pytest.raises(RuntimeError, match=_GUARD):
        run_text_capture(
            ["powershell", "-NoProfile", "-EncodedCommand", encoded],
            timeout=5,
        )


def test_powershell_shell_string_form_is_blocked():
    with pytest.raises(RuntimeError, match=_GUARD):
        run_text_capture(
            f'powershell -NoProfile -Command "irm {_PS_INSTALL_URL} | iex"',
            timeout=5, shell=True,
        )


# --- the other half: this must not become a blanket ban on PowerShell -------
#
# Three real in-repo callers pass -Command scripts and must keep working:
# hermes_cli/claw.py:96 (Get-CimInstance), session_bridge/mcp_server.py:1273
# (SID lookup + ConvertTo-Json), tools/environments/local.py:749
# (Get-ProcessMitigation). None fetches anything. The predicate therefore
# requires ALL THREE of: a URL, a fetch cmdlet, and an exec cmdlet.

@pytest.mark.skipif(
    not sys.platform.startswith("win"), reason="powershell.exe is Windows-only"
)
def test_plain_powershell_command_is_not_blocked():
    run_text_capture(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", "exit 0"],
        timeout=60,
    )


@pytest.mark.skipif(
    not sys.platform.startswith("win"), reason="powershell.exe is Windows-only"
)
def test_powershell_merely_printing_a_url_is_not_blocked():
    """A URL alone must not trip it, or every diagnostic that echoes a link
    becomes unrunnable."""
    run_text_capture(
        [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "Write-Output 'docs at https://example.invalid/help'",
        ],
        timeout=60,
    )
