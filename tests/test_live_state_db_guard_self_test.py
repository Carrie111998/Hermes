"""Self-test for the live-``state.db`` guard in tests/conftest.py.

This file is the canary. ``~/.hermes/state.db`` is the LIVE gateway/session
database for this machine: a test that opens it can corrupt real sessions and,
because sqlite takes locks, can wedge the running gateway.

Nothing in the suite is supposed to reach it -- ``_hermetic_environment``
redirects HERMES_HOME to a per-test tempdir and every production path resolves
through ``hermes_state.py``'s ``get_hermes_home() / "state.db"``. So an open of
the live file means the isolation FAILED for that code path, and until the
guard existed it failed SILENTLY: the test passed, having touched real data.

If anyone removes or weakens the guard, these tests fail. If a new way of
spelling the same path appears (a URI form, a bytes path, a relative walk),
add a test for it here too.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

LIVE_ROOT = os.path.join(os.path.expanduser("~"), ".hermes")
LIVE_DB = os.path.join(LIVE_ROOT, "state.db")

# Every assertion below pins this phrase, so a rewrite that keeps the guard but
# changes the message stays green while a rewrite that drops it goes red.
GUARD = "live-state-db guard"


# ──────────────────── the guard fires ─────────────────────────


def test_blocks_plain_path():
    with pytest.raises(RuntimeError, match=GUARD):
        sqlite3.connect(LIVE_DB)


def test_blocks_keyword_argument():
    """``connect(database=...)`` must be read, not just positional args."""
    with pytest.raises(RuntimeError, match=GUARD):
        sqlite3.connect(database=LIVE_DB)


def test_blocks_pathlib_path():
    from pathlib import Path

    with pytest.raises(RuntimeError, match=GUARD):
        sqlite3.connect(Path(LIVE_DB))


def test_blocks_dbapi2_alias():
    """``sqlite3.dbapi2.connect`` is a second public name for the same call."""
    with pytest.raises(RuntimeError, match=GUARD):
        sqlite3.dbapi2.connect(LIVE_DB)


def test_blocks_case_and_separator_variants():
    """normcase+realpath means drive case and slash direction cannot evade it."""
    variant = LIVE_DB.replace("\\", "/")
    if os.name == "nt":
        variant = variant[0].swapcase() + variant[1:]
    with pytest.raises(RuntimeError, match=GUARD):
        sqlite3.connect(variant)


def test_blocks_relative_path(monkeypatch):
    """A relative walk resolves to the same file, so it must be refused too."""
    monkeypatch.chdir(LIVE_ROOT)
    with pytest.raises(RuntimeError, match=GUARD):
        sqlite3.connect(os.path.join(".", "state.db"))


def test_blocks_uri_form():
    uri = "file:" + LIVE_DB.replace("\\", "/") + "?mode=ro"
    with pytest.raises(RuntimeError, match=GUARD):
        sqlite3.connect(uri, uri=True)


def test_blocks_wal_sidecar():
    with pytest.raises(RuntimeError, match=GUARD):
        sqlite3.connect(LIVE_DB + "-wal")


def test_guard_does_not_create_the_file(tmp_path):
    """A refusal must not have touched the disk on its way to raising.

    If the guard ever ran *after* the real connect, a machine that had no
    state.db would silently gain an empty one -- and the test would still see
    a RuntimeError, so only this assertion can tell the two apart.
    """
    existed = os.path.exists(LIVE_DB)
    with pytest.raises(RuntimeError, match=GUARD):
        sqlite3.connect(LIVE_DB)
    assert os.path.exists(LIVE_DB) == existed, (
        "the guard changed the existence of the live state.db -- it must "
        "refuse BEFORE delegating to the real sqlite3.connect"
    )


# ──────────────────── the guard stays out of the way ──────────────────


def test_allows_memory_database():
    conn = sqlite3.connect(":memory:")
    try:
        assert conn.execute("select 1").fetchone() == (1,)
    finally:
        conn.close()


def test_allows_a_tmp_path_database(tmp_path):
    """The positive control: ordinary isolated DBs must be unaffected."""
    target = tmp_path / "state.db"  # same BASENAME, different root
    conn = sqlite3.connect(str(target))
    try:
        conn.execute("create table t (x int)")
    finally:
        conn.close()
    assert target.exists()


def test_allows_a_hermes_home_database(tmp_path, monkeypatch):
    """A ``state.db`` under the *redirected* HERMES_HOME is the normal case."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    try:
        conn.execute("create table t (x int)")
    finally:
        conn.close()


# ──────────────────── the bypass marker ─────────────────────────


@pytest.mark.live_state_db_bypass
def test_bypass_marker_lifts_the_guard():
    """The documented escape hatch must actually work.

    Deliberately does NOT open the live DB -- it asserts the guard is disarmed
    by calling the refusal helper directly, so this canary can never be the
    thing that touches real data.
    """
    from tests.conftest import _allow_live_state_db, _reject_live_state_db

    assert _allow_live_state_db[0] is True
    _reject_live_state_db(LIVE_DB)  # must not raise


def test_guard_is_rearmed_after_a_bypassed_test():
    """A bypass must not leak into the next test in the same process.

    Ordering-dependent by construction: it only means anything when it runs
    after ``test_bypass_marker_lifts_the_guard`` in the same file, which is the
    order pytest collects them in.
    """
    from tests.conftest import _allow_live_state_db

    assert _allow_live_state_db[0] is False
    with pytest.raises(RuntimeError, match=GUARD):
        sqlite3.connect(LIVE_DB)
