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
