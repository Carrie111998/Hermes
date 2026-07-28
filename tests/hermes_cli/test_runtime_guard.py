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


def test_vulnerable_sqlite_blocks_startup(monkeypatch):
    """Vulnerable SQLite must GATE startup, not merely warn.

    This reverses the original advisory design deliberately. Warning-only left a
    live corruption path: hermes-agent/venv is Python 3.11.15 -- inside
    requires-python, so the Python half of the guard passes it -- linking SQLite
    3.50.4, and ~10 of the profile's 11 databases are already in WAL. Upstream's
    "refuse WAL on new databases" does not retroactively protect those. A guard
    that observes the exact precondition for database corruption and returns
    control to the caller is documentation, not a guard.
    """
    monkeypatch.setattr(rg, "check_python", lambda **k: True)
    monkeypatch.setattr(rg, "check_sqlite", lambda **k: False)
    with pytest.raises(SystemExit) as exc:
        rg.enforce(exit_on_failure=True, stream=io.StringIO())
    assert exc.value.code == 1


def test_sqlite_check_matches_upstream_predicate():
    """The guard must agree with hermes_state, not re-derive its own rule."""
    from hermes_state import is_sqlite_wal_reset_vulnerable

    out = io.StringIO()
    assert rg.check_sqlite(stream=out) is (not is_sqlite_wal_reset_vulnerable())


def test_suppress_env_cannot_bypass_a_real_corruption_risk(monkeypatch):
    """The cosmetic suppressor must not double as a safety override.

    HERMES_SUPPRESS_SQLITE_WARNING used to return True before the vulnerability
    was even probed, so one env var silently converted a corruption risk into a
    clean start. In this system an emergency-bypass file already became the
    routine path (2109 BYPASS vs 58 ALLOW in a gate audit log); a silent env
    bypass is the same failure with less friction.
    """
    monkeypatch.setattr(rg, "_sqlite_vulnerable", lambda: (True, "3.50.4"))
    monkeypatch.setenv(rg.SUPPRESS_SQLITE_WARNING_ENV, "1")
    monkeypatch.delenv(rg.ALLOW_VULNERABLE_SQLITE_ENV, raising=False)
    out = io.StringIO()
    assert rg.check_sqlite(stream=out) is False, "suppressor bypassed the risk"


def test_explicit_override_allows_vulnerable_sqlite_but_is_loud(monkeypatch):
    """Rollback to the 3.11 venv stays possible -- deliberately and audibly."""
    monkeypatch.setattr(rg, "_sqlite_vulnerable", lambda: (True, "3.50.4"))
    monkeypatch.setenv(rg.ALLOW_VULNERABLE_SQLITE_ENV, "1")
    monkeypatch.setenv(rg.SUPPRESS_SQLITE_WARNING_ENV, "1")  # must not silence this
    out = io.StringIO()
    assert rg.check_sqlite(stream=out) is True
    text = out.getvalue()
    assert "3.50.4" in text and "OVERRIDE" in text.upper(), (
        "an accepted corruption risk must still be announced"
    )


def test_safe_sqlite_is_silent(monkeypatch):
    monkeypatch.setattr(rg, "_sqlite_vulnerable", lambda: (False, "3.53.1"))
    out = io.StringIO()
    assert rg.check_sqlite(stream=out) is True
    assert out.getvalue() == ""


def test_unprobeable_sqlite_does_not_pass_silently(monkeypatch):
    """If the version cannot be determined, say so rather than assuming safe."""
    monkeypatch.setattr(rg, "_sqlite_vulnerable", lambda: (None, ""))
    out = io.StringIO()
    assert rg.check_sqlite(stream=out) is True  # unknown must not brick startup
    assert out.getvalue().strip(), "an unverifiable runtime must not be silent"


# ── the guard is actually wired into the CLI ─────────────────────────────────

def test_cli_main_invokes_the_guard():
    """A guard nobody calls is the exact class of defect this audit found.

    Asserts against main()'s source so an upstream merge that drops the call
    fails here instead of silently restoring unguarded startup.
    """
    source = (REPO_ROOT / "hermes_cli" / "main.py").read_text(encoding="utf-8")
    assert "runtime_guard" in source, "hermes_cli/main.py no longer imports runtime_guard"
    assert re.search(r"_enforce_runtime\s*\(", source), "runtime_guard.enforce() is never called"


# ── every console script must cross the guard, not just `hermes` ─────────────

def test_every_console_script_enforces_the_guard():
    """Each [project.scripts] entry point must call ``runtime_guard.enforce``.

    The guard originally landed only in ``hermes_cli.main:main``. The other two
    entry points -- ``hermes-agent`` (run_agent:main) and ``hermes-acp``
    (acp_adapter.entry:main) -- were installed on the 3.14 interpreter that
    ``hermes`` resolves to, exited 0 there, and reach SessionDB. A guard on one
    of three doors is not a guard; this test fails if a new entry point is added
    without one.
    """
    import tomllib

    pyproject = REPO_ROOT / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts, "no [project.scripts] found -- test is looking in the wrong place"

    unguarded = []
    for name, target in scripts.items():
        module_path, _, func = target.partition(":")
        src_file = REPO_ROOT / (module_path.replace(".", "/") + ".py")
        if not src_file.is_file():
            pkg_init = REPO_ROOT / module_path.replace(".", "/") / "__init__.py"
            src_file = pkg_init if pkg_init.is_file() else src_file
        assert src_file.is_file(), f"{name}: cannot locate source for {target}"

        source = src_file.read_text(encoding="utf-8", errors="replace")
        if "runtime_guard" not in source:
            unguarded.append(f"{name} -> {target} ({src_file.name})")

    assert not unguarded, (
        "console script(s) bypass the runtime guard: " + "; ".join(unguarded)
    )
