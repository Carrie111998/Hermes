"""Parity guard for the credential-subset extraction (Phase 9 / Packet C, C1).

C1 split the credential-bearing entries out of the write denylist so the new
read-side deny can reuse them without also denying reads of shell rc files and
/etc/passwd. The write lists are now *composed* from that subset.

These tests freeze the pre-split membership. If a future edit drops an entry
while refactoring, this fails rather than silently weakening the write boundary.

Hermeticity note: ``build_*_denied_paths()`` take *home* as an argument, so
these pass a SYNTHETIC home rather than reading ``expanduser("~")`` at module
scope. That is both isolatable and a better unit test -- it exercises the
function's contract instead of whatever happens to be on the developer's
machine. (The original module-level ``HOME`` constant was evaluated at import
time, before any fixture could redirect it.)
"""

import os

import pytest

from agent.file_safety import (
    build_credential_denied_paths,
    build_credential_denied_prefixes,
    build_write_denied_paths,
    build_write_denied_prefixes,
)

# Home-relative entries, as (kind, *parts). Kind records why each is on the
# list, which is the distinction C1 turns on.
CREDENTIAL_RELPATHS = [
    (".ssh", "authorized_keys"),
    (".ssh", "id_rsa"),
    (".ssh", "id_ed25519"),
    (".ssh", "config"),
    (".netrc",),
    (".pgpass",),
    (".npmrc",),
    (".pypirc",),
]

# Write-protected for NON-credential reasons -- these must never be read-denied.
NON_CREDENTIAL_RELPATHS = [
    (".bashrc",), (".zshrc",), (".profile",),
    (".bash_profile",), (".zprofile",),
]

SYSTEM_WRITE_PATHS = ["/etc/sudoers", "/etc/passwd", "/etc/shadow"]

CREDENTIAL_RELPREFIXES = [
    (".ssh",), (".aws",), (".gnupg",), (".kube",),
    (".docker",), (".azure",), (".config", "gh"),
]

SYSTEM_WRITE_PREFIXES = ["/etc/sudoers.d", "/etc/systemd"]


@pytest.fixture
def home(tmp_path):
    """A synthetic home. Never the operator's real one."""
    h = tmp_path / "denylist_home"
    h.mkdir(exist_ok=True)
    return os.path.realpath(str(h))


def _rp(home, *parts):
    return os.path.realpath(os.path.join(home, *parts))


# --- write-list parity: nothing lost in the split ---------------------------

@pytest.mark.parametrize("parts", CREDENTIAL_RELPATHS + NON_CREDENTIAL_RELPATHS)
def test_write_denylist_retains_every_pre_split_path(home, parts):
    assert _rp(home, *parts) in build_write_denied_paths(home)


@pytest.mark.parametrize("path", SYSTEM_WRITE_PATHS)
def test_write_denylist_retains_system_paths(home, path):
    assert os.path.realpath(path) in build_write_denied_paths(home)


@pytest.mark.parametrize("parts", CREDENTIAL_RELPREFIXES)
def test_write_denylist_retains_every_pre_split_prefix(home, parts):
    assert _rp(home, *parts) + os.sep in build_write_denied_prefixes(home)


@pytest.mark.parametrize("prefix", SYSTEM_WRITE_PREFIXES)
def test_write_denylist_retains_system_prefixes(home, prefix):
    assert os.path.realpath(prefix) + os.sep in build_write_denied_prefixes(home)


def test_write_paths_are_exactly_the_pre_split_set(home, tmp_path, monkeypatch):
    """No entry was added or dropped by the split (HERMES_HOME .env aside)."""
    hermes_home = tmp_path / "hh"
    hermes_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    expected = (
        {_rp(home, *p) for p in CREDENTIAL_RELPATHS + NON_CREDENTIAL_RELPATHS}
        | {os.path.realpath(p) for p in SYSTEM_WRITE_PATHS}
        | {os.path.realpath(hermes_home / ".env")}
    )
    assert build_write_denied_paths(home) == expected


def test_write_prefixes_are_exactly_the_pre_split_set(home):
    expected = (
        {_rp(home, *p) + os.sep for p in CREDENTIAL_RELPREFIXES}
        | {os.path.realpath(p) + os.sep for p in SYSTEM_WRITE_PREFIXES}
    )
    assert set(build_write_denied_prefixes(home)) == expected


# --- the subset itself -------------------------------------------------------

@pytest.mark.parametrize("parts", NON_CREDENTIAL_RELPATHS)
def test_credential_subset_excludes_shell_rc_files(home, parts):
    """The whole point of the split: these stay READABLE."""
    assert _rp(home, *parts) not in build_credential_denied_paths(home)


@pytest.mark.parametrize("path", SYSTEM_WRITE_PATHS)
def test_credential_subset_excludes_system_files(home, path):
    assert os.path.realpath(path) not in build_credential_denied_paths(home)


@pytest.mark.parametrize("prefix", SYSTEM_WRITE_PREFIXES)
def test_credential_subset_excludes_system_prefixes(home, prefix):
    assert os.path.realpath(prefix) + os.sep not in build_credential_denied_prefixes(home)


@pytest.mark.parametrize("parts", CREDENTIAL_RELPATHS)
def test_credential_subset_includes_the_real_credential_stores(home, parts):
    assert _rp(home, *parts) in build_credential_denied_paths(home)


@pytest.mark.parametrize("parts", CREDENTIAL_RELPREFIXES)
def test_credential_subset_includes_the_credential_dirs(home, parts):
    assert _rp(home, *parts) + os.sep in build_credential_denied_prefixes(home)


def test_credential_subset_tracks_hermes_home(home, tmp_path, monkeypatch):
    hermes_home = tmp_path / "hh2"
    hermes_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    assert os.path.realpath(hermes_home / ".env") in build_credential_denied_paths(home)
