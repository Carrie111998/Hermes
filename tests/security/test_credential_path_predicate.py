"""The layer-1 protected-path predicate (Phase 9 / Packet C, C2).

Layer 1 is the BOUNDARY. It is pure path logic and must hold regardless of
HERMES_REDACT_SECRETS, the `code_file` argument, config, or import order.
"""

import os

import pytest

from agent.file_safety import (
    get_credential_read_error,
    is_credential_basename,
    is_credential_read_denied,
    is_credential_template_basename,
)

# (basename, denied?) -- the full policy table, both directions.
BASENAMES = [
    # --- real credential stores -------------------------------------------
    (".env", True),
    (".env.local", True),
    (".env.production", True),
    (".env.staging", True),
    (".env.development", True),
    (".env.production.local", True),
    (".env.test", True),          # fixture creds are still creds
    (".envrc", True),             # direnv: `export AWS_SECRET_ACCESS_KEY=`
    (".credentials", True),
    (".credentials.json", True),
    ("credentials", True),        # ~/.aws/credentials
    ("credentials.json", True),   # gcloud / docker
    (".ENV", True),               # case-insensitive
    # --- templates: must stay readable ------------------------------------
    (".env.example", False),
    (".env.sample", False),
    (".env.template", False),
    (".env.dist", False),
    (".env.defaults", False),
    (".env.schema", False),
    ("env.example", False),       # no leading dot; common in Node/Docker repos
    (".env.example.local", False),
    (".env.local.example", False),
    (".ENV.EXAMPLE", False),
    # --- not in the .env family at all ------------------------------------
    (".envexample", False),       # not a real convention; don't guess
    (".environment", False),
    (".enviro", False),
    ("main.py", False),
    ("README.md", False),
    ("environment.yml", False),
]


@pytest.mark.parametrize("basename,denied", BASENAMES)
def test_basename_policy(basename, denied):
    assert is_credential_basename(basename) is denied


def test_env_example_is_readable_because_setup_workflows_need_it():
    """Named separately: this exemption is load-bearing, not incidental.

    Reading .env.example to learn which keys a project needs is the commonest
    legitimate .env workflow. Blocking it makes the agent worse at setup tasks
    and buys nothing -- a template holds no live secret.
    """
    assert is_credential_basename(".env.example") is False
    assert is_credential_template_basename(".env.example") is True


def test_template_token_matches_any_component_not_just_the_suffix():
    assert is_credential_template_basename(".env.example.local") is True
    assert is_credential_template_basename(".env.local.example") is True


# --- full-path behaviour -----------------------------------------------------

def test_denies_env_anywhere_on_disk(tmp_path):
    target = tmp_path / "someproject" / ".env"
    target.parent.mkdir()
    target.write_text("K=v\n")
    assert is_credential_read_denied(str(target)) is True


def test_allows_ordinary_source_file(tmp_path):
    target = tmp_path / "main.py"
    target.write_text("print('hi')\n")
    assert is_credential_read_denied(str(target)) is False


def test_denies_hermes_home_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("K=v\n")
    assert is_credential_read_denied(str(tmp_path / ".env")) is True


def test_denies_ssh_directory_contents(_hermetic_credential_environment):
    """Uses the synthetic home explicitly, so hermeticity is stated rather
    than inherited by accident from the autouse fixture."""
    home = os.path.realpath(str(_hermetic_credential_environment))
    assert is_credential_read_denied(os.path.join(home, ".ssh", "id_rsa")) is True
    assert is_credential_read_denied(os.path.join(home, ".aws", "credentials")) is True


@pytest.mark.parametrize("rel", [".bashrc", ".zshrc", ".profile", ".zprofile"])
def test_shell_rc_files_stay_readable(rel, _hermetic_credential_environment):
    """C1's accepted cost, asserted so it cannot regress silently."""
    home = os.path.realpath(str(_hermetic_credential_environment))
    assert is_credential_read_denied(os.path.join(home, rel)) is False


def test_etc_passwd_stays_readable():
    assert is_credential_read_denied("/etc/passwd") is False


# --- symlinks (covered) vs hard links (deliberately NOT covered) -------------

def test_symlink_pointing_at_env_is_denied(tmp_path):
    real = tmp_path / ".env"
    real.write_text("K=v\n")
    link = tmp_path / "notes.txt"
    link.symlink_to(real)
    assert is_credential_read_denied(str(link)) is True


def test_symlink_named_env_pointing_elsewhere_is_denied(tmp_path):
    real = tmp_path / "notes.txt"
    real.write_text("harmless\n")
    link = tmp_path / ".env"
    link.symlink_to(real)
    assert is_credential_read_denied(str(link)) is True


def test_hard_link_is_NOT_covered_by_layer_1(tmp_path):
    """Deliberate residual, pinned rather than hidden.

    realpath resolves symlinks, not hard links. There is no path-level way to
    detect that notes.txt and .env are the same inode. Only the layer-2
    known-value tripwire covers this.
    """
    real = tmp_path / ".env"
    real.write_text("K=v\n")
    hard = tmp_path / "notes.txt"
    os.link(real, hard)
    assert is_credential_read_denied(str(hard)) is False


def test_copy_to_a_template_name_is_NOT_covered_by_layer_1(tmp_path):
    """`cp .env .env.example` bypasses layer 1 by construction. Accepted."""
    src = tmp_path / ".env"
    src.write_text("K=v\n")
    dst = tmp_path / ".env.example"
    dst.write_text(src.read_text())
    assert is_credential_read_denied(str(dst)) is False


# --- additive-only config ----------------------------------------------------

def test_extra_deny_paths_widen_the_set(tmp_path, monkeypatch):
    target = tmp_path / "secrets.txt"
    target.write_text("K=v\n")
    assert is_credential_read_denied(str(target)) is False
    monkeypatch.setenv("HERMES_CREDENTIAL_READ_DENY_EXTRA", str(target))
    assert is_credential_read_denied(str(target)) is True


def test_extra_deny_accepts_a_directory(tmp_path, monkeypatch):
    d = tmp_path / "vault"
    d.mkdir()
    inner = d / "anything.txt"
    inner.write_text("K=v\n")
    monkeypatch.setenv("HERMES_CREDENTIAL_READ_DENY_EXTRA", str(d))
    assert is_credential_read_denied(str(inner)) is True


def test_there_is_no_kill_switch(tmp_path, monkeypatch):
    """No env var may DISABLE the boundary -- only widen it."""
    target = tmp_path / ".env"
    target.write_text("K=v\n")
    for var in (
        "HERMES_CREDENTIAL_READ_DENY_EXTRA",
        "HERMES_REDACT_SECRETS",
        "HERMES_CREDENTIAL_READ_DENY",
        "HERMES_DISABLE_CREDENTIAL_READ_DENY",
    ):
        monkeypatch.setenv(var, "")
    assert is_credential_read_denied(str(target)) is True
    monkeypatch.setenv("HERMES_DISABLE_CREDENTIAL_READ_DENY", "1")
    monkeypatch.setenv("HERMES_REDACT_SECRETS", "0")
    assert is_credential_read_denied(str(target)) is True


# --- the error message -------------------------------------------------------

def test_error_message_is_returned_only_for_denied_paths(tmp_path):
    denied = tmp_path / ".env"
    denied.write_text("K=v\n")
    allowed = tmp_path / ".env.example"
    allowed.write_text("K=\n")
    assert get_credential_read_error(str(allowed)) is None
    msg = get_credential_read_error(str(denied))
    assert msg is not None
    assert "cannot be disabled" in msg
    assert "rotate_credential.sh" in msg
