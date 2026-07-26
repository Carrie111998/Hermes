"""Tests for atomic persistence in the Google Workspace OAuth setup flow."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


SETUP_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/setup.py"
)


@pytest.fixture
def setup_module(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    spec = importlib.util.spec_from_file_location("google_oauth_setup_test", SETUP_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_setup_persists_client_and_pending_auth_through_atomic_writer(
    setup_module, monkeypatch, tmp_path
):
    source = tmp_path / "client.json"
    client_payload = {"installed": {"client_id": "client-id"}}
    source.write_text(json.dumps(client_payload), encoding="utf-8")
    writes = []
    monkeypatch.setattr(
        setup_module,
        "atomic_write_json",
        lambda path, payload: writes.append((path, payload)),
        raising=False,
    )

    setup_module.store_client_secret(str(source))
    setup_module._save_pending_auth(state="state", code_verifier="verifier")

    assert writes == [
        (setup_module.CLIENT_SECRET_PATH, client_payload),
        (
            setup_module.PENDING_AUTH_PATH,
            {
                "state": "state",
                "code_verifier": "verifier",
                "redirect_uri": setup_module.REDIRECT_URI,
            },
        ),
    ]


def test_setup_check_refresh_persists_through_atomic_writer(setup_module, monkeypatch):
    setup_module.TOKEN_PATH.write_text(
        json.dumps({"token": "ya29.old", "refresh_token": "1//refresh"}),
        encoding="utf-8",
    )

    class FakeCredentials:
        valid = False
        expired = True
        refresh_token = "1//refresh"

        def refresh(self, request):
            self.valid = True
            self.expired = False

        def to_json(self):
            return json.dumps(
                {"token": "ya29.refreshed", "refresh_token": "1//refresh"}
            )

    class FakeCredentialsFactory:
        @staticmethod
        def from_authorized_user_file(filename):
            assert filename == str(setup_module.TOKEN_PATH)
            return FakeCredentials()

    google_module = types.ModuleType("google")
    oauth2_module = types.ModuleType("google.oauth2")
    credentials_module = types.ModuleType("google.oauth2.credentials")
    setattr(credentials_module, "Credentials", FakeCredentialsFactory)
    transport_module = types.ModuleType("google.auth.transport")
    requests_module = types.ModuleType("google.auth.transport.requests")
    setattr(requests_module, "Request", lambda: object())
    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth2_module)
    monkeypatch.setitem(sys.modules, "google.oauth2.credentials", credentials_module)
    monkeypatch.setitem(sys.modules, "google.auth.transport", transport_module)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", requests_module)
    monkeypatch.setattr(setup_module, "_ensure_deps", lambda: None)
    writes = []
    monkeypatch.setattr(
        setup_module,
        "atomic_write_json",
        lambda path, payload: writes.append((path, payload)),
    )

    assert setup_module.check_auth(quiet=True) is True
    assert len(writes) == 1
    assert writes[0][0] == setup_module.TOKEN_PATH
    assert writes[0][1]["token"] == "ya29.refreshed"
    assert writes[0][1]["type"] == "authorized_user"


def test_setup_code_exchange_persists_token_through_atomic_writer(
    setup_module, monkeypatch
):
    setup_module.CLIENT_SECRET_PATH.write_text(
        json.dumps({"installed": {"client_id": "client-id"}}), encoding="utf-8"
    )
    setup_module.PENDING_AUTH_PATH.write_text(
        json.dumps(
            {
                "state": "expected-state",
                "code_verifier": "verifier",
                "redirect_uri": setup_module.REDIRECT_URI,
            }
        ),
        encoding="utf-8",
    )

    class FakeCredentials:
        granted_scopes = list(setup_module.SCOPES)

        def to_json(self):
            return json.dumps(
                {"token": "ya29.new", "refresh_token": "1//refresh"}
            )

    class FakeFlow:
        credentials = FakeCredentials()

        def fetch_token(self, code):
            assert code == "auth-code"

    class FakeFlowFactory:
        @staticmethod
        def from_client_secrets_file(filename, **kwargs):
            assert filename == str(setup_module.CLIENT_SECRET_PATH)
            assert kwargs["state"] == "expected-state"
            assert kwargs["code_verifier"] == "verifier"
            return FakeFlow()

    oauthlib_module = types.ModuleType("google_auth_oauthlib")
    flow_module = types.ModuleType("google_auth_oauthlib.flow")
    setattr(flow_module, "Flow", FakeFlowFactory)
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib", oauthlib_module)
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", flow_module)
    monkeypatch.setattr(setup_module, "_ensure_deps", lambda: None)
    writes = []
    monkeypatch.setattr(
        setup_module,
        "atomic_write_json",
        lambda path, payload: writes.append((path, payload)),
    )

    setup_module.exchange_auth_code("auth-code")

    assert len(writes) == 1
    assert writes[0][0] == setup_module.TOKEN_PATH
    assert writes[0][1]["token"] == "ya29.new"
    assert writes[0][1]["type"] == "authorized_user"
