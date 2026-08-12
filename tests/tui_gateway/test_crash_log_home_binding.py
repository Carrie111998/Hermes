"""HERMES_HOME binding for the TUI gateway panic log.

``tui_gateway/server.py`` installs ``sys.excepthook`` and
``threading.excepthook`` at IMPORT time, process-wide.  Forty-five test modules
import that module, so any unhandled thread exception anywhere in those pytest
processes runs ``_thread_panic_hook`` — no patching, no gateway required.

While the crash-log path was a module-level constant baked from
``get_hermes_home()`` at import, every one of those writes landed in the
developer's real ``~/.hermes/logs/tui_gateway_crash.log``: the autouse
``_hermetic_environment`` fixture redirects ``HERMES_HOME`` only AFTER
collection has already imported the module, so the constant had the real home
in it before the first test ran.  Observed in the wild on 2026-08-11 — a scratch
script in an unrelated worktree appended its own ``subprocess.TimeoutExpired``
to the user's live crash log, indistinguishable from a real gateway crash.

See GBrain ``concepts/import-time-hermes-home-snapshot-bug`` (original class).
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tui_gateway import server


def _live_home() -> Path:
    """The home the hermetic fixture points this test at."""
    return Path(os.environ["HERMES_HOME"])


def _expected_log() -> Path:
    return _live_home() / "logs" / "tui_gateway_crash.log"


def test_crash_log_is_not_baked_at_import():
    """The module must not carry a resolved path from import time.

    This is the invariant the whole bug class turns on: a non-None
    ``_CRASH_LOG`` at import means the path was fixed before conftest could
    redirect ``HERMES_HOME``.
    """
    assert server._CRASH_LOG is None


def test_crash_log_path_follows_the_live_hermes_home():
    resolved = Path(server._crash_log_path())
    assert resolved == _expected_log()
    # And explicitly: not the developer's real home.
    assert resolved != Path.home() / ".hermes" / "logs" / "tui_gateway_crash.log"


def test_crash_log_path_tracks_a_later_hermes_home_change(monkeypatch, tmp_path):
    """Resolution happens per call, not once — the seam has no cache."""
    other = tmp_path / "another-home"
    monkeypatch.setenv("HERMES_HOME", str(other))
    assert Path(server._crash_log_path()) == other / "logs" / "tui_gateway_crash.log"


def test_explicit_crash_log_override_still_wins(monkeypatch, tmp_path):
    """Backward compatibility: ``monkeypatch.setattr(server, "_CRASH_LOG", ...)``.

    The None sentinel exists so existing tests that pin the constant keep
    working; a non-None value must beat live resolution.
    """
    target = tmp_path / "pinned" / "crash.log"
    monkeypatch.setattr(server, "_CRASH_LOG", str(target))
    assert server._crash_log_path() == str(target)


def test_thread_panic_hook_writes_under_the_live_home():
    """The reachable-without-patching write path, exercised for real."""
    args = SimpleNamespace(
        exc_type=RuntimeError,
        exc_value=RuntimeError("regression probe"),
        exc_traceback=None,
        thread=SimpleNamespace(name="probe-worker"),
    )
    server._thread_panic_hook(args)

    written = _expected_log()
    assert written.exists(), "thread panic hook wrote outside the per-test home"
    body = written.read_text(encoding="utf-8")
    assert "thread exception" in body
    assert "probe-worker" in body


def test_panic_hook_writes_under_the_live_home(capsys):
    server._panic_hook(RuntimeError, RuntimeError("regression probe"), None)

    written = _expected_log()
    assert written.exists(), "sys.excepthook wrote outside the per-test home"
    assert "unhandled exception" in written.read_text(encoding="utf-8")


def test_turn_dispatcher_crash_write_uses_the_seam():
    """``_run_prompt_submit``'s ``except Exception`` shares the same resolver.

    Existing tests call ``server._run_prompt_submit`` directly, so this write
    is reachable from the suite; assert it reads the seam rather than a baked
    module constant.
    """
    source = Path(server.__file__).read_text(encoding="utf-8")
    assert "os.path.dirname(_CRASH_LOG)" not in source
    assert 'open(_CRASH_LOG, "a"' not in source
    assert source.count("_crash_log_path()") >= 6


def test_entry_shares_the_resolver_rather_than_a_stale_string():
    """``tui_gateway/entry.py`` used ``from ... import _CRASH_LOG``.

    A ``from``-import of the constant is a second snapshot; entry must import
    the resolver instead so its signal/exit logging follows the live home too.
    """
    entry = pytest.importorskip("tui_gateway.entry")

    assert entry._crash_log_path is server._crash_log_path

    entry._log_exit("regression probe")
    written = _expected_log()
    assert written.exists(), "entry._log_exit wrote outside the per-test home"
    assert "gateway exit" in written.read_text(encoding="utf-8")
