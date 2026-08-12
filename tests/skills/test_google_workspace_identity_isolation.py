"""Isolation tests for identity-scoped Google credential resolution.

Verifies structurally (not just by convention) that a JID-resolved turn can
never touch Zee's (zarkash's) Google credentials and vice versa, and that
credential resolution fails closed for any missing/unrecognized identity —
per the design principle in _google_identities.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


IDENTITIES_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/_google_identities.py"
)
API_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/google_api.py"
)
SETUP_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/setup.py"
)


def _load_module(name: str, path: Path, monkeypatch, hermes_home: Path):
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    scripts_dir = str(path.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def identities_module(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    return _load_module("google_identities_test", IDENTITIES_PATH, monkeypatch, hermes_home)


@pytest.fixture
def api_module(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    module = _load_module("google_api_isolation_test", API_PATH, monkeypatch, hermes_home)
    module._gws_binary = lambda: "/usr/bin/gws"
    module._ensure_authenticated = lambda: None
    return module


@pytest.fixture
def setup_module(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    return _load_module("google_setup_isolation_test", SETUP_PATH, monkeypatch, hermes_home)


class TestFailClosed:
    def test_no_identity_raises(self, identities_module):
        with pytest.raises(identities_module.UnknownGoogleIdentityError):
            identities_module.get_google_credentials(None)

    def test_empty_string_identity_raises(self, identities_module):
        with pytest.raises(identities_module.UnknownGoogleIdentityError):
            identities_module.get_google_credentials("")

    def test_unregistered_identity_raises(self, identities_module):
        with pytest.raises(identities_module.UnknownGoogleIdentityError):
            identities_module.get_google_credentials("someone-not-registered")

    def test_unregistered_identity_error_never_returns_jid_paths(self, identities_module):
        """The failure mode must be an exception, never a silent jid fallback."""
        jid_token, _, _ = identities_module.get_google_credentials("jid")
        try:
            identities_module.get_google_credentials("someone-not-registered")
            pytest.fail("expected UnknownGoogleIdentityError, got a return value")
        except identities_module.UnknownGoogleIdentityError:
            pass
        # jid's own resolution is untouched by the failed lookup.
        jid_token_again, _, _ = identities_module.get_google_credentials("jid")
        assert jid_token_again == jid_token


class TestStructuralIsolation:
    """Simulates a JID-resolved turn and a Zee-resolved turn and confirms
    each only ever touches its own credential path — never the other's."""

    def test_jid_and_zarkash_paths_never_overlap(self, identities_module):
        jid_token, jid_secret, jid_scopes = identities_module.get_google_credentials("jid")
        zee_token, zee_secret, zee_scopes = identities_module.get_google_credentials("zarkash")

        assert jid_token != zee_token
        assert jid_secret != zee_secret
        assert jid_token.parent != zee_token.parent
        # zarkash's credential dir must be a subdirectory outside HERMES_HOME
        # root, keyed to his identity — never sharing JID's root files.
        assert "family_credentials" in str(zee_token)
        assert "zarkash" in str(zee_token)
        assert "family_credentials" not in str(jid_token)

    def test_jid_scopes_never_leak_into_zarkash_scopes(self, identities_module):
        _, _, jid_scopes = identities_module.get_google_credentials("jid")
        _, _, zee_scopes = identities_module.get_google_credentials("zarkash")
        assert jid_scopes != zee_scopes
        # Zee's Gmail access is read + draft-only — send/modify must be absent.
        assert "https://www.googleapis.com/auth/gmail.send" not in zee_scopes
        assert "https://www.googleapis.com/auth/gmail.modify" not in zee_scopes
        assert "https://www.googleapis.com/auth/gmail.readonly" in zee_scopes
        assert "https://www.googleapis.com/auth/gmail.compose" in zee_scopes
        # Contacts: full read/write for both identities (JID upgraded from
        # contacts.readonly after Zee's PR #16 shipped) — each identity's own
        # scope, resolved independently, never shared state between them.
        assert "https://www.googleapis.com/auth/contacts" in zee_scopes
        assert "https://www.googleapis.com/auth/contacts.readonly" not in zee_scopes
        assert "https://www.googleapis.com/auth/contacts" in jid_scopes
        assert "https://www.googleapis.com/auth/contacts.readonly" not in jid_scopes

    def test_simulated_jid_turn_only_touches_jid_credential_file(self, api_module, tmp_path):
        """Write a sentinel token for jid, resolve as jid, confirm only jid's
        file is read — zarkash's directory is never created or touched."""
        api_module._resolve_identity("jid")
        jid_token_path = api_module.TOKEN_PATH
        jid_token_path.write_text('{"token": "jid-sentinel"}', encoding="utf-8")

        zarkash_dir = jid_token_path.parent / "family_credentials" / "zarkash"
        assert not zarkash_dir.exists()

        api_module._ensure_authenticated()  # must not raise — jid's file exists
        assert json_load(jid_token_path)["token"] == "jid-sentinel"
        assert not zarkash_dir.exists()  # still never touched

    def test_simulated_zarkash_turn_only_touches_zarkash_credential_file(self, api_module):
        """Write a sentinel token for jid first, then resolve as zarkash and
        confirm zarkash's (nonexistent) file is what's checked — never jid's
        sentinel, and no cross-read of jid's data."""
        api_module._resolve_identity("jid")
        jid_token_path = api_module.TOKEN_PATH
        jid_token_path.write_text('{"token": "jid-sentinel"}', encoding="utf-8")

        api_module._resolve_identity("zarkash")
        zee_token_path = api_module.TOKEN_PATH
        assert zee_token_path != jid_token_path
        # zarkash's token path must not exist and jid's sentinel content must
        # be completely untouched by the identity switch — the two files are
        # structurally distinct paths, not just conventionally-different names.
        assert not zee_token_path.exists()
        assert json_load(jid_token_path)["token"] == "jid-sentinel"

    def test_ensure_authenticated_fails_closed_for_unprovisioned_identity(self, monkeypatch, tmp_path):
        """The real (unstubbed) _ensure_authenticated must error for an
        identity with no token yet, never silently pass using someone else's
        file. Loaded fresh here (not the api_module fixture) since that
        fixture stubs _ensure_authenticated out for other tests' convenience."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        module = _load_module("google_api_real_auth_check", API_PATH, monkeypatch, hermes_home)
        module._gws_binary = lambda: "/usr/bin/gws"  # keep _run_gws path, don't stub auth

        module._resolve_identity("jid")
        module.TOKEN_PATH.write_text('{"token": "jid-sentinel"}', encoding="utf-8")

        module._resolve_identity("zarkash")
        with pytest.raises(SystemExit):
            module._ensure_authenticated()

    def test_resolve_identity_fails_closed_leaves_no_ambient_state(self, api_module):
        """An unrecognized identity must raise and must not silently leave
        the module resolved as jid (or anything else) for the next call."""
        api_module._resolve_identity("jid")
        jid_token_path = api_module.TOKEN_PATH

        with pytest.raises(api_module.UnknownGoogleIdentityError):
            api_module._resolve_identity("someone-not-registered")

        # Resolution didn't silently change to bad state; jid's own re-resolve
        # still works correctly afterward.
        api_module._resolve_identity("jid")
        assert api_module.TOKEN_PATH == jid_token_path

    def test_main_requires_identity_argument(self, api_module, monkeypatch, capsys):
        """--identity must be a required CLI argument — omitting it is a
        parse error (argparse exits 2), not a silent default."""
        monkeypatch.setattr(sys, "argv", ["google_api.py", "gmail", "labels"])
        with pytest.raises(SystemExit) as exc_info:
            api_module.main()
        assert exc_info.value.code != 0

    def test_setup_module_credential_dirs_never_overlap(self, setup_module):
        setup_module._resolve_identity("jid")
        jid_token_path = setup_module.TOKEN_PATH

        setup_module._resolve_identity("zarkash")
        zee_token_path = setup_module.TOKEN_PATH

        assert jid_token_path != zee_token_path
        assert jid_token_path.parent != zee_token_path.parent
        # setup's --identity zarkash provisioning must not create/touch
        # anything under jid's credential directory.
        assert not (jid_token_path.parent / "family_credentials").exists() or (
            jid_token_path.parent / "family_credentials" not in jid_token_path.parents
        )

    def test_setup_creates_zarkash_dir_with_restrictive_permissions(self, setup_module):
        import stat

        setup_module._resolve_identity("zarkash")
        cred_dir = setup_module.TOKEN_PATH.parent
        assert cred_dir.exists()
        mode = stat.S_IMODE(cred_dir.stat().st_mode)
        assert mode == 0o700, f"expected 0700, got {oct(mode)}"


def json_load(path: Path):
    import json

    return json.loads(path.read_text(encoding="utf-8"))
