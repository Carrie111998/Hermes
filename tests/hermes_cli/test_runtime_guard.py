"""Tests for the startup runtime guard.

The guard exists because nothing enforced ``requires-python``: ``hermes`` ran on
3.14, where the private-stdlib mirror in tools/daemon_pool.py breaks every
delegate_task, and on interpreters linking SQLite with the WAL-reset bug while
the profile's databases were already in WAL mode.
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest

from hermes_cli import runtime_guard as rg

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── the constant must not drift from pyproject ───────────────────────────────

def test_supported_range_matches_pyproject():
    """MIN/MAX_PYTHON must equal pyproject's requires-python.

    The guard duplicates the range because resolving pyproject at runtime is
    unreliable for an installed package. This test is what keeps the duplicate
    honest — without it, a pyproject bump would silently leave the guard
    enforcing the old range.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'requires-python\s*=\s*"([^"]+)"', text)
    assert match, "requires-python not found in pyproject.toml"
    spec = match.group(1).replace(" ", "")

    low = re.search(r">=(\d+)\.(\d+)", spec)
    high = re.search(r"<(\d+)\.(\d+)", spec)
    assert low and high, f"unexpected requires-python format: {spec!r}"

    assert rg.MIN_PYTHON == (int(low.group(1)), int(low.group(2))), spec
    assert rg.MAX_PYTHON_EXCLUSIVE == (int(high.group(1)), int(high.group(2))), spec


# ── version predicate ────────────────────────────────────────────────────────

@pytest.mark.parametrize("version", [(3, 11), (3, 12), (3, 13), (3, 13, 14)])
def test_supported_versions_pass(version):
    assert rg.python_supported(version)


@pytest.mark.parametrize("version", [(3, 10), (3, 9), (2, 7), (3, 14), (3, 14, 0), (3, 15), (4, 0)])
def test_unsupported_versions_fail(version):
    assert not rg.python_supported(version)


def test_the_interpreter_running_these_tests_is_supported():
    """The suite itself must not run on an unsupported interpreter."""
    assert rg.python_supported(), (
        f"tests are running on unsupported Python {sys.version.split()[0]}"
    )


# ── check_python behaviour ───────────────────────────────────────────────────

def test_check_python_rejects_and_explains(monkeypatch):
    monkeypatch.delenv(rg.ALLOW_UNSUPPORTED_PYTHON_ENV, raising=False)
    monkeypatch.setattr(rg, "python_supported", lambda *a, **k: False)
    out = io.StringIO()
    assert rg.check_python(stream=out) is False
    text = out.getvalue()
    assert "ERROR" in text
    assert "unsupported Python" in text
    assert rg.ALLOW_UNSUPPORTED_PYTHON_ENV in text, "must tell the user how to override"


def test_check_python_override_allows_but_still_warns(monkeypatch):
    monkeypatch.setenv(rg.ALLOW_UNSUPPORTED_PYTHON_ENV, "1")
    monkeypatch.setattr(rg, "python_supported", lambda *a, **k: False)
    out = io.StringIO()
    assert rg.check_python(stream=out) is True
    assert "WARNING" in out.getvalue()


def test_check_python_is_silent_when_supported(monkeypatch):
    monkeypatch.setattr(rg, "python_supported", lambda *a, **k: True)
    out = io.StringIO()
    assert rg.check_python(stream=out) is True
    assert out.getvalue() == "", "a supported runtime must print nothing"


# ── enforce() ────────────────────────────────────────────────────────────────

def test_enforce_exits_on_unsupported_python(monkeypatch):
    monkeypatch.delenv(rg.ALLOW_UNSUPPORTED_PYTHON_ENV, raising=False)
    monkeypatch.setattr(rg, "check_python", lambda **k: False)
    monkeypatch.setattr(rg, "check_sqlite", lambda **k: True)
    with pytest.raises(SystemExit) as exc:
        rg.enforce(stream=io.StringIO())
    assert exc.value.code == 1


def test_enforce_can_report_without_exiting(monkeypatch):
    monkeypatch.setattr(rg, "check_python", lambda **k: False)
    monkeypatch.setattr(rg, "check_sqlite", lambda **k: True)
    assert rg.enforce(exit_on_failure=False, stream=io.StringIO()) is False


def test_vulnerable_sqlite_warns_but_never_blocks_startup(monkeypatch):
    """SQLite is advisory: it must warn, and must NOT gate startup."""
    monkeypatch.setattr(rg, "check_python", lambda **k: True)
    monkeypatch.setattr(rg, "check_sqlite", lambda **k: False)
    assert rg.enforce(exit_on_failure=True, stream=io.StringIO()) is True


def test_sqlite_check_matches_upstream_predicate():
    """The guard must agree with hermes_state, not re-derive its own rule."""
    from hermes_state import is_sqlite_wal_reset_vulnerable

    out = io.StringIO()
    assert rg.check_sqlite(stream=out) is (not is_sqlite_wal_reset_vulnerable())


def test_sqlite_warning_can_be_suppressed(monkeypatch):
    monkeypatch.setenv(rg.SUPPRESS_SQLITE_WARNING_ENV, "1")
    out = io.StringIO()
    assert rg.check_sqlite(stream=out) is True
    assert out.getvalue() == ""


# ── the guard is actually wired into the CLI ─────────────────────────────────

def test_cli_main_invokes_the_guard():
    """A guard nobody calls is the exact class of defect this audit found.

    Asserts against main()'s source so an upstream merge that drops the call
    fails here instead of silently restoring unguarded startup.
    """
    source = (REPO_ROOT / "hermes_cli" / "main.py").read_text(encoding="utf-8")
    assert "runtime_guard" in source, "hermes_cli/main.py no longer imports runtime_guard"
    assert re.search(r"_enforce_runtime\s*\(", source), "runtime_guard.enforce() is never called"
