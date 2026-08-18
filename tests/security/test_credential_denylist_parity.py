"""Parity guard for the credential-subset extraction (Phase 9 / Packet C, C1).

C1 split the credential-bearing entries out of the write denylist so the new
read-side deny can reuse them without also denying reads of shell rc files and
/etc/passwd.  The write lists are now *composed* from that subset.

These tests freeze the pre-split membership.  If a future edit drops an entry
while refactoring, this fails rather than silently weakening the write boundary.
"""

import os

import pytest

from agent.file_safety import (
    build_credential_denied_paths,
    build_credential_denied_prefixes,
    build_write_denied_paths,
    build_write_denied_prefixes,
)

HOME = os.path.realpath(os.path.expanduser("~"))


def _rp(*parts):
    return os.path.realpath(os.path.join(HOME, *parts))


# Exactly the membership that existed before the C1 split, minus the
# HERMES_HOME-dependent .env entry which is asserted separately.
PRE_SPLIT_WRITE_PATHS = [
    _rp(".ssh", "authorized_keys"),
    _rp(".ssh", "id_rsa"),
    _rp(".ssh", "id_ed25519"),
    _rp(".ssh", "config"),
    _rp(".bashrc"),
    _rp(".zshrc"),
    _rp(".profile"),
    _rp(".bash_profile"),
    _rp(".zprofile"),
    _rp(".netrc"),
    _rp(".pgpass"),
    _rp(".npmrc"),
    _rp(".pypirc"),
    os.path.realpath("/etc/sudoers"),
    os.path.realpath("/etc/passwd"),
    os.path.realpath("/etc/shadow"),
]

PRE_SPLIT_WRITE_PREFIXES = [
    _rp(".ssh") + os.sep,
    _rp(".aws") + os.sep,
    _rp(".gnupg") + os.sep,
    _rp(".kube") + os.sep,
    _rp(".docker") + os.sep,
    _rp(".azure") + os.sep,
    _rp(".config", "gh") + os.sep,
    os.path.realpath("/etc/sudoers.d") + os.sep,
    os.path.realpath("/etc/systemd") + os.sep,
]


@pytest.mark.parametrize("path", PRE_SPLIT_WRITE_PATHS)
def test_write_denylist_retains_every_pre_split_path(path):
    assert path in build_write_denied_paths(HOME)


@pytest.mark.parametrize("prefix", PRE_SPLIT_WRITE_PREFIXES)
def test_write_denylist_retains_every_pre_split_prefix(prefix):
    assert prefix in build_write_denied_prefixes(HOME)


def test_write_paths_are_exactly_the_pre_split_set(tmp_path, monkeypatch):
    """No entry was added or dropped by the split (HERMES_HOME .env aside)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    expected = set(PRE_SPLIT_WRITE_PATHS) | {os.path.realpath(tmp_path / ".env")}
    assert build_write_denied_paths(HOME) == expected


def test_write_prefixes_are_exactly_the_pre_split_set():
    assert set(build_write_denied_prefixes(HOME)) == set(PRE_SPLIT_WRITE_PREFIXES)


# --- the subset itself -------------------------------------------------------

def test_credential_subset_excludes_shell_rc_and_system_files():
    """The whole point of the split: these stay READABLE."""
    creds = build_credential_denied_paths(HOME)
    for path in (
        _rp(".bashrc"), _rp(".zshrc"), _rp(".profile"),
        _rp(".bash_profile"), _rp(".zprofile"),
        os.path.realpath("/etc/passwd"), os.path.realpath("/etc/sudoers"),
        os.path.realpath("/etc/shadow"),
    ):
        assert path not in creds, f"{path} must not be read-denied"

    prefixes = build_credential_denied_prefixes(HOME)
    assert os.path.realpath("/etc/systemd") + os.sep not in prefixes
    assert os.path.realpath("/etc/sudoers.d") + os.sep not in prefixes


def test_credential_subset_includes_the_real_credential_stores():
    creds = build_credential_denied_paths(HOME)
    for path in (
        _rp(".ssh", "id_rsa"), _rp(".ssh", "id_ed25519"),
        _rp(".ssh", "authorized_keys"), _rp(".ssh", "config"),
        _rp(".netrc"), _rp(".pgpass"), _rp(".npmrc"), _rp(".pypirc"),
    ):
        assert path in creds

    prefixes = build_credential_denied_prefixes(HOME)
    for prefix in (
        _rp(".ssh"), _rp(".aws"), _rp(".gnupg"), _rp(".kube"),
        _rp(".docker"), _rp(".azure"), _rp(".config", "gh"),
    ):
        assert prefix + os.sep in prefixes


def test_credential_subset_tracks_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert os.path.realpath(tmp_path / ".env") in build_credential_denied_paths(HOME)
