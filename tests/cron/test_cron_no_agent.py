"""Tests for cronjob no_agent mode — script-driven jobs that skip the LLM.

Covers:

* ``create_job(no_agent=True)`` shape, validation, and serialization.
* ``cronjob(action='create', no_agent=True)`` tool-level validation.
* ``cronjob(action='update')`` flipping no_agent on/off.
* ``scheduler.run_job`` short-circuit path: success/silent/failure.
* Shell script support in ``_run_job_script`` (.sh runs via bash).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs/scripts don't leak."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))

    # Reload modules that cache get_hermes_home() at import time.
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)

    return home


# ---------------------------------------------------------------------------
# create_job / update_job: data-layer semantics
# ---------------------------------------------------------------------------


def test_create_job_no_agent_requires_script(hermes_env):
    from cron.jobs import create_job

    with pytest.raises(ValueError, match="no_agent=True requires a script"):
        create_job(prompt=None, schedule="every 5m", no_agent=True)


def test_update_job_roundtrips_no_agent_flag(hermes_env):
    from cron.jobs import create_job, update_job, get_job

    script_path = hermes_env / "scripts" / "w.sh"
    script_path.write_text("echo hi\n")
    job = create_job(prompt=None, schedule="every 5m", script="w.sh", no_agent=True, deliver="local")

    update_job(job["id"], {"no_agent": False})
    reloaded = get_job(job["id"])
    assert reloaded["no_agent"] is False

    update_job(job["id"], {"no_agent": True})
    reloaded = get_job(job["id"])
    assert reloaded["no_agent"] is True


# ---------------------------------------------------------------------------
# cronjob tool: API-layer validation
# ---------------------------------------------------------------------------


def test_cronjob_tool_create_no_agent_without_script_errors(hermes_env):
    from tools.cronjob_tools import cronjob

    result = json.loads(
        cronjob(action="create", schedule="every 5m", no_agent=True, deliver="local")
    )
    assert result.get("success") is False
    assert "no_agent=True requires a script" in result.get("error", "")


# ---------------------------------------------------------------------------
# scheduler.run_job: short-circuit behavior
# ---------------------------------------------------------------------------


def test_run_job_no_agent_success_returns_script_stdout(hermes_env):
    """Happy path: script exits 0 with output, delivered verbatim."""
    from cron.jobs import create_job
    from cron.scheduler import run_job

    script_path = hermes_env / "scripts" / "alert.sh"
    script_path.write_text("#!/bin/bash\necho 'RAM 92% on host'\n")

    job = create_job(
        prompt=None, schedule="every 5m", script="alert.sh", no_agent=True, deliver="local"
    )
    success, doc, final_response, error = run_job(job)
    assert success is True
    assert error is None
    assert "RAM 92% on host" in final_response
    assert "RAM 92% on host" in doc


# ---------------------------------------------------------------------------
# _run_job_script: shell-script support
# ---------------------------------------------------------------------------


def test_run_job_script_path_traversal_still_blocked(hermes_env):
    """Security regression: shell-script support must NOT loosen containment."""
    from cron.scheduler import _run_job_script

    # Absolute path outside the scripts dir should be rejected.
    ok, output = _run_job_script("/etc/passwd")
    assert ok is False
    assert "Blocked" in output or "outside" in output


# ---------------------------------------------------------------------------
# _run_job_script: bash resolution must go through the shared Windows-aware
# resolver, not a raw PATH lookup (issue #46332 — WSL launcher picked over
# Git Bash on native Windows).
# ---------------------------------------------------------------------------


class TestRunJobScriptBashResolver:
    """Regression tests for the .sh/.bash interpreter resolution (Layer 1 of
    issue #46332): cron jobs must use the shared ``_find_bash()`` resolver
    (portable Git -> Git for Windows -> PATH-with-start-probe), never a raw
    ``shutil.which("bash")`` that lands on the System32 WSL launcher."""

    def _write_script(self, hermes_env) -> str:
        script_path = hermes_env / "scripts" / "cron-resolver.sh"
        script_path.write_text("#!/bin/bash\necho resolver ok\n")
        return str(script_path)

    def _fake_run(self, captured):
        class _FakeResult:
            returncode = 0
            stdout = "resolver ok\n"
            stderr = ""

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return _FakeResult()

        return fake_run

    def test_shared_resolver_beats_raw_which_on_windows(
        self, hermes_env, monkeypatch
    ):
        """The bug: raw shutil.which('bash') on Windows returns the WSL
        launcher (C:\\Windows\\System32\\bash.exe). The fix: the shared
        resolver's answer must win."""
        from cron.scheduler import _run_job_script
        import tools.environments.local as local_mod

        script = self._write_script(hermes_env)
        git_bash = "C:/Program Files/Git/bin/bash.exe"

        # Raw PATH lookup would return the WSL launcher — must be ignored.
        monkeypatch.setattr(
            "cron.scheduler.shutil.which", lambda name: r"C:\Windows\System32\bash.exe"
        )
        monkeypatch.setattr(local_mod, "_find_bash", lambda: git_bash)

        captured = {}
        monkeypatch.setattr(
            "cron.scheduler.subprocess.run", self._fake_run(captured)
        )

        ok, _ = _run_job_script(script)
        assert ok is True
        assert captured["argv"][0] == git_bash

    def test_runtime_error_from_resolver_gives_clear_error(
        self, hermes_env, monkeypatch
    ):
        """No usable bash anywhere (only a WSL stub that cannot start): the
        resolver raises RuntimeError and the job reports the actionable
        message instead of spawning the stub."""
        from cron.scheduler import _run_job_script
        import tools.environments.local as local_mod

        script = self._write_script(hermes_env)

        def _no_bash():
            raise RuntimeError("no usable bash")

        monkeypatch.setattr(local_mod, "_find_bash", _no_bash)

        ok, message = _run_job_script(script)
        assert ok is False
        assert "bash not found" in message

    def test_unexpected_resolver_failure_falls_back_to_path_lookup(
        self, hermes_env, monkeypatch
    ):
        """A non-RuntimeError resolver failure (e.g. module unavailable in an
        embedded context) degrades to the historical PATH lookup."""
        from cron.scheduler import _run_job_script
        import tools.environments.local as local_mod

        script = self._write_script(hermes_env)

        def _boom():
            raise ImportError("module unavailable")

        monkeypatch.setattr(local_mod, "_find_bash", _boom)
        monkeypatch.setattr(
            "cron.scheduler.shutil.which", lambda name: "/usr/bin/bash"
        )

        captured = {}
        monkeypatch.setattr(
            "cron.scheduler.subprocess.run", self._fake_run(captured)
        )

        ok, _ = _run_job_script(script)
        assert ok is True
        assert captured["argv"][0] == "/usr/bin/bash"
