"""Tests for agent.ssl_verify.resolve_httpx_verify."""

import ssl

import certifi
import pytest

from agent.ssl_verify import resolve_httpx_verify

_CA_ENV_VARS = ("HERMES_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")


@pytest.fixture
def clean_ca_env(monkeypatch):
    for var in _CA_ENV_VARS:
        monkeypatch.delenv(var, raising=False)




def test_hermes_ca_bundle_returns_ssl_context(clean_ca_env, monkeypatch):
    monkeypatch.setenv("HERMES_CA_BUNDLE", certifi.where())
    result = resolve_httpx_verify()
    assert isinstance(result, ssl.SSLContext)






def test_default_without_env_is_true(clean_ca_env):
    assert resolve_httpx_verify() is True


def test_ssl_verify_false_allowed_for_local_host(clean_ca_env):
    assert resolve_httpx_verify(ssl_verify=False, base_url="http://localhost:11434/v1") is False
    assert resolve_httpx_verify(ssl_verify=False, base_url="https://127.0.0.1:8443/v1") is False
    assert resolve_httpx_verify(ssl_verify=False, base_url="http://ollama:11434/v1") is False


def test_ssl_verify_false_keeps_legacy_behavior_without_base_url(clean_ca_env):
    # Callers that don't know the base_url keep the historical warn-and-proceed
    # behavior so genuinely local setups whose URL is resolved later still work.
    assert resolve_httpx_verify(ssl_verify=False) is False


def test_ssl_verify_false_refused_for_public_host(clean_ca_env):
    with pytest.raises(ValueError, match="public endpoint"):
        resolve_httpx_verify(ssl_verify=False, base_url="https://api.openai.com/v1")
    with pytest.raises(ValueError, match="public endpoint"):
        resolve_httpx_verify(ssl_verify=False, base_url="https://ollama.example.com/v1")
