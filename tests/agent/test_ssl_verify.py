"""Tests for agent.ssl_verify.resolve_httpx_verify."""

import ssl

import certifi
import pytest

from agent.ssl_verify import CA_BUNDLE_ENV_VARS, resolve_ca_bundle_env, resolve_httpx_verify

_CA_ENV_VARS = CA_BUNDLE_ENV_VARS


@pytest.fixture
def clean_ca_env(monkeypatch):
    for var in _CA_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch




def test_hermes_ca_bundle_returns_ssl_context(clean_ca_env, monkeypatch):
    monkeypatch.setenv("HERMES_CA_BUNDLE", certifi.where())
    result = resolve_httpx_verify()
    assert isinstance(result, ssl.SSLContext)






def test_default_without_env_is_true(clean_ca_env):
    assert resolve_httpx_verify() is True


def test_ca_bundle_env_uses_canonical_precedence(clean_ca_env, tmp_path):
    paths = {}
    for env_var in CA_BUNDLE_ENV_VARS:
        path = tmp_path / f"{env_var.lower()}.pem"
        path.write_text("stub")
        paths[env_var] = str(path)
        clean_ca_env.setenv(env_var, str(path))

    for expected in CA_BUNDLE_ENV_VARS:
        assert resolve_ca_bundle_env() == paths[expected]
        clean_ca_env.delenv(expected)

    assert resolve_ca_bundle_env() == ""


def test_curl_ca_bundle_is_honoured(clean_ca_env):
    clean_ca_env.setenv("CURL_CA_BUNDLE", certifi.where())
    result = resolve_httpx_verify()
    assert isinstance(result, ssl.SSLContext)
