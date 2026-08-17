"""jobflow's on-disk paths: same location in production, isolated under pytest.

Before 2026-08-17 these were MODULE-LEVEL constants built from ``Path.home()``:

    CHECKPOINT_DB = Path.home() / ".hermes" / "graphs" / "checkpoints.db"
    APPROVAL_LOG  = Path.home() / ".hermes" / "graphs" / "approval-log.jsonl"
    APPLY_LOG     = Path.home() / ".hermes" / "graphs" / "apply-log.jsonl"
    TRACKER_LOG   = Path.home() / ".hermes" / "graphs" / "tracker-log.jsonl"

Two independent defects in that shape:

1. ``Path.home()`` BYPASSES the ``HERMES_HOME`` redirect ``conftest.py`` installs,
   so anything reaching these writes lands in the developer's real
   ``~/.hermes/graphs`` — which holds live job-application records.
2. Module-level means resolved at IMPORT time, and conftest's autouse fixture sets
   ``HERMES_HOME`` AFTER import. So even the correct resolver would have snapshotted
   the wrong root if it stayed a constant. Both halves have to change together.

The resolver is ``get_default_hermes_root()`` and NOT ``get_hermes_home()``: these
files live at the ROOT (``~/.hermes/graphs``), while ``get_hermes_home()`` returns
``<root>/profiles/<name>`` under a profile-scoped HERMES_HOME. Using it would have
relocated live data into the profile dir. Same root-vs-profile rule the notification
layer follows.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import hermes_constants
from graphs import jobflow

PATH_FUNCS = [
    ("checkpoint_db_path", "checkpoints.db"),
    ("approval_log_path", "approval-log.jsonl"),
    ("apply_log_path", "apply-log.jsonl"),
    ("tracker_log_path", "tracker-log.jsonl"),
]


# --- the "keep the current paths" contract -------------------------------


@pytest.mark.parametrize("func_name,filename", PATH_FUNCS)
def test_production_paths_are_byte_identical_to_the_old_constants(
    func_name, filename, monkeypatch
):
    """With no HERMES_HOME, resolve exactly where the old constants pointed.

    This is the whole point of choosing get_default_hermes_root(): the fix must
    isolate tests WITHOUT moving a single live file. If this test ever fails,
    the change has relocated real job-application data.
    """
    monkeypatch.delenv("HERMES_HOME", raising=False)
    if hermes_constants.get_default_hermes_root() != Path.home() / ".hermes":
        pytest.skip("platform default home is not ~/.hermes; the literal below cannot apply")

    expected = Path.home() / ".hermes" / "graphs" / filename

    assert getattr(jobflow, func_name)() == expected


def test_a_profile_scoped_hermes_home_does_NOT_relocate_these_files(monkeypatch):
    """The get_hermes_home() trap, pinned.

    Under HERMES_HOME=<root>/profiles/main, get_hermes_home() returns the PROFILE
    dir. Had the fix used it, these logs would silently move to
    ~/.hermes/profiles/main/graphs/ and orphan the existing records.
    """
    root = Path.home() / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "main"))

    assert jobflow.approval_log_path() == root / "graphs" / "approval-log.jsonl"
    assert jobflow.main_inbox_path() == root / "mailbox" / "main" / "inbox"
    # ...and prove the wrong resolver really would have differed, so this test
    # fails loudly if the two resolvers ever converge and stop discriminating.
    assert hermes_constants.get_hermes_home() != hermes_constants.get_default_hermes_root()


# --- the isolation the fix buys ------------------------------------------


@pytest.mark.parametrize("func_name,filename", PATH_FUNCS)
def test_paths_follow_an_isolated_hermes_home(func_name, filename, tmp_path, monkeypatch):
    """Under conftest's tempdir HERMES_HOME, nothing may resolve to the real home."""
    fake_root = tmp_path / "isolated"
    monkeypatch.setenv("HERMES_HOME", str(fake_root))

    resolved = getattr(jobflow, func_name)()

    assert resolved == fake_root / "graphs" / filename
    # Guard against the REAL ~/.hermes, not against Path.home(): on Windows
    # tmp_path itself lives under C:\Users\<user>\AppData\Local\Temp, so
    # "not under the home directory" is false for every correctly-isolated
    # path and the assertion would fire on a passing fix.
    assert (Path.home() / ".hermes") not in resolved.parents, (
        f"{func_name}() escaped into the real ~/.hermes: {resolved}"
    )


def test_main_inbox_follows_an_isolated_hermes_home(tmp_path, monkeypatch):
    """The APPLY_PACKET target — a real mailbox reach if left unredirected."""
    fake_root = tmp_path / "isolated"
    monkeypatch.setenv("HERMES_HOME", str(fake_root))

    assert jobflow.main_inbox_path() == fake_root / "mailbox" / "main" / "inbox"


# --- the import-time half of the bug -------------------------------------


def test_paths_are_resolved_lazily_not_snapshotted_at_import(tmp_path, monkeypatch):
    """Changing HERMES_HOME AFTER import must change the answer.

    This is the half a correct-resolver-but-still-a-constant fix would miss.
    conftest sets HERMES_HOME in an autouse fixture, i.e. after this module was
    imported, so a module-level constant would keep the import-time root and
    every test in the session would share it.
    """
    first = tmp_path / "one"
    monkeypatch.setenv("HERMES_HOME", str(first))
    assert jobflow.approval_log_path().is_relative_to(first)

    second = tmp_path / "two"
    monkeypatch.setenv("HERMES_HOME", str(second))
    assert jobflow.approval_log_path().is_relative_to(second), (
        "the path did not follow the env change -- it is being snapshotted, "
        "not resolved per call"
    )


def test_reimporting_the_module_does_not_rebind_a_home_constant(monkeypatch, tmp_path):
    """Belt and braces: a reload must not resurrect an import-time binding."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "reloaded"))
    importlib.reload(jobflow)
    try:
        assert jobflow.approval_log_path().is_relative_to(tmp_path / "reloaded")
    finally:
        importlib.reload(jobflow)


# --- regression guard against reintroduction ------------------------------


def test_the_module_contains_no_Path_home_call_at_all():
    """Static guard.

    The four constants were not the only instance -- MAIN_INBOX was built with
    Path.home() inline inside a function body at the old :534, where no
    module-level audit would have found it. Assert on the SOURCE so any new
    Path.home() anywhere in the file trips, function bodies included.
    """
    source = Path(jobflow.__file__).read_text(encoding="utf-8")

    offenders = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(source.splitlines(), 1)
        if "Path.home()" in line and not line.lstrip().startswith(("#", "*", '"'))
    ]

    assert not offenders, (
        "graphs/jobflow.py resolves the real user home again; use "
        "get_default_hermes_root() so tests stay isolated:\n  " + "\n  ".join(offenders)
    )


def test_the_old_module_level_constants_are_gone():
    """They must not linger as aliases -- an alias re-snapshots at import."""
    for dead in ("CHECKPOINT_DB", "APPROVAL_LOG", "APPLY_LOG", "TRACKER_LOG"):
        assert not hasattr(jobflow, dead), (
            f"{dead} still exists as a module attribute; it would be bound at "
            "import time and bypass HERMES_HOME again"
        )
