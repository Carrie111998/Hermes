"""``_ACTION_LOG_DIR`` must resolve HERMES_HOME at call time, not at import.

Regression tests for the import-time ``get_hermes_home()`` snapshot bug class.
``hermes_cli/web_server.py`` used to hold ``_ACTION_LOG_DIR: Path =
get_hermes_home() / "logs"`` at module scope.  ``tests/conftest.py``'s autouse
``_hermetic_environment`` fixture redirects ``HERMES_HOME`` to a per-test
tempdir, but only *after* modules are imported — and ~30 test modules import
``hermes_cli.web_server`` at module scope, i.e. at collection time.  That armed
the two writes in ``_record_completed_action`` and ``_spawn_hermes_action``
against the developer's live ``~/.hermes/logs/`` for the whole session.

The autouse guard below is the safety net the bug class demands: for this
defect "the test fails" and "the user's live log directory was written to" are
the same event, so the guard refuses to let any test in this file run while the
effective directory sits under the real home.  It reads through the seam when
one exists and the bare constant when it does not, so it also protects a bisect
onto the unfixed tree.
"""

from pathlib import Path

import pytest

from hermes_cli import web_server as ws


WEB_SERVER_SOURCE = Path(ws.__file__)


def _effective_action_log_dir() -> Path:
    """Resolve the action-log dir through the seam, or the pre-fix constant."""
    resolver = getattr(ws, "_action_log_dir", None)
    if resolver is not None:
        return Path(resolver())
    return Path(ws._ACTION_LOG_DIR)


@pytest.fixture(autouse=True)
def _never_write_to_the_real_hermes_home():
    """Refuse to run if the action log would land in the user's live home."""
    forbidden = [
        Path.home() / ".hermes",
        Path.home() / ".hermes" / "profiles" / "main",
    ]
    resolved = _effective_action_log_dir().expanduser().resolve(strict=False)
    for root in forbidden:
        root = root.expanduser().resolve(strict=False)
        if resolved == root or root in resolved.parents:
            pytest.fail(
                "REFUSING TO RUN: the action log directory resolves to "
                f"{resolved}, which is inside the real Hermes home {root}. "
                "Export a throwaway HERMES_HOME before running these tests."
            )
    yield


class TestSeamShape:
    def test_module_constant_is_a_none_sentinel(self):
        """The snapshot is gone; the constant survives only as an override."""
        assert ws._ACTION_LOG_DIR is None

    def test_no_use_site_dereferences_the_constant_directly(self):
        """Every body reference goes through the resolver.

        Sliced *after* the resolver definition so the seam's own override
        branch (``Path(_ACTION_LOG_DIR)``) cannot make this vacuous or false.
        """
        source = WEB_SERVER_SOURCE.read_text(encoding="utf-8")
        marker = "def _action_log_dir()"
        assert marker in source, "the resolver seam is missing"
        body = source[source.index(marker) :]
        end_of_resolver = body.index("_ACTION_LOG_TAIL_MAX_BYTES")
        remainder = body[end_of_resolver:]
        assert "_ACTION_LOG_DIR.mkdir" not in remainder
        assert "_ACTION_LOG_DIR /" not in remainder


class TestResolution:
    def test_follows_hermes_home_changed_after_import(self, tmp_path, monkeypatch):
        """The whole point: a post-import HERMES_HOME change is honoured."""
        later = tmp_path / "flipped"
        monkeypatch.setenv("HERMES_HOME", str(later))
        assert ws._action_log_dir() == later / "logs"

    def test_explicit_override_wins(self, tmp_path, monkeypatch):
        """Existing tests pinning the constant to a Path keep working."""
        pinned = tmp_path / "pinned"
        monkeypatch.setattr(ws, "_ACTION_LOG_DIR", pinned)
        assert ws._action_log_dir() == pinned

    def test_string_override_is_coerced_to_path(self, tmp_path, monkeypatch):
        pinned = tmp_path / "as-str"
        monkeypatch.setattr(ws, "_ACTION_LOG_DIR", str(pinned))
        assert ws._action_log_dir() == pinned

    def test_ignores_the_context_local_profile_override(self, tmp_path, monkeypatch):
        """Pins the resolver choice: process home, not ``get_hermes_home()``.

        An action log belongs to the dashboard process — no write call site
        runs inside ``_profile_scope``, and the requested profile is carried
        into the spawned child via ``-p`` instead.  A resolver that followed
        the context override would scatter action logs across profile homes.
        """
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        launch_home = tmp_path / "launch"
        other_profile = tmp_path / "profiles" / "secondary"
        monkeypatch.setenv("HERMES_HOME", str(launch_home))

        token = set_hermes_home_override(str(other_profile))
        try:
            assert ws._action_log_dir() == launch_home / "logs"
        finally:
            reset_hermes_home_override(token)


class TestWritesFollowTheLiveHome:
    def test_record_completed_action_writes_under_the_current_home(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "live"
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(ws, "_ACTION_LOG_DIR", None)

        ws._record_completed_action("hermes-update", "guidance message", exit_code=1)

        written = home / "logs" / ws._ACTION_LOG_FILES["hermes-update"]
        assert written.exists()
        assert "guidance message" in written.read_text(encoding="utf-8")

        ws._ACTION_PROCS.pop("hermes-update", None)
        ws._ACTION_RESULTS.pop("hermes-update", None)

    def test_spawn_hermes_action_writes_under_the_current_home(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "live"
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(ws, "_ACTION_LOG_DIR", None)

        class _FakeProc:
            pid = 4321

        monkeypatch.setattr(ws.subprocess, "Popen", lambda cmd, **kw: _FakeProc())

        ws._spawn_hermes_action(["doctor"], "doctor")

        written = home / "logs" / ws._ACTION_LOG_FILES["doctor"]
        assert written.exists()
        assert "doctor started" in written.read_text(encoding="utf-8")

        ws._ACTION_PROCS.pop("doctor", None)
        ws._ACTION_COMMANDS.pop("doctor", None)

    def test_status_read_follows_the_same_home_as_the_write(
        self, tmp_path, monkeypatch
    ):
        """The read at ``/api/actions/{name}/status`` must not split away."""
        home = tmp_path / "live"
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(ws, "_ACTION_LOG_DIR", None)

        ws._record_completed_action("doctor", "tail me", exit_code=1)

        assert ws._action_log_dir() == home / "logs"
        tail = ws._tail_lines(
            ws._action_log_dir() / ws._ACTION_LOG_FILES["doctor"], 50
        )
        assert any("tail me" in line for line in tail)

        ws._ACTION_PROCS.pop("doctor", None)
        ws._ACTION_RESULTS.pop("doctor", None)
