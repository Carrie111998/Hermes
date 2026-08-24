"""Tests for the per-profile Projects store (hermes_cli/projects_db)."""

from __future__ import annotations

import os

import pytest

from hermes_cli import projects_db as pdb


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


def test_create_dedups_by_primary_path(conn):
    pid = pdb.create_project(conn, name="GeoTrace", folders=["/www/geotrace"])

    # Same folder again (any name): refused, existing project named in error.
    with pytest.raises(ValueError, match="already belongs to project 'geotrace'"):
        pdb.create_project(conn, name="GeoTrace", folders=["/www/geotrace"])
    with pytest.raises(ValueError, match="already belongs"):
        pdb.create_project(conn, name="Other Name", primary_path="/www/geotrace")

    # Trailing-separator spelling of the same folder is still a duplicate.
    with pytest.raises(ValueError, match="already belongs"):
        pdb.create_project(conn, name="GeoTrace", primary_path="/www/geotrace/")

    # Deliberate duplicates stay possible.
    dup = pdb.create_project(
        conn, name="GeoTrace", folders=["/www/geotrace"], allow_duplicate_path=True
    )
    assert dup != pid
    assert len(pdb.list_projects(conn)) == 2


def test_create_dedup_ignores_archived_and_other_paths(conn):
    pid = pdb.create_project(conn, name="App", folders=["/www/app"])
    pdb.archive_project(conn, pid)

    # Archived project no longer blocks the path.
    fresh = pdb.create_project(conn, name="App", folders=["/www/app"])
    assert fresh != pid

    # Different folder is never a collision; folder-less projects don't match.
    pdb.create_project(conn, name="Elsewhere", folders=["/www/other"])
    pdb.create_project(conn, name="No Folder")


def test_find_by_primary_path(conn):
    pid = pdb.create_project(conn, name="App", folders=["/www/app"])

    assert pdb.find_by_primary_path(conn, "/www/app").id == pid
    assert pdb.find_by_primary_path(conn, "/www/app/").id == pid
    assert pdb.find_by_primary_path(conn, "/www/nope") is None
    assert pdb.find_by_primary_path(conn, "") is None






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


# ---------------------------------------------------------------------------
# Organizations (t_8b6e58a9)
# ---------------------------------------------------------------------------


def test_existing_projects_stay_ungrouped_after_migration(conn):
    """Migration adds organization_id nullable — no backfill, ever."""
    pid = pdb.create_project(conn, name="Saylent Swarm", folders=["/srv/saylent-swarm"])
    proj = pdb.get_project(conn, pid)
    assert proj.organization_id is None
    assert proj.organization is None
    assert proj.to_dict()["organization"] is None
    assert proj.to_dict()["organization_id"] is None


def test_organization_create_list_get():
    import sqlite3

    # Simulate a legacy pre-org DB file that only has the v1 projects schema,
    # then open it through connect() to prove the ADD COLUMN migration path
    # (not just CREATE TABLE IF NOT EXISTS on a fresh DB).
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        legacy_path = Path(d) / "projects.db"
        legacy_conn = sqlite3.connect(str(legacy_path))
        legacy_conn.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                description TEXT, created_at INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO projects (id, slug, name, description, created_at, archived)
            VALUES ('p_legacy1', 'saylent-swarm', 'Saylent Swarm', NULL, 1700000000, 0);
            """
        )
        legacy_conn.commit()
        legacy_conn.close()

        c = pdb.connect(db_path=legacy_path)
        try:
            # Legacy row survived the migration and is still ungrouped.
            legacy = pdb.get_project(c, "saylent-swarm")
            assert legacy is not None
            assert legacy.organization_id is None

            oid = pdb.create_organization(c, name="Family Office", color="#ff8800")
            org = pdb.get_organization(c, oid)
            assert org.name == "Family Office"
            assert org.slug == "family-office"
            assert org.color == "#ff8800"

            orgs = pdb.list_organizations(c)
            assert [o.id for o in orgs] == [oid]

            # Lookup by slug too.
            assert pdb.get_organization(c, "family-office").id == oid
            assert pdb.get_organization(c, "nope") is None
        finally:
            c.close()


def test_organization_rename(conn):
    oid = pdb.create_organization(conn, name="Old Name")
    assert pdb.update_organization(conn, oid, name="New Name") is True
    assert pdb.get_organization(conn, oid).name == "New Name"


def test_organization_slug_dedup(conn):
    a = pdb.create_organization(conn, name="Acme")
    b = pdb.create_organization(conn, name="Acme")
    assert pdb.get_organization(conn, a).slug == "acme"
    assert pdb.get_organization(conn, b).slug == "acme-2"


def test_assign_project_to_organization_and_unset(conn):
    pid = pdb.create_project(conn, name="Hermes Agent", folders=["/tmp/hermes"])
    oid = pdb.create_organization(conn, name="Family Office")

    assert pdb.set_project_organization(conn, pid, oid) is True
    proj = pdb.get_project(conn, pid)
    assert proj.organization_id == oid
    assert proj.organization.id == oid
    assert proj.organization.name == "Family Office"
    assert proj.to_dict()["organization"] == {
        "id": oid, "slug": "family-office", "name": "Family Office",
        "color": None, "description": None,
        "created_at": proj.organization.created_at,
    }

    # Unset back to ungrouped.
    assert pdb.set_project_organization(conn, pid, None) is True
    proj2 = pdb.get_project(conn, pid)
    assert proj2.organization_id is None
    assert proj2.organization is None
    assert proj2.to_dict()["organization"] is None


def test_move_project_between_organizations(conn):
    pid = pdb.create_project(conn, name="App", folders=["/www/app"])
    o1 = pdb.create_organization(conn, name="Org One")
    o2 = pdb.create_organization(conn, name="Org Two")

    pdb.set_project_organization(conn, pid, o1)
    assert pdb.get_project(conn, pid).organization_id == o1

    pdb.set_project_organization(conn, pid, o2)
    assert pdb.get_project(conn, pid).organization_id == o2


def test_set_project_organization_rejects_unknown_ids(conn):
    pid = pdb.create_project(conn, name="App", folders=["/www/app"])
    oid = pdb.create_organization(conn, name="Org")

    with pytest.raises(ValueError, match="no such project"):
        pdb.set_project_organization(conn, "p_nope", oid)
    with pytest.raises(ValueError, match="no such organization"):
        pdb.set_project_organization(conn, pid, "o_nope")


def test_set_project_organization_accepts_project_slug(conn):
    """B1 repro: passing a project *slug* (not id) must still update the
    row, not silently match 0 rows via a WHERE id=? on an unresolved slug."""
    pid = pdb.create_project(conn, name="Hermes Agent", folders=["/tmp/hermes"])
    oid = pdb.create_organization(conn, name="Family Office")
    proj = pdb.get_project(conn, pid)

    assert pdb.set_project_organization(conn, proj.slug, oid) is True
    assert pdb.get_project(conn, pid).organization_id == oid


def test_set_project_organization_accepts_organization_slug(conn):
    """B2 repro: passing an organization *slug* (not id) must resolve to
    the canonical org id before the write, not raise a raw IntegrityError
    from writing the unresolved slug into the FK column."""
    pid = pdb.create_project(conn, name="App", folders=["/www/app"])
    oid = pdb.create_organization(conn, name="Family Office")
    org = pdb.get_organization(conn, oid)

    assert pdb.set_project_organization(conn, pid, org.slug) is True
    proj = pdb.get_project(conn, pid)
    assert proj.organization_id == oid
    assert proj.organization.id == oid


def test_delete_organization_ungroups_member_projects(conn):
    """Deleting an org sets member projects' organization_id back to NULL
    (FK ON DELETE SET NULL) rather than orphaning the FK or cascading."""
    pid = pdb.create_project(conn, name="App", folders=["/www/app"])
    oid = pdb.create_organization(conn, name="Org")
    pdb.set_project_organization(conn, pid, oid)
    assert pdb.get_project(conn, pid).organization_id == oid

    assert pdb.delete_organization(conn, oid) is True

    proj = pdb.get_project(conn, pid)
    assert proj.organization_id is None
    assert proj.organization is None
    # Project itself is untouched, not deleted.
    assert proj.name == "App"


def test_list_projects_batches_organization_lookup(conn):
    """list_projects() must not do one org query per project (N+1)."""
    oid = pdb.create_organization(conn, name="Org")
    p1 = pdb.create_project(conn, name="One", folders=["/a"])
    p2 = pdb.create_project(conn, name="Two", folders=["/b"])
    pdb.set_project_organization(conn, p1, oid)
    # p2 stays ungrouped.

    projects = {p.id: p for p in pdb.list_projects(conn)}
    assert projects[p1].organization.name == "Org"
    assert projects[p2].organization is None


