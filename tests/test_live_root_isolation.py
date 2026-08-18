"""Regression tests for the collection-time HERMES_HOME pin.

The autouse ``_hermetic_environment`` fixture cannot protect the window
*before* the first test exists, and several production modules do real work to
the Hermes home at import (``tools/approval.py``'s module-level
``load_permanent_allowlist()``, ``hermes_cli/main.py``'s module-level
``setup_logging()``). ``tests/conftest.py`` therefore pins HERMES_HOME when it
is imported, ahead of collection. These tests pin that behaviour down.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

import pytest

import tests.conftest as conftest

REAL_HERMES = Path(os.path.expanduser("~")).resolve() / ".hermes"


def _is_inside_real_hermes(path: Path) -> bool:
    resolved = Path(path).resolve()
    return resolved == REAL_HERMES or REAL_HERMES in resolved.parents


@pytest.fixture
def pin_sandbox(monkeypatch):
    """Run ``_pin_hermes_home_before_collection`` without leaking state.

    The function mutates ``os.environ`` and a module global and creates a real
    directory, so every call needs the env restored, the global restored, and
    the directory removed.
    """
    created: list[Path] = []
    original = conftest._SESSION_HERMES_HOME

    def run() -> Path | None:
        conftest._SESSION_HERMES_HOME = None
        conftest._pin_hermes_home_before_collection()
        pinned = conftest._SESSION_HERMES_HOME
        if pinned is not None:
            created.append(pinned)
        return pinned

    try:
        yield run
    finally:
        conftest._SESSION_HERMES_HOME = original
        for path in created:
            shutil.rmtree(path, ignore_errors=True)


# ── the guard's decision logic ─────────────────────────────────────────────


def test_unset_hermes_home_is_pinned_to_a_throwaway_dir(pin_sandbox, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)

    pinned = pin_sandbox()

    assert pinned is not None, "an unset HERMES_HOME must be pinned, not left to resolve to ~/.hermes"
    assert not _is_inside_real_hermes(pinned)
    assert os.environ["HERMES_HOME"] == str(pinned)


def test_hermes_home_pointing_at_the_real_root_is_overridden(pin_sandbox, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(REAL_HERMES))

    pinned = pin_sandbox()

    assert pinned is not None
    assert not _is_inside_real_hermes(pinned)


def test_hermes_home_pointing_inside_the_real_root_is_overridden(pin_sandbox, monkeypatch):
    # The default profile-scoped home. This is the shape that actually ships --
    # get_hermes_home() resolves to ~/.hermes/profiles/main -- so a guard that
    # only compared against the bare root would let every real leak through.
    monkeypatch.setenv("HERMES_HOME", str(REAL_HERMES / "profiles" / "main"))

    pinned = pin_sandbox()

    assert pinned is not None
    assert not _is_inside_real_hermes(pinned)


def test_an_already_redirected_hermes_home_is_left_alone(pin_sandbox, monkeypatch, tmp_path):
    # CI, docker and the per-test fixture all set HERMES_HOME deliberately.
    # Clobbering theirs would break the isolation they already arranged.
    deliberate = tmp_path / "ci_home"
    deliberate.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(deliberate))

    pinned = pin_sandbox()

    assert pinned is None
    assert os.environ["HERMES_HOME"] == str(deliberate)


def test_pinned_home_has_the_layout_ensure_hermes_home_would_create(pin_sandbox, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)

    pinned = pin_sandbox()

    for subdir in ("cron", "sessions", "logs", "logs/curator", "memories",
                   "pairing", "hooks", "image_cache", "audio_cache", "skills"):
        assert (pinned / subdir).is_dir(), f"{subdir} missing from the pinned home"


# ── the damage the pin exists to prevent ───────────────────────────────────


def test_no_log_handler_writes_into_the_real_hermes_tree():
    """The ``agent.log`` contamination, checked directly.

    ``hermes_cli/main.py`` calls ``setup_logging()`` at module level. Without
    the pin that attaches a RotatingFileHandler for the *real*
    ``logs/agent.log`` to the ROOT logger and latches
    ``hermes_logging._logging_initialized``, so every record any later test
    emits is appended to the developer's production log.

    Non-vacuous exactly when something has imported a module that calls
    ``setup_logging()`` -- which a whole-suite run always does. In a run of
    this file alone it is a cheap invariant rather than a proof.
    """
    offenders = []
    for logger in [logging.getLogger()] + [
        logging.getLogger(name) for name in list(logging.root.manager.loggerDict)
    ]:
        for handler in getattr(logger, "handlers", []):
            filename = getattr(handler, "baseFilename", None)
            if filename and _is_inside_real_hermes(Path(filename)):
                offenders.append(filename)

    assert not offenders, (
        "log handlers are writing into the live Hermes tree: "
        f"{sorted(set(offenders))}"
    )


def test_ensure_hermes_home_never_ensured_the_real_tree():
    """The mkdir/chmod half, checked against the module's own memo.

    ``ensure_hermes_home`` records every home it has created directories under
    in ``_HERMES_HOME_ENSURED``. A real entry there is proof the live tree was
    mkdir'd and chmod'd during this session.
    """
    config = pytest.importorskip("hermes_cli.config")
    ensured = getattr(config, "_HERMES_HOME_ENSURED", set())

    offenders = [entry for entry in ensured if _is_inside_real_hermes(Path(str(entry)))]

    assert not offenders, f"ensure_hermes_home() created directories in the live tree: {offenders}"


# ── sys.path must not keep the live checkout after a test ──────────────────

def _offending_sys_path_entries(entries) -> list[str]:
    """Entries pointing into the live Hermes tree but outside this checkout.

    Exemptions, each for a different reason:

    * ``conftest.SYS_PATH_AT_IMPORT`` -- the runner put it there before
      collection. ``~/.hermes/ops`` arrives this way on every guarded run.
    * ``PROJECT_ROOT`` and below -- the repo lives under ~/.hermes/agent-src,
      so the checkout under test is inside the live tree by construction.
    * ``sys.prefix`` and below -- the venv is ~/.hermes/agent-src/.venv, so
      its site-packages are the running interpreter, not deployed code.
    """
    project_root = Path(conftest.PROJECT_ROOT).resolve()
    venv_root = Path(sys.prefix).resolve()
    from_runner = set(conftest.SYS_PATH_AT_IMPORT)
    offenders = []
    for entry in entries:
        if not entry or entry in from_runner:
            continue
        try:
            resolved = Path(entry).resolve()
        except (OSError, ValueError):
            continue
        inside_live = resolved == REAL_HERMES or REAL_HERMES in resolved.parents
        inside_repo = resolved == project_root or project_root in resolved.parents
        inside_venv = resolved == venv_root or venv_root in resolved.parents
        if inside_live and not inside_repo and not inside_venv:
            offenders.append(entry)
    return offenders


def test_sys_path_carries_no_live_hermes_entry_outside_this_repo():
    """The deployed-script leak, checked as a standing invariant.

    Tests that ``exec_module`` a script out of the live Hermes root inherit its
    ``sys.path.insert(0, HERMES_ROOT / "agent-src")`` permanently. Left in
    place, later imports resolve from the DEPLOYED checkout rather than the one
    under test -- so a fix present here can be invisible, and a bug fixed here
    can still appear. ``_no_live_checkout_on_sys_path`` in conftest strips
    those entries at teardown; this pins that they are gone.

    The repo itself lives under ~/.hermes/agent-src, so PROJECT_ROOT and
    anything beneath it is explicitly allowed -- that is the checkout under
    test, not the deployed one.

    This is a delta, not an absolute: entries the RUNNER supplied before
    collection (``conftest.SYS_PATH_AT_IMPORT``) are deliberate and exempt.
    The concrete case is ``~/.hermes/ops``, which this box's mandated
    ``pytest-run.cmd`` wrapper and both cron gates put on PYTHONPATH so
    ``-p pytest_fd_guard`` is importable -- an absolute assertion reds every
    guarded run, including the 02:30 and 04:30 nightly gates, on a path that
    is not a deployed-code leak at all.
    """
    offenders = _offending_sys_path_entries(sys.path)

    assert not offenders, (
        "sys.path points at the live Hermes tree outside this checkout; imports "
        f"may resolve to the DEPLOYED code instead of the code under test: {offenders}"
    )


def test_the_sys_path_invariant_still_catches_a_real_deployed_leak():
    """Non-vacuity guard for the exemption above.

    ``_offending_sys_path_entries`` skips whatever the runner already had on
    ``sys.path``, so it must be shown to still flag what it exists for: an
    entry inserted into the live Hermes tree DURING a test, which is what
    ``devflow_observability.py``'s line 34 does.
    """
    # A live-tree path that is outside every checkout no matter which one is
    # under test -- the deployed script directory those tests exec out of.
    probe = str(REAL_HERMES / "profiles" / "main" / "scripts")
    assert probe not in conftest.SYS_PATH_AT_IMPORT
    assert _offending_sys_path_entries([probe]) == [probe]

    # The literal leak, when this checkout is not itself the deployed one.
    # In the shared checkout PROJECT_ROOT *is* ~/.hermes/agent-src, and an
    # entry equal to the repo under test is correct rather than a leak.
    leak = REAL_HERMES / "agent-src"
    if leak.resolve() != Path(conftest.PROJECT_ROOT).resolve():
        assert leak not in [Path(e) for e in conftest.SYS_PATH_AT_IMPORT]
        assert _offending_sys_path_entries([str(leak)]) == [str(leak)]
