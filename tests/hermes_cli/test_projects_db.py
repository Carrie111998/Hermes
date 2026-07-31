"""Tests for the Projects store (hermes_cli/projects_db).

The Projects store is machine-level (shared across profiles) per #75308:
``projects_db_path()`` resolves the *process* Hermes home — not the active
profile — so a profile switch never moves the DB on disk.
"""

from __future__ import annotations

import os

from unittest.mock import patch

import pytest

from hermes_cli import projects_db as pdb
from hermes_constants import get_process_hermes_home


@pytest.fixture(autouse=True)
def _reset_legacy_migration_cache():
    """Reset module-level migration cache between tests so each one starts clean."""
    pdb._LEGACY_MIGRATION_DONE.clear()
    yield
    pdb._LEGACY_MIGRATION_DONE.clear()


@pytest.fixture
def conn(tmp_path):
    c = pdb.connect(db_path=tmp_path / "projects.db")
    try:
        yield c
    finally:
        c.close()




def test_discovery_policy_change_clears_only_discovered_rows(conn):
    project_id = pdb.create_project(conn, name="Explicit", folders=["/www/explicit"])
    pdb.record_discovered_repos(
        conn, [("/www/scanned", "scanned")], policy_key="policy-a"
    )

    assert pdb.reconcile_discovered_repos_policy(conn, "policy-b") is True
    assert pdb.list_discovered_repos(conn) == []
    assert pdb.get_project(conn, project_id) is not None
    assert pdb.get_discovery_policy_key(conn) == "policy-b"




def test_create_get_list(conn):
    pid = pdb.create_project(conn, name="Hermes Agent", folders=["/tmp/hermes"])
    proj = pdb.get_project(conn, pid)

    assert proj is not None
    assert proj.slug == "hermes-agent"
    assert proj.name == "Hermes Agent"
    # First folder becomes primary.
    assert proj.primary_path == "/tmp/hermes"
    assert [f.path for f in proj.folders] == ["/tmp/hermes"]
    assert proj.folders[0].is_primary is True

    # Lookup by slug too.
    assert pdb.get_project(conn, "hermes-agent").id == pid
    assert len(pdb.list_projects(conn)) == 1




def test_project_for_path_skips_archived(conn):
    pid = pdb.create_project(conn, name="P", folders=["/www/app"])
    pdb.archive_project(conn, pid)

    assert pdb.project_for_path(conn, "/www/app/src") is None
    # Archived hidden from the default list but visible with include_archived.
    assert pdb.list_projects(conn) == []
    assert len(pdb.list_projects(conn, include_archived=True)) == 1

    pdb.restore_project(conn, pid)
    assert pdb.project_for_path(conn, "/www/app/src").id == pid




def test_per_profile_isolation(tmp_path):
    # Two distinct DB paths stand in for two profiles' HERMES_HOME.
    a = pdb.connect(db_path=tmp_path / "a" / "projects.db")
    b = pdb.connect(db_path=tmp_path / "b" / "projects.db")
    try:
        pdb.create_project(a, name="Only In A", folders=["/a"])
        pdb.record_discovered_repos(a, [("/a/scanned", "scanned")])

        assert [p.slug for p in pdb.list_projects(a)] == ["only-in-a"]
        assert pdb.list_projects(b) == []
        assert [row["root"] for row in pdb.list_discovered_repos(a)] == [
            "/a/scanned"
        ]
        assert pdb.list_discovered_repos(b) == []
    finally:
        a.close()
        b.close()


# ── #75308: projects_db_path resolves process HERMES_HOME, not profile ──────


def test_projects_db_path_uses_process_hermes_home(tmp_path, monkeypatch):
    """With the active profile scoped via ``set_hermes_home_override``,
    projects_db_path() still resolves the *process* Hermes home — the
    value of ``HERMES_HOME`` at process launch, before any profile
    override. The legacy per-profile DB lookup only happens when the
    profile home differs from the process home."""
    process_home = tmp_path / "machine"
    profile_home = tmp_path / "profiles" / "staging"
    process_home.mkdir(parents=True)
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(process_home))
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(str(profile_home))
    try:
        # process home = the env var; profile home = the override. With
        # the override active, ``get_hermes_home()`` returns the profile
        # path but ``get_process_hermes_home()`` still returns the
        # process path — projects_db_path() must take the process path.
        path = pdb.projects_db_path()
        assert path.resolve() == (process_home / "projects.db").resolve()
    finally:
        reset_hermes_home_override(token)


def test_projects_db_path_returns_process_home_when_only_env_var(monkeypatch, tmp_path):
    """When HERMES_HOME is the process home (default install), no migration is
    attempted and the path resolves normally."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = pdb.projects_db_path()
    assert path == (tmp_path / "projects.db").resolve()


def test_legacy_per_profile_db_is_migrated_on_first_access(tmp_path, monkeypatch):
    """A legacy per-profile DB that has rows is copied to the machine-level
    path on the first call to projects_db_path(). Subsequent calls are a
    no-op (#75308)."""
    process_home = tmp_path / "machine"
    profile_home = tmp_path / "profiles" / "staging"
    process_home.mkdir(parents=True)
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(process_home))
    # Profile home differs from process home: this is the case the issue
    # describes (gateway launched with --profile in app-global mode).
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(str(profile_home))
    try:
        # Seed a legacy per-profile DB with one project.
        legacy_conn = pdb.connect(db_path=profile_home / "projects.db")
        try:
            pdb.create_project(legacy_conn, name="Legacy Project", folders=["/legacy/path"])
        finally:
            legacy_conn.close()
        assert (profile_home / "projects.db").exists()

        # The machine-level path must not exist yet.
        assert not (process_home / "projects.db").exists()

        # First call migrates.
        target = pdb.projects_db_path()
        assert target.resolve() == (process_home / "projects.db").resolve()
        assert (process_home / "projects.db").exists()

        # The migrated DB is readable and carries the legacy row.
        new_conn = pdb.connect(db_path=target)
        try:
            rows = pdb.list_projects(new_conn)
        finally:
            new_conn.close()
        assert [r.slug for r in rows] == ["legacy-project"]
    finally:
        reset_hermes_home_override(token)


def test_legacy_empty_db_is_not_migrated(tmp_path, monkeypatch):
    """An empty legacy per-profile DB does not shadow the machine-level home;
    the copy is skipped and a fresh machine-level DB is opened."""
    process_home = tmp_path / "machine"
    profile_home = tmp_path / "profiles" / "staging"
    process_home.mkdir(parents=True)
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(process_home))
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(str(profile_home))
    try:
        # Create an empty legacy DB (no rows, just schema).
        legacy_conn = pdb.connect(db_path=profile_home / "projects.db")
        legacy_conn.close()
        assert (profile_home / "projects.db").exists()

        target = pdb.projects_db_path()
        assert target.resolve() == (process_home / "projects.db").resolve()

        # Empty legacy must NOT shadow: machine-level DB must be opened fresh
        # (still empty), the legacy file is left untouched.
        new_conn = pdb.connect(db_path=target)
        try:
            assert pdb.list_projects(new_conn) == []
        finally:
            new_conn.close()
    finally:
        reset_hermes_home_override(token)


def test_legacy_migration_is_idempotent(tmp_path, monkeypatch):
    """Second call to projects_db_path() must NOT re-copy the legacy DB over
    a populated machine-level one."""
    process_home = tmp_path / "machine"
    profile_home = tmp_path / "profiles" / "staging"
    process_home.mkdir(parents=True)
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(process_home))
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(str(profile_home))
    try:
        legacy_conn = pdb.connect(db_path=profile_home / "projects.db")
        try:
            pdb.create_project(legacy_conn, name="Legacy", folders=["/legacy"])
        finally:
            legacy_conn.close()

        # First call migrates.
        pdb.projects_db_path()

        # Now mutate the machine-level DB; the second call must not clobber it.
        target_conn = pdb.connect(db_path=process_home / "projects.db")
        try:
            pdb.create_project(target_conn, name="Machine", folders=["/machine"])
        finally:
            target_conn.close()

        # Re-resolve; the legacy file should not be copied over the machine store.
        # The first call already migrated "Legacy"; the second call must leave
        # both "Legacy" (from the initial copy) and the new "Machine" alone.
        pdb.projects_db_path()
        target_conn = pdb.connect(db_path=process_home / "projects.db")
        try:
            slugs = sorted(p.slug for p in pdb.list_projects(target_conn))
        finally:
            target_conn.close()
        assert slugs == ["legacy", "machine"]
        # Idempotency check: the machine store's mtime must be newer than the
        # legacy store's, i.e. the second call did not re-copy legacy over
        # machine and clobber the "Machine" addition.
        legacy_mtime = (profile_home / "projects.db").stat().st_mtime
        machine_mtime = (process_home / "projects.db").stat().st_mtime
        assert machine_mtime > legacy_mtime
    finally:
        reset_hermes_home_override(token)
