from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from hermes_cli import secrets_cli


_KEY_MAP = {"AcqLens Test Login": "ACQLENS_TEST_LOGIN"}


def _config(*, key_map=_KEY_MAP, enabled: bool = True) -> dict:
    return {
        "secrets": {
            "bitwarden": {
                "enabled": enabled,
                "access_token_env": "BWS_ACCESS_TOKEN",
                "project_id": "proj-123",
                "server_url": "https://vault.bitwarden.com",
                "override_existing": False,
                "key_map": key_map,
            }
        }
    }


def test_setup_test_fetch_forwards_configured_key_map(monkeypatch, tmp_path):
    captured = {}
    saved_config = {}
    monkeypatch.setattr(secrets_cli, "load_config", lambda: _config())
    monkeypatch.setattr(
        secrets_cli.bw,
        "find_bws",
        lambda install_if_missing=False: Path("/fake/bws"),
    )
    monkeypatch.setattr(secrets_cli, "_bws_version", lambda _binary: "bws v2.0.0")
    monkeypatch.setattr(secrets_cli, "save_env_value", lambda _name, _value: None)
    monkeypatch.setattr(secrets_cli, "get_env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(
        secrets_cli,
        "save_config",
        lambda cfg: saved_config.update(cfg),
    )

    def _fake_fetch(**kwargs):
        captured.update(kwargs)
        return {"ACQLENS_TEST_LOGIN": "opaque"}, []

    monkeypatch.setattr(secrets_cli.bw, "fetch_bitwarden_secrets", _fake_fetch)

    result = secrets_cli.cmd_setup(
        Namespace(
            access_token="0.test-token",
            project_id="proj-123",
            server_url="https://vault.bitwarden.com",
        )
    )

    assert result == 0
    assert captured["key_map"] == _KEY_MAP
    assert captured["use_cache"] is False
    assert saved_config["secrets"]["bitwarden"]["key_map"] == _KEY_MAP


@pytest.mark.parametrize("apply", [False, True])
def test_sync_paths_forward_configured_key_map(monkeypatch, apply):
    captured = {}
    monkeypatch.setattr(secrets_cli, "load_config", lambda: _config())
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.test-token")
    monkeypatch.delenv("ACQLENS_TEST_LOGIN", raising=False)

    def _fake_fetch(**kwargs):
        captured.update(kwargs)
        return {"ACQLENS_TEST_LOGIN": "opaque"}, []

    monkeypatch.setattr(secrets_cli.bw, "fetch_bitwarden_secrets", _fake_fetch)

    result = secrets_cli.cmd_sync(Namespace(apply=apply))

    assert result == 0
    assert captured["key_map"] == _KEY_MAP
    assert captured["use_cache"] is False
    assert ("ACQLENS_TEST_LOGIN" in secrets_cli.os.environ) is apply


@pytest.mark.parametrize("raw_key_map", [None, [], "not-a-map", 7])
def test_sync_treats_non_mapping_key_map_as_empty(monkeypatch, raw_key_map):
    captured = {}
    monkeypatch.setattr(
        secrets_cli,
        "load_config",
        lambda: _config(key_map=raw_key_map),
    )
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.test-token")

    def _fake_fetch(**kwargs):
        captured.update(kwargs)
        return {}, []

    monkeypatch.setattr(secrets_cli.bw, "fetch_bitwarden_secrets", _fake_fetch)

    assert secrets_cli.cmd_sync(Namespace(apply=False)) == 0
    assert captured["key_map"] == {}


def test_sync_normalizes_key_map_keys_and_values_to_strings(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        secrets_cli,
        "load_config",
        lambda: _config(key_map={123: "ACQLENS_TEST_LOGIN", "label": 456}),
    )
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.test-token")

    def _fake_fetch(**kwargs):
        captured.update(kwargs)
        return {}, []

    monkeypatch.setattr(secrets_cli.bw, "fetch_bitwarden_secrets", _fake_fetch)

    assert secrets_cli.cmd_sync(Namespace(apply=False)) == 0
    assert captured["key_map"] == {
        "123": "ACQLENS_TEST_LOGIN",
        "label": "456",
    }
