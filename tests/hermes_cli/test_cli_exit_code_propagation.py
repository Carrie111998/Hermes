"""hermes_cli.main's top-level dispatch propagates handler exit codes.

Before this fix, ``main()`` called ``args.func(args)`` and discarded the
return value, so every subcommand that returns a non-zero int for a real
error (1 = user error, 2 = usage error — the convention used by plugin CLI
commands, ``kanban``, ``migrate``, etc.) still exited the process with 0.
That silently broke scripted/cron use of any of those commands: a caller
checking ``$?`` could never see a failure.

These are subprocess-level tests (the real entry point, not a mocked
dispatch) because ``main()`` builds the entire parser inline and there is
no smaller seam to unit-test the dispatch line in isolation — the existing
convention in this test suite (see ``test_kanban_core_functionality.py``)
is the same real-subprocess approach.
"""

from __future__ import annotations

import os
import subprocess
import sys


def _run_hermes(argv: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *argv],
        capture_output=True,
        text=True,
        env=env,
    )


def _isolated_env(tmp_path) -> dict:
    """A fresh HERMES_HOME with the `crm` plugin enabled.

    `crm` is ``kind: standalone`` — opt-in via ``plugins.enabled`` — so its
    CLI subcommand isn't registered in the parser at all until enabled.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - crm\n", encoding="utf-8"
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    return env


def test_plugin_command_user_error_exits_nonzero(tmp_path):
    """A plugin CLI command's documented error exit code reaches the shell.

    The `crm` plugin returns 1 for a user error (unknown contact).
    """
    env = _isolated_env(tmp_path)
    r = _run_hermes(
        ["crm", "show", "nobody", "--store-path", str(tmp_path / "crm.json")],
        env,
    )
    assert "Unknown contact" in r.stdout
    assert r.returncode == 1, r.stdout + r.stderr


def test_plugin_command_success_exits_zero(tmp_path):
    env = _isolated_env(tmp_path)
    store = str(tmp_path / "crm.json")
    r = _run_hermes(["crm", "add", "Exit Code Test", "--store-path", store], env)
    assert r.returncode == 0, r.stdout + r.stderr

    r = _run_hermes(["crm", "list", "--store-path", store], env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Exit Code Test" in r.stdout


def test_usage_error_exits_two(tmp_path):
    """crm_command's own usage-error convention (missing subcommand)."""
    env = _isolated_env(tmp_path)
    r = _run_hermes(["crm"], env)
    assert r.returncode == 2, r.stdout + r.stderr
