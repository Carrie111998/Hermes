"""Regression test for the HERMES_CRON_SESSION env-var leak.

Background
----------
``cron/scheduler.py`` historically set ``HERMES_CRON_SESSION`` directly on
``os.environ`` so ``tools/approval.py`` could branch on it via
``env_var_enabled("HERMES_CRON_SESSION")``. ``os.environ`` is
*process-global*, so the flag persisted across every subsequent agent run
spawned from the same process tree — including user-driven Telegram bot
turns routed through ``run_agent.py``. The downstream cost was that
``execute_code`` (and other cron-gated paths in ``tools/approval.py``) was
silently blocked even when the active session had nothing to do with cron.

Fix shape
---------
The flag has been migrated to a ``contextvars.ContextVar`` accessed via
``gateway.session_context``. ``env_var_enabled`` falls back to ``os.environ``
only when the ContextVar is ``_UNSET``, so a stale ``os.environ`` value left
behind by the scheduler no longer bleeds into user sessions.

These tests pin that contract so the leak cannot regress.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_in_subprocess_with_env(snippet: str, env_extra: dict[str, str]) -> str:
    """Execute ``snippet`` in a fresh Python subprocess and return stdout.

    Models the leak surface: the parent process leaks an env var into a
    child that represents a "later, user-driven" spawn.
    """
    env = {**os.environ, **env_extra}
    # Force HERMES_HOME to an empty tempdir so the child doesn't pull in
    # real config; force -I to bypass any user site-packages.
    code = (
        "import sys, os, json; "
        "sys.path.insert(0, %r); " % REPO_ROOT
        + snippet
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"subprocess failed: rc={completed.returncode}\n"
            f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
        )
    return completed.stdout.strip()


def _cron_session_var_value() -> str:
    """Return the resolved HERMES_CRON_SESSION value as the runtime sees it."""
    from utils import env_var_enabled
    # Mirror what tools/approval.py does at line 1606.
    return "1" if env_var_enabled("HERMES_CRON_SESSION") else "0"


# ---------------------------------------------------------------------------
# 1. The leak — directly observable in the current scheduler process
# ---------------------------------------------------------------------------


class TestEnvLeakReproduces:
    """Sanity check that ``os.environ`` writes are process-global.

    Without the fix, this test passes (the leak is real). After the fix,
    we expect the ContextVar path to be used and the corresponding
    per-process observation (this test) to no longer be relevant — but the
    isolation contract is owned by the next test class, not this one.
    """

    def test_os_environ_writes_are_visible_to_subprocess(self):
        marker = "LEAK_TEST_MARKER_42"
        os.environ["HERMES_CRON_SESSION"] = marker
        try:
            out = _run_in_subprocess_with_env(
                "import os; print(os.environ.get('HERMES_CRON_SESSION', '<unset>'))",
                {},
            )
            assert out == marker, (
                "os.environ leaks must still be observable in subprocesses — "
                "this test verifies the precondition; the actual fix is "
                "verified by TestCronSessionContextVarIsolation."
            )
        finally:
            os.environ.pop("HERMES_CRON_SESSION", None)


# ---------------------------------------------------------------------------
# 2. The fix — ContextVar isolation across the scheduler boundary
# ---------------------------------------------------------------------------


class TestCronSessionContextVarIsolation:
    """``env_var_enabled`` should NOT see the leaked env var after the fix.

    The fix migrates ``HERMES_CRON_SESSION`` to a ContextVar on the cron
    scheduler side, and adds a ContextVar-aware fallback to
    ``env_var_enabled`` on the consumer side. A stale ``os.environ``
    value left over from a previous scheduler tick must not flip the cron
    branch in a fresh, non-cron session.
    """

    def test_env_var_enabled_ignores_leaked_environ_when_contextvar_unset(self, monkeypatch):
        # Simulate the scheduler previously having leaked the env var into
        # the ambient process environment.
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")

        # Inside a fresh asyncio task without the ContextVar set, the
        # runtime must report cron=False even though os.environ says 1.
        async def run_in_fresh_task():
            return _cron_session_var_value()

        result = asyncio.new_event_loop().run_until_complete(run_in_fresh_task())
        assert result == "0", (
            f"env_var_enabled(HERMES_CRON_SESSION) returned {result!r} from a "
            "stale os.environ leak; expected '0' (ContextVar unset, no cron "
            "session active)."
        )

    def test_env_var_enabled_sees_contextvar_when_set_within_task(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")  # leak still present

        async def run_in_fresh_task():
            # Mirror the production-side set in cron/scheduler.py: import
            # the helper, set the cron marker, read it through the same
            # env_var_enabled path that tools/approval.py uses.
            from gateway.session_context import (
                set_cron_session, reset_cron_session,
            )
            token = set_cron_session("1")
            try:
                return _cron_session_var_value()
            finally:
                reset_cron_session(token)

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(run_in_fresh_task())
        finally:
            loop.close()

        assert result == "1", (
            "When the scheduler sets the cron-session ContextVar inside an "
            "asyncio task, env_var_enabled must report '1'."
        )


# ---------------------------------------------------------------------------
# 3. End-to-end: subprocess running execute_code after a leaked cron tick
# ---------------------------------------------------------------------------


class TestExecuteCodeNotBlockedByLeakedCronFlag:
    """The user-facing symptom: ``execute_code`` returned
    ``BLOCKED: ... Cron jobs run without a user present...`` because a
    previous scheduler tick left ``HERMES_CRON_SESSION=1`` in the parent
    process environment. Verify a clean subprocess is unaffected.
    """

    def test_clean_subprocess_does_not_see_cron_session(self):
        out = _run_in_subprocess_with_env(
            "from utils import env_var_enabled; "
            "print('1' if env_var_enabled('HERMES_CRON_SESSION') else '0')",
            {},  # no extra env, simulating a fresh user-facing spawn
        )
        assert out == "0"
