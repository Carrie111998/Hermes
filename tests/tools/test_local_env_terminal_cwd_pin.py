"""Regression tests for the local run-env TERMINAL_CWD pin (#95078).

``_make_run_env`` merges the host process environment, so a long-lived
backend that carries a process-global ``TERMINAL_CWD`` (gateway/cron bridge,
a session ``cd``, a worktree retarget) leaks that stale value into every
local subprocess. A nested ``hermes`` process inherits the correct OS cwd
from the spawn but prefers the inherited variable when resolving its agent
cwd (``agent/runtime_cwd.py``), landing in the wrong project. The fix pins
the run-env copy to the per-command effective cwd at the ``_run_bash`` spawn
point, so the child's carrier and its OS cwd always agree.
"""

from tools.environments.local import LocalEnvironment


def test_per_command_cwd_pins_terminal_cwd_env(tmp_path, monkeypatch):
    stale = tmp_path / "stale-home"
    project = tmp_path / "project"
    stale.mkdir()
    project.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(stale))

    env = LocalEnvironment(cwd=str(stale), timeout=10)
    try:
        result = env.execute("printenv TERMINAL_CWD", cwd=str(project), timeout=10)
    finally:
        env.cleanup()

    assert result["returncode"] == 0, result
    assert result["output"].strip() == str(project), (
        "a subprocess spawned with an explicit per-command cwd must not see "
        "the stale process-global TERMINAL_CWD (#95078)"
    )


def test_session_cwd_pins_over_stale_process_global(tmp_path, monkeypatch):
    # No per-command cwd: the pin falls back to the backend session cwd,
    # which is still more current than a stale process-global value.
    stale = tmp_path / "stale-home"
    session = tmp_path / "session-dir"
    stale.mkdir()
    session.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(stale))

    env = LocalEnvironment(cwd=str(session), timeout=10)
    try:
        result = env.execute("printenv TERMINAL_CWD", timeout=10)
    finally:
        env.cleanup()

    assert result["returncode"] == 0, result
    assert result["output"].strip() == str(session)


def test_backend_env_not_polluted_after_execute(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    env = LocalEnvironment(cwd=str(project), timeout=10)
    try:
        assert "TERMINAL_CWD" not in env.env, (
            "fixture assumption: the backend env dict starts without the carrier"
        )
        env.execute("true", cwd=str(project), timeout=10)
        assert "TERMINAL_CWD" not in env.env, (
            "the per-command pin must live only in the spawned run env, "
            "never mutate the backend's own environment dict"
        )
    finally:
        env.cleanup()
