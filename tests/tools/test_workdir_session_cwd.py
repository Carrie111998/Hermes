"""Regression tests: per-command workdir= override must not mutate session cwd (#73683).

A one-off ``workdir=`` on the terminal tool permanently changed the session's
working directory because:
  1. ``env.execute()`` → ``_update_cwd()`` updated ``env.cwd`` from the
     command's end directory even when ``cwd`` was an explicit override.
  2. ``terminal_tool()`` then wrote that polluted ``env.cwd`` into the durable
     per-session record via ``record_session_cwd()``.

These tests assert that workdir= is truly per-command: the session record and
the shared env's cwd are both restored to their pre-command values afterward.
"""

import json

import pytest

import tools.terminal_tool as tt


@pytest.fixture(autouse=True)
def _clean_store(monkeypatch):
    monkeypatch.setattr(tt, "_session_cwd", {})
    monkeypatch.setattr(tt, "_task_env_overrides", {})


def _setup_terminal(monkeypatch, task_id, env):
    """Wire up terminal_tool so it runs foreground commands through *env*."""
    monkeypatch.setattr(tt, "_active_environments", {task_id: env})
    monkeypatch.setattr(tt, "_last_activity", {})
    monkeypatch.setattr(
        tt, "_get_env_config",
        lambda: {"env_type": "local", "cwd": "/default", "timeout": 60,
                 "lifetime_seconds": 3600},
    )
    monkeypatch.setattr(
        tt, "_check_all_guards",
        lambda command, env_type, **kwargs: {"approved": True},
    )


# ---------------------------------------------------------------------------
# Layer 2 + 3: session record must not be polluted by workdir=
# ---------------------------------------------------------------------------

class TestWorkdirDoesNotPolluteSessionRecord:
    def test_workdir_command_leaves_session_record_untouched(self, monkeypatch):
        """After a workdir= command, get_session_cwd must still be the
        pre-command value, not the workdir."""
        # Seed the session record as if a prior command left us in /project.
        tt.record_session_cwd("sess-a", "/project")

        class FakeEnv:
            env = {}
            cwd = "/project"

            def execute(self, command, **kwargs):
                # Simulate env's post-command tracking: cwd moves to workdir.
                self.cwd = kwargs.get("cwd", self.cwd)
                return {"output": kwargs.get("cwd", ""), "returncode": 0}

        env = FakeEnv()
        _setup_terminal(monkeypatch, "sess-a", env)

        result = json.loads(tt.terminal_tool(
            command="pwd", task_id="sess-a", workdir="/tmp",
        ))
        assert result["exit_code"] == 0
        # Session record must NOT have been polluted with /tmp.
        assert tt.get_session_cwd("sess-a") == "/project"

    def test_workdir_then_normal_command_returns_to_session_cwd(self, monkeypatch):
        """End-to-end: after a workdir= command, a subsequent plain command
        must resolve back to the session's real cwd."""
        tt.record_session_cwd("sess-a", "/project")

        resolved_cwds = []

        class FakeEnv:
            env = {}
            cwd = "/project"

            def execute(self, command, **kwargs):
                self.cwd = kwargs.get("cwd", self.cwd)
                resolved_cwds.append(kwargs.get("cwd"))
                return {"output": kwargs.get("cwd", ""), "returncode": 0}

        env = FakeEnv()
        _setup_terminal(monkeypatch, "sess-a", env)

        # Command with workdir override
        json.loads(tt.terminal_tool(
            command="pwd", task_id="sess-a", workdir="/tmp",
        ))
        # Command without workdir — should resolve to session record (/project)
        json.loads(tt.terminal_tool(
            command="pwd", task_id="sess-a",
        ))

        assert resolved_cwds == ["/tmp", "/project"]


# ---------------------------------------------------------------------------
# Layer 1 + 4: shared env's cwd must be restored after workdir=
# ---------------------------------------------------------------------------

class TestWorkdirRestoresEnvCwd:
    def test_env_cwd_restored_after_workdir_command(self, monkeypatch):
        tt.record_session_cwd("sess-a", "/project")

        class FakeEnv:
            env = {}
            cwd = "/project"

            def execute(self, command, **kwargs):
                self.cwd = kwargs.get("cwd", self.cwd)
                return {"output": kwargs.get("cwd", ""), "returncode": 0}

        env = FakeEnv()
        _setup_terminal(monkeypatch, "sess-a", env)

        json.loads(tt.terminal_tool(
            command="pwd", task_id="sess-a", workdir="/tmp",
        ))

        # env.cwd must be restored, not left at /tmp.
        assert env.cwd == "/project"

    def test_env_cwd_restored_even_when_command_changes_dir(self, monkeypatch):
        """Even if the command itself cd-s within the workdir, env.cwd must
        return to the pre-command value, not the workdir."""
        tt.record_session_cwd("sess-a", "/project")

        class FakeEnv:
            env = {}
            cwd = "/project"

            def execute(self, command, **kwargs):
                # Simulate cd into a subdir of workdir
                self.cwd = "/tmp/sub"
                return {"output": "/tmp/sub", "returncode": 0}

        env = FakeEnv()
        _setup_terminal(monkeypatch, "sess-a", env)

        json.loads(tt.terminal_tool(
            command="cd sub && pwd", task_id="sess-a", workdir="/tmp",
        ))

        assert env.cwd == "/project"
        assert tt.get_session_cwd("sess-a") == "/project"


# ---------------------------------------------------------------------------
# Layer 3: normal cd (no workdir) must still update the session record
# ---------------------------------------------------------------------------

class TestNormalCdStillRecorded:
    def test_cd_without_workdir_updates_session_record(self, monkeypatch):
        """Without workdir=, a cd must still be recorded — existing behavior
        must not regress."""
        tt.record_session_cwd("sess-a", "/project")

        class FakeEnv:
            env = {}
            cwd = "/project"

            def execute(self, command, **kwargs):
                self.cwd = "/new/dir"
                return {"output": "/new/dir", "returncode": 0}

        env = FakeEnv()
        _setup_terminal(monkeypatch, "sess-a", env)

        json.loads(tt.terminal_tool(
            command="cd /new/dir", task_id="sess-a",
        ))

        assert env.cwd == "/new/dir"
        assert tt.get_session_cwd("sess-a") == "/new/dir"

    def test_env_without_cwd_tracking_workdir_still_skips_record(self, monkeypatch):
        """An env that has no cwd attribute must not crash on workdir restore."""
        tt.record_session_cwd("sess-a", "/project")

        class FakeEnv:
            env = {}

            def execute(self, command, **kwargs):
                return {"output": "", "returncode": 0}

        env = FakeEnv()
        _setup_terminal(monkeypatch, "sess-a", env)

        result = json.loads(tt.terminal_tool(
            command="pwd", task_id="sess-a", workdir="/tmp",
        ))
        assert result["exit_code"] == 0
        assert tt.get_session_cwd("sess-a") == "/project"


# ---------------------------------------------------------------------------
# Layer 5: error-path cwd leak (#73717)
# ---------------------------------------------------------------------------
# The retry-loop early-return paths (timeout, exhausted retries) previously
# exited *before* the cwd-restore block, so a workdir= override that mutated
# env.cwd during execute() leaked past the failed call and corrupted the
# shared env for every later session bound to it. These tests assert cwd is
# restored on every retry-loop exit, success or otherwise.

class TestWorkdirEnvCwdRestoredOnErrorPaths:
    def test_workdir_restore_on_timeout(self, monkeypatch):
        """workdir= + a TimeoutError from env.execute() must still restore
        env.cwd before the function returns exit_code=124."""
        tt.record_session_cwd("sess-a", "/project")

        class FakeEnv:
            env = {}
            cwd = "/project"

            def execute(self, command, **kwargs):
                # Simulate the env's post-command cwd tracking persisting
                # the workdir before the timeout surfaces.
                self.cwd = kwargs.get("cwd", self.cwd)
                raise TimeoutError("execution exceeded timeout")

        env = FakeEnv()
        _setup_terminal(monkeypatch, "sess-a", env)

        result = json.loads(tt.terminal_tool(
            command="sleep 9999", task_id="sess-a", workdir="/tmp",
        ))

        assert result["exit_code"] == 124
        assert "timed out" in result["error"].lower()
        # env.cwd must be restored to the pre-command session value, not left
        # at /tmp (the workdir). This is the #73717 leak.
        assert env.cwd == "/project"
        # The durable session record must also be untouched.
        assert tt.get_session_cwd("sess-a") == "/project"

    def test_workdir_restore_on_retry_exhaustion(self, monkeypatch):
        """workdir= + a non-timeout exception that exhausts all retries must
        still restore env.cwd before the function returns exit_code=-1."""
        tt.record_session_cwd("sess-a", "/project")

        class FakeEnv:
            env = {}
            cwd = "/project"

            def execute(self, command, **kwargs):
                # Simulate the env's post-command cwd tracking persisting
                # the workdir before each retry raises.
                self.cwd = kwargs.get("cwd", self.cwd)
                raise RuntimeError("transient backend failure")

        env = FakeEnv()
        _setup_terminal(monkeypatch, "sess-a", env)

        result = json.loads(tt.terminal_tool(
            command="do thing", task_id="sess-a", workdir="/tmp",
        ))

        assert result["exit_code"] == -1
        assert "RuntimeError" in result["error"]
        # env.cwd must be restored to the pre-command session value, not left
        # at /tmp (the workdir) after all retries failed.
        assert env.cwd == "/project"
        # The durable session record must also be untouched.
        assert tt.get_session_cwd("sess-a") == "/project"
