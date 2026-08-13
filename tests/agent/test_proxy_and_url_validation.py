"""Tests for malformed proxy env var and base URL validation.

Salvaged from PR #6403 by MestreY0d4-Uninter — validates that the agent
surfaces clear errors instead of cryptic httpx ``Invalid port`` exceptions
when proxy env vars or custom endpoint URLs are malformed.
"""
from __future__ import annotations

import os

import pytest

from agent.auxiliary_client import _validate_base_url, _validate_proxy_env_urls


# -- proxy env validation ------------------------------------------------


def test_proxy_env_accepts_normal_values(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:6153")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example.com:8443")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    _validate_proxy_env_urls()  # should not raise


def test_proxy_env_accepts_empty(monkeypatch):
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    _validate_proxy_env_urls()  # should not raise


def test_proxy_env_normalizes_socks_alias(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:1080/")
    _validate_proxy_env_urls()
    assert os.environ["ALL_PROXY"] == "socks5://127.0.0.1:1080/"


@pytest.mark.parametrize("key", [
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
])
def test_proxy_env_rejects_malformed_port(monkeypatch, key):
    monkeypatch.setenv(key, "http://127.0.0.1:6153export")
    # Windows environment names are case-insensitive and `os.environ`
    # canonicalizes them to upper case, so `http_proxy` and `HTTP_PROXY` are one
    # variable there and the error can only name the canonical spelling. On
    # POSIX they are genuinely distinct and the exact spelling must be echoed,
    # otherwise the message points the user at a variable they have not set.
    expected_key = key.upper() if os.name == "nt" else key
    with pytest.raises(
        RuntimeError,
        match=rf"Malformed proxy environment variable {expected_key}=.*6153export",
    ):
        _validate_proxy_env_urls()


# -- base URL validation -------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://api.example.com/v1",
    "http://127.0.0.1:6153/v1",
    "acp://copilot",
    "",
    None,
])
def test_base_url_accepts_valid(url):
    _validate_base_url(url)  # should not raise


def test_base_url_rejects_malformed_port():
    with pytest.raises(RuntimeError, match="Malformed custom endpoint URL"):
        _validate_base_url("http://127.0.0.1:6153export")
