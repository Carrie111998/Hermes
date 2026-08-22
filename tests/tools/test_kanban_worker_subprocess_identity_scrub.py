"""Regression: a kanban worker's own subprocess spawn must not self-register
as a NEW dispatcher run against the SAME task (t_30fe3850).

Root cause: when a kanban worker used its ``terminal`` tool to shell out to
another ``hermes`` CLI invocation (e.g. probing a tool not in its own
profile's toolset via ``hermes chat -t <tool> -q ...``), that subprocess
inherited ``HERMES_KANBAN_TASK``/``HERMES_KANBAN_RUN_ID``/``HERMES_KANBAN_BOARD``
from the parent shell env. The nested CLI process then treated those vars as
its own dispatcher-assigned identity: ``_default_task_id()`` resolved a task
id from the environment, ``_check_kanban_mode()`` decided it was a worker,
and it posted spurious ``kanban_block``/comments against the real card,
burning the task's block-loop budget (see t_1b6aa6fc runs 223-224).

Fix: every subprocess env builder behind the terminal tool
(``_sanitize_subprocess_env`` for background/PTY spawns, ``_make_run_env``
for the foreground bash spawn, and ``hermes_subprocess_env`` for the
non-terminal spawn surface) now unconditionally strips the run-identity
subset of Kanban env vars (``KANBAN_IDENTITY_ENV_KEYS``:
TASK/RUN_ID/CLAIM_LOCK/BOARD/DB) via ``_scrub_delegated_child_kanban_env``,
regardless of ``delegate_task`` lineage. ``HERMES_KANBAN_WORKSPACE`` is
deliberately preserved: it is a directory-reference convenience the worker
protocol relies on (``cd $HERMES_KANBAN_WORKSPACE``), not run identity.
"""

from __future__ import annotations

import os

import pytest


WORKER_IDENTITY_ENV = {
    "HERMES_KANBAN_TASK": "t_parent_real_task",
    "HERMES_KANBAN_RUN_ID": "229",
    "HERMES_KANBAN_CLAIM_LOCK": "lock-xyz",
    "HERMES_KANBAN_BOARD": "vps-migration",
    "HERMES_KANBAN_DB": "/root/.hermes/kanban/boards/vps-migration/kanban.db",
    # Deliberately NOT stripped by the identity scrub — directory reference,
    # not run identity; the worker protocol needs it preserved.
    "HERMES_KANBAN_WORKSPACE": "/srv/repos/saylent-swarm/.worktrees/t_parent",
}


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for key, value in WORKER_IDENTITY_ENV.items():
        monkeypatch.setenv(key, value)
    yield


class TestForegroundTerminalSpawn:
    """`_make_run_env` backs LocalEnvironment.execute — the ordinary
    foreground `terminal` tool call."""

    def test_identity_vars_stripped_from_subprocess_own_hermes_cli_call(self):
        from tools.environments.local import _make_run_env

        env = _make_run_env({})

        for key in (
            "HERMES_KANBAN_TASK",
            "HERMES_KANBAN_RUN_ID",
            "HERMES_KANBAN_CLAIM_LOCK",
            "HERMES_KANBAN_BOARD",
            "HERMES_KANBAN_DB",
        ):
            assert key not in env, (
                f"{key} leaked into a worker's own subprocess — a nested "
                "`hermes` CLI call would self-register as a new run of the "
                "SAME task (t_30fe3850 regression)"
            )

    def test_workspace_path_preserved_for_cd_convention(self):
        """HERMES_KANBAN_WORKSPACE is a directory reference, not identity —
        the worker protocol tells workers to `cd $HERMES_KANBAN_WORKSPACE`
        in their own terminal calls, so it must survive."""
        from tools.environments.local import _make_run_env

        env = _make_run_env({})
        assert env.get("HERMES_KANBAN_WORKSPACE") == WORKER_IDENTITY_ENV[
            "HERMES_KANBAN_WORKSPACE"
        ]


class TestBackgroundPtySpawn:
    """`_sanitize_subprocess_env` backs process_registry.spawn_local — the
    `background=True` / PTY terminal tool path."""

    def test_identity_vars_stripped(self):
        from tools.environments.local import _sanitize_subprocess_env

        env = _sanitize_subprocess_env(dict(os.environ))

        for key in (
            "HERMES_KANBAN_TASK",
            "HERMES_KANBAN_RUN_ID",
            "HERMES_KANBAN_CLAIM_LOCK",
            "HERMES_KANBAN_BOARD",
            "HERMES_KANBAN_DB",
        ):
            assert key not in env

    def test_workspace_path_preserved(self):
        from tools.environments.local import _sanitize_subprocess_env

        env = _sanitize_subprocess_env(dict(os.environ))
        assert env.get("HERMES_KANBAN_WORKSPACE") == WORKER_IDENTITY_ENV[
            "HERMES_KANBAN_WORKSPACE"
        ]


class TestNonTerminalSpawnSurface:
    """`hermes_subprocess_env` backs browser/ACP/CLI-executor spawns."""

    def test_identity_vars_stripped(self):
        from tools.environments.local import hermes_subprocess_env

        env = hermes_subprocess_env(inherit_credentials=True)

        for key in (
            "HERMES_KANBAN_TASK",
            "HERMES_KANBAN_RUN_ID",
            "HERMES_KANBAN_CLAIM_LOCK",
            "HERMES_KANBAN_BOARD",
            "HERMES_KANBAN_DB",
        ):
            assert key not in env


class TestDelegatedChildStillGetsFullScrub:
    """delegate_task children keep the pre-existing, stricter full scrub
    (also strips WORKSPACE + stamps the lineage marker) — unchanged
    behaviour, just routed through the same entry point now."""

    def test_full_scrub_when_delegated_child(self):
        from agent.delegation_context import delegated_child_context
        from tools.environments.local import hermes_subprocess_env

        with delegated_child_context():
            env = hermes_subprocess_env(inherit_credentials=True)

        assert env["HERMES_DELEGATED_CHILD_CONTEXT"] == "1"
        assert "HERMES_KANBAN_TASK" not in env
        assert "HERMES_KANBAN_WORKSPACE" not in env


class TestEndToEndRegressionScenario:
    """Mirrors the exact repro from t_1b6aa6fc: a worker spawns a real `hermes`
    CLI subprocess to probe a tool outside its own profile's toolset."""

    def test_spawned_hermes_cli_env_carries_no_task_identity(self, monkeypatch):
        """Simulates `terminal(command='hermes chat -t magnific -q ...')`:
        the env handed to the child subprocess must not resolve a task id via
        `_default_task_id()`'s env fallback."""
        from tools.environments.local import _make_run_env

        child_env = _make_run_env({})

        # This is the exact check kanban_tools._default_task_id() performs
        # against the child's os.environ once it boots.
        assert child_env.get("HERMES_KANBAN_TASK") is None, (
            "a subprocess spawned by a kanban worker must not be able to "
            "resolve the parent's task id and self-register a new run "
            "against it"
        )
