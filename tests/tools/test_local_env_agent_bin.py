"""Tests for the opt-in ``$HERMES_HOME/agent-bin`` PATH override slot.

``agent-bin`` is a Hermes-owned prepend slot for the subprocess PATH the
terminal tool (and the cron / background / PTY spawn path) builds. Dropping a
shim there — a ``claude`` wrapper, a sandbox launcher, a telemetry shim — makes
it win over the same-named CLI everywhere the agent shells out.

Two properties matter and are asserted by calling the real functions:

1. **Opt-in.** With no ``agent-bin`` directory the produced PATH is
   byte-identical to what the untouched sibling helpers produce, so a default
   install pays nothing.
2. **Head of PATH.** With the directory present nothing precedes it — not the
   hermes install dir, not any caller-supplied entry — because the whole point
   is operator interception.

The managed runtime dirs (``$HERMES_HOME/bin``, node) are deliberately
*appended* instead, so "a tool the user deliberately put on their own PATH
still wins" (25d0bcd4). These tests pin the hermes install dir and neutralise
the Git Bash dir prepend so the asserted PATH layout is deterministic on every
host rather than depending on whether a real ``hermes`` is on the runner's PATH.
"""

import os
import sys
from unittest.mock import patch

import pytest

from hermes_constants import get_hermes_home
from tools.environments import local as local_mod
from tools.environments.local import (
    _make_run_env,
    _prepend_agent_bin_dir,
    _sanitize_subprocess_env,
)

#: Stand-in for the resolved hermes console-script dir. Pinned so ordering
#: assertions don't depend on the runner having a real ``hermes`` installed.
HERMES_BIN_DIR = "/opt/hermes/bin"

#: Caller-supplied PATH used by every test, so "before every caller-PATH
#: entry" is a concrete assertion rather than a shape check.
CALLER_PATH = os.pathsep.join(["/usr/bin", "/bin"])

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: the agent-bin prepend is a documented no-op on Windows",
)


@pytest.fixture(autouse=True)
def _deterministic_path_layout(monkeypatch):
    """Pin the two other PATH contributors so agent-bin's position is testable."""
    monkeypatch.setattr(local_mod, "_HERMES_BIN_DIR", HERMES_BIN_DIR)
    monkeypatch.setattr(local_mod, "_git_bash_bin_dirs", lambda: [])


@pytest.fixture
def agent_bin() -> str:
    """Create ``$HERMES_HOME/agent-bin`` and return its path.

    The autouse ``_hermetic_environment`` fixture already points HERMES_HOME at
    a per-test tempdir, so this creates the real directory the helper looks for
    rather than mocking the lookup.
    """
    directory = get_hermes_home() / "agent-bin"
    directory.mkdir()
    return str(directory)


def _run_env_path(env: dict | None = None) -> str:
    """Return the PATH ``_make_run_env`` builds from ``CALLER_PATH``.

    HERMES_HOME is carried into the cleared environ because the helper resolves
    ``get_hermes_home()`` from the process environment.
    """
    process_env = {
        "PATH": CALLER_PATH,
        "HERMES_HOME": os.environ["HERMES_HOME"],
    }
    with patch.dict(os.environ, process_env, clear=True):
        return _make_run_env(env or {})["PATH"]


class TestDefaultInstallIsUnchanged:
    """No agent-bin dir → the PATH string is what it was before this seam."""

    @posix_only
    def test_make_run_env_path_is_byte_identical(self):
        # Rebuild the pre-change pipeline from the untouched sibling helpers:
        # sane-path merge, then the hermes install dir prepend.
        expected = local_mod._prepend_hermes_bin_dir(
            local_mod._append_missing_sane_path_entries(CALLER_PATH)
        )

        assert _run_env_path() == expected

    @posix_only
    def test_sanitize_subprocess_env_path_is_byte_identical(self):
        expected = local_mod._prepend_hermes_bin_dir(CALLER_PATH)

        sanitized = _sanitize_subprocess_env({"PATH": CALLER_PATH})

        assert sanitized["PATH"] == expected

    @posix_only
    def test_no_empty_entries_are_introduced(self):
        """A PATH with empty components must not gain one from this seam.

        An empty PATH element means "current working directory" to a POSIX
        shell — the foot-gun ``_append_missing_sane_path_entries`` already
        strips. Assert that stays true on the default path.
        """
        assert "" not in _run_env_path().split(os.pathsep)


class TestAgentBinTakesPrecedence:
    """agent-bin present → nothing precedes it."""

    @posix_only
    def test_make_run_env_puts_agent_bin_first(self, agent_bin):
        entries = _run_env_path().split(os.pathsep)

        assert entries[0] == agent_bin

    @posix_only
    def test_make_run_env_beats_hermes_install_dir_and_caller_entries(self, agent_bin):
        entries = _run_env_path().split(os.pathsep)

        for later in (HERMES_BIN_DIR, "/usr/bin", "/bin"):
            assert entries.index(agent_bin) < entries.index(later), (
                f"{later} must not precede the agent-bin override dir"
            )

    @posix_only
    def test_sanitize_subprocess_env_puts_agent_bin_first(self, agent_bin):
        """Cron / background / PTY spawns get the same override slot."""
        sanitized = _sanitize_subprocess_env({"PATH": CALLER_PATH})
        entries = sanitized["PATH"].split(os.pathsep)

        assert entries[0] == agent_bin
        assert entries.index(agent_bin) < entries.index(HERMES_BIN_DIR)

    @posix_only
    def test_appears_after_the_directory_is_created_mid_process(self):
        """The dir is resolved per call, so an operator can add it live."""
        before = _run_env_path().split(os.pathsep)
        assert not any(entry.endswith("agent-bin") for entry in before)

        directory = get_hermes_home() / "agent-bin"
        directory.mkdir()

        assert _run_env_path().split(os.pathsep)[0] == str(directory)

    @posix_only
    def test_no_empty_entries_are_introduced(self, agent_bin):
        assert "" not in _run_env_path().split(os.pathsep)


class TestPrependHelper:
    """``_prepend_agent_bin_dir`` in isolation."""

    @posix_only
    def test_noop_when_directory_is_missing(self):
        assert _prepend_agent_bin_dir(CALLER_PATH) == CALLER_PATH

    @posix_only
    def test_noop_on_empty_path_when_directory_is_missing(self):
        assert _prepend_agent_bin_dir("") == ""

    @posix_only
    def test_prepends_when_directory_exists(self, agent_bin):
        result = _prepend_agent_bin_dir(CALLER_PATH)

        assert result == os.pathsep.join([agent_bin, CALLER_PATH])

    @posix_only
    def test_is_idempotent(self, agent_bin):
        once = _prepend_agent_bin_dir(CALLER_PATH)

        assert _prepend_agent_bin_dir(once) == once

    @posix_only
    def test_returns_input_unchanged_when_already_present(self, agent_bin):
        """Membership dedupe: an existing entry keeps its position."""
        existing = os.pathsep.join(["/usr/bin", agent_bin, "/bin"])

        assert _prepend_agent_bin_dir(existing) == existing

    @pytest.mark.windows_only
    def test_noop_passthrough_on_windows(self, agent_bin):
        """Windows PATH is a deliberate passthrough in the sibling helpers."""
        native = os.pathsep.join([r"C:\Windows\System32", r"C:\Program Files\Git\bin"])

        assert _prepend_agent_bin_dir(native) == native
