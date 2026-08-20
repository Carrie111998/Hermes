"""Tests for the re-auth sidecar timestamp written by setup.py (Part 1 of the
oauth-reauth-expiry-check feature).

The sidecar (``google_token_reauth_at.json``, next to each identity's
``google_token.json``) is the only reliable anchor for estimating Google's
~7-day Testing-mode refresh-token expiry window, because the token file's own
mtime is rewritten on every routine access-token refresh, not just a full
re-auth. These tests confirm:

  * the sidecar is written only after a successful ``--auth-code`` exchange
  * it is scoped per-identity (writing one identity's sidecar never touches
    another identity's token/sidecar files)
  * it works for ANY identity registered in ``_google_identities.py`` --
    nothing here is hardcoded to "jid" or "zarkash" specifically, since the
    module resolves everything through ``_resolve_identity()``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


SETUP_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/setup.py"
)
IDENTITIES_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/_google_identities.py"
)


def _load_module(name: str, path: Path, monkeypatch, hermes_home: Path):
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    scripts_dir = str(path.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    # _google_identities / _hermes_home compute HERMES_HOME once, at import
    # time, and normal `import _google_identities` (as setup.py does) caches
    # that in sys.modules by name. Across tests that each monkeypatch a
    # DIFFERENT HERMES_HOME, a stale cached copy would silently keep pointing
    # every later test's setup.py module at the FIRST test's tmp_path. Evict
    # both before every load so each test's setup.py re-imports them fresh
    # against its own current HERMES_HOME.
    for stale in ("_google_identities", "_hermes_home"):
        sys.modules.pop(stale, None)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup_module(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    return _load_module("reauth_sidecar_setup_module", SETUP_PATH, monkeypatch, hermes_home)


@pytest.fixture
def identities_module(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    return _load_module(
        "reauth_sidecar_identities_module", IDENTITIES_PATH, monkeypatch, hermes_home
    )


def _fake_successful_exchange(module, monkeypatch, *, identity: str):
    """Drive exchange_auth_code() to its success path without real OAuth I/O.

    Mocks only the network/PKCE-flow bits (CLIENT_SECRET_PATH existence,
    _load_pending_auth, the google_auth_oauthlib Flow); everything about
    identity resolution and file writing runs for real so these tests
    exercise the actual sidecar-writing code path.
    """
    module._resolve_identity(identity)
    module.CLIENT_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    module.CLIENT_SECRET_PATH.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        module,
        "_load_pending_auth",
        lambda: {"state": "s", "code_verifier": "v", "redirect_uri": "http://localhost:1"},
    )

    fake_creds = MagicMock()
    fake_creds.to_json.return_value = json.dumps({"token": "tok123", "refresh_token": "rt"})
    fake_creds.granted_scopes = None

    fake_flow = MagicMock()
    fake_flow.credentials = fake_creds
    fake_flow.fetch_token.return_value = None

    fake_flow_cls = MagicMock()
    fake_flow_cls.from_client_secrets_file.return_value = fake_flow

    fake_module = MagicMock()
    fake_module.Flow = fake_flow_cls
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", fake_module)
    monkeypatch.setattr(module, "_ensure_deps", lambda: None)

    module.exchange_auth_code("fake-code-not-a-url")


class TestReauthSidecarWrittenOnSuccess:
    def test_sidecar_written_after_successful_exchange(self, setup_module, monkeypatch):
        _fake_successful_exchange(setup_module, monkeypatch, identity="jid")

        assert setup_module.REAUTH_SIDECAR_PATH.exists()
        payload = json.loads(setup_module.REAUTH_SIDECAR_PATH.read_text(encoding="utf-8"))
        assert payload["identity"] == "jid"
        assert isinstance(payload["recorded_at_epoch"], float)
        assert payload["recorded_at"]  # ISO8601 string, non-empty

    def test_sidecar_path_is_next_to_token_path(self, setup_module, monkeypatch):
        _fake_successful_exchange(setup_module, monkeypatch, identity="jid")
        assert setup_module.REAUTH_SIDECAR_PATH.parent == setup_module.TOKEN_PATH.parent
        assert setup_module.REAUTH_SIDECAR_PATH.name == "google_token_reauth_at.json"

    def test_no_sidecar_before_any_exchange(self, setup_module):
        setup_module._resolve_identity("jid")
        assert not setup_module.REAUTH_SIDECAR_PATH.exists()

    def test_works_for_zarkash_identity_too_without_any_code_change(
        self, setup_module, monkeypatch
    ):
        """Confirms the sidecar mechanism is identity-agnostic: it works for
        zarkash purely because _resolve_identity() re-derives every path from
        _google_identities.py's registry -- nothing in setup.py branches on
        the identity name itself."""
        _fake_successful_exchange(setup_module, monkeypatch, identity="zarkash")

        assert setup_module.REAUTH_SIDECAR_PATH.exists()
        payload = json.loads(setup_module.REAUTH_SIDECAR_PATH.read_text(encoding="utf-8"))
        assert payload["identity"] == "zarkash"


class TestReauthSidecarPerIdentityIsolation:
    def test_writing_one_identity_sidecar_does_not_touch_the_others(
        self, setup_module, monkeypatch
    ):
        _fake_successful_exchange(setup_module, monkeypatch, identity="jid")
        jid_sidecar = setup_module.REAUTH_SIDECAR_PATH
        jid_token_dir = setup_module.TOKEN_PATH.parent

        # Now perform a second, independent exchange for zarkash.
        _fake_successful_exchange(setup_module, monkeypatch, identity="zarkash")
        zee_sidecar = setup_module.REAUTH_SIDECAR_PATH

        assert jid_sidecar != zee_sidecar
        assert jid_sidecar.parent != zee_sidecar.parent
        # jid's sidecar must still exist, be under jid's dir, and be
        # untouched by the zarkash exchange that followed it.
        assert jid_sidecar.exists()
        assert jid_sidecar.parent == jid_token_dir
        jid_payload = json.loads(jid_sidecar.read_text(encoding="utf-8"))
        assert jid_payload["identity"] == "jid"

    def test_sidecar_dirs_never_overlap_across_registered_identities(
        self, identities_module
    ):
        """Structural check mirroring test_setup_module_credential_dirs_never_overlap:
        every registered identity's credential (and therefore sidecar)
        directory must be distinct. Iterates the real registry rather than
        naming identities, so a future third/fourth identity is covered
        automatically."""
        dirs = [
            entry["credentials_dir"]
            for entry in identities_module.IDENTITIES.values()
        ]
        assert len(dirs) == len(set(dirs)), "duplicate credential dirs in IDENTITIES registry"
