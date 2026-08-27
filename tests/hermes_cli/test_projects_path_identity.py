"""Filesystem-aware project-path identity.

``os.path.normcase`` is a no-op on POSIX, so the pre-existing case-normalised
dedup only ever worked on Windows. These tests pin the behaviour that makes it
hold on any case-insensitive volume (macOS/APFS included) without changing
behaviour on a case-sensitive one.

Host-honest: the case-insensitive assertions skip on a case-sensitive volume
rather than faking the filesystem.
"""

import os

import pytest

from hermes_cli import projects_db as pdb


def _case_insensitive(tmp_path) -> bool:
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    return (tmp_path / "caseprobe").exists()


@pytest.fixture
def conn(tmp_path):
    c = pdb.connect(tmp_path / "projects.db")
    yield c
    c.close()


# --- fs_identity ---------------------------------------------------------

def test_fs_identity_is_none_for_missing_path(tmp_path):
    assert pdb.fs_identity(str(tmp_path / "nope")) is None


def test_fs_identity_matches_for_the_same_directory(tmp_path):
    d = tmp_path / "Proj"
    d.mkdir()
    assert pdb.fs_identity(str(d)) == pdb.fs_identity(str(d) + os.sep)


def test_fs_identity_differs_for_different_directories(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert pdb.fs_identity(str(a)) != pdb.fs_identity(str(b))


# --- identity key --------------------------------------------------------

def test_identity_key_prefers_filesystem_identity_over_the_string(tmp_path):
    d = tmp_path / "Proj"
    d.mkdir()
    assert pdb.path_identity_key(str(d)).startswith("fsid:")


def test_identity_key_falls_back_to_a_string_key_when_absent(tmp_path):
    assert pdb.path_identity_key(str(tmp_path / "later")).startswith("path:")


def test_missing_paths_that_differ_stay_distinct(tmp_path):
    assert pdb.path_identity_key(str(tmp_path / "x")) != pdb.path_identity_key(
        str(tmp_path / "y")
    )


# --- the actual defect ---------------------------------------------------

def test_case_variant_of_an_existing_dir_resolves_to_one_project(tmp_path, conn):
    """The M1 defect: two spellings of ONE directory minted two projects."""
    if not _case_insensitive(tmp_path):
        pytest.skip("case-sensitive volume; spellings are genuinely different dirs")
    d = tmp_path / "MixedCase"
    d.mkdir()
    pid = pdb.create_project(
        conn, name="p", folders=[str(d)], primary_path=str(d)
    )
    found = pdb.find_by_primary_path(conn, str(tmp_path / "mixedcase"))
    assert found is not None and found.id == pid


def test_case_variant_owns_the_same_path_lookup(tmp_path, conn):
    if not _case_insensitive(tmp_path):
        pytest.skip("case-sensitive volume")
    d = tmp_path / "MixedCase"
    d.mkdir()
    pid = pdb.create_project(conn, name="p", folders=[str(d)], primary_path=str(d))
    owner = pdb.project_for_path(conn, str(tmp_path / "mixedcase" / "sub" / "f.py"))
    assert owner is not None and owner.id == pid


def test_distinct_directories_remain_distinct_projects(tmp_path, conn):
    a, b = tmp_path / "alpha", tmp_path / "beta"
    a.mkdir()
    b.mkdir()
    pa = pdb.create_project(conn, name="a", folders=[str(a)], primary_path=str(a))
    pb = pdb.create_project(conn, name="b", folders=[str(b)], primary_path=str(b))
    assert pa != pb
    assert pdb.find_by_primary_path(conn, str(a)).id == pa
    assert pdb.find_by_primary_path(conn, str(b)).id == pb


# --- regression guards ---------------------------------------------------

def test_existing_projects_still_resolve_by_their_own_path(tmp_path, conn):
    d = tmp_path / "repo"
    d.mkdir()
    pid = pdb.create_project(conn, name="r", folders=[str(d)], primary_path=str(d))
    assert pdb.find_by_primary_path(conn, str(d)).id == pid
    assert pdb.project_for_path(conn, str(d)).id == pid


def test_nested_folder_still_wins_longest_prefix(tmp_path, conn):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    po = pdb.create_project(conn, name="o", folders=[str(outer)], primary_path=str(outer))
    pi = pdb.create_project(conn, name="i", folders=[str(inner)], primary_path=str(inner))
    assert pdb.project_for_path(conn, str(inner / "f.py")).id == pi
    assert pdb.project_for_path(conn, str(outer / "f.py")).id == po


def test_unrelated_path_owns_nothing(tmp_path, conn):
    d = tmp_path / "repo"
    d.mkdir()
    pdb.create_project(conn, name="r", folders=[str(d)], primary_path=str(d))
    assert pdb.project_for_path(conn, str(tmp_path / "elsewhere" / "f.py")) is None


def test_identity_is_not_the_durable_key(tmp_path, conn):
    """A recreated directory changes inode; the project id must not change."""
    d = tmp_path / "repo"
    d.mkdir()
    pid = pdb.create_project(conn, name="r", folders=[str(d)], primary_path=str(d))
    before = pdb.fs_identity(str(d))
    d.rmdir()
    d.mkdir()
    after = pdb.fs_identity(str(d))
    assert pdb.get_project(conn, pid) is not None
    assert pdb.get_project(conn, pid).id == pid
    if before != after:
        # Identity drifted, yet the row is still found by its stored path.
        assert pdb.find_by_primary_path(conn, str(d)).id == pid


def test_volume_probe_is_honest_about_this_host(tmp_path):
    """The probe must agree with what the filesystem actually does."""
    probe = tmp_path / "ProbeDir"
    probe.mkdir()
    actual = (tmp_path / "probedir").exists()
    assert pdb._volume_is_case_insensitive(str(probe)) is actual
