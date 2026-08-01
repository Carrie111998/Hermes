"""Tests for the optional ``truststore`` OS-trust-store integration in
``agent.ssl_verify``.

The new approach (after PR review) avoids ``truststore.inject_into_ssl()``
at import time — that mutates a process-global default and cannot guarantee
process-wide ordering per Truststore's own docs.  Instead we build a
``truststore.SSLContext`` on demand and pass it as the ``verify=`` argument
to owned HTTP clients, gated by the ``network.trust_store: true`` config
key (per ``AGENTS.md``: behavioral settings go in ``config.yaml``, not env
vars).
"""

from __future__ import annotations

import builtins
import ssl
import sys

import pytest

from agent.ssl_verify import (
    _build_truststore_context,
    resolve_httpx_verify,
    resolve_httpx_verify_with_truststore,
)


# --- _build_truststore_context ----------------------------------------------


def test_build_truststore_context_not_installed_returns_none(monkeypatch):
    """When the ``truststore`` package is absent, returns None (silent no-op)."""
    real_import = builtins.__import__

    def _block_truststore(name, *args, **kwargs):
        if name == "truststore":
            raise ImportError("No module named 'truststore'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_truststore)
    monkeypatch.setattr(sys, "platform", "win32")

    assert _build_truststore_context() is None


def test_build_truststore_context_skipped_on_linux(monkeypatch):
    """On Linux the OS trust store is already bridged via ca-certificates.crt;
    truststore is a no-op, so we skip the import entirely.
    """
    monkeypatch.setattr(sys, "platform", "linux")

    called = {"import": False}
    real_import = builtins.__import__

    def _spy_import(name, *args, **kwargs):
        if name == "truststore":
            called["import"] = True
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _spy_import)
    assert _build_truststore_context() is None
    assert called["import"] is False, "truststore must not be imported on Linux"


def test_build_truststore_context_success_returns_sslcontext(monkeypatch):
    """When ``truststore`` is importable and on Windows/macOS, returns an
    ``ssl.SSLContext`` (or subclass) instance.
    """
    monkeypatch.setattr(sys, "platform", "win32")

    class _FakeSSLContext(ssl.SSLContext):
        pass

    class _FakeTruststoreModule:
        SSLContext = _FakeSSLContext

    real_import = builtins.__import__

    def _stub_import(name, *args, **kwargs):
        if name == "truststore":
            return _FakeTruststoreModule
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _stub_import)
    ctx = _build_truststore_context()
    assert ctx is not None
    assert isinstance(ctx, ssl.SSLContext)


def test_build_truststore_context_failure_returns_none(monkeypatch):
    """If ``truststore.SSLContext()`` raises (e.g. OS store inaccessible),
    returns None so callers fall back to the certifi default.
    """
    monkeypatch.setattr(sys, "platform", "win32")

    class _FailingTruststoreModule:
        class SSLContext:
            def __init__(self):
                raise RuntimeError("OS trust store unavailable")

    real_import = builtins.__import__

    def _stub_import(name, *args, **kwargs):
        if name == "truststore":
            return _FailingTruststoreModule
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _stub_import)
    assert _build_truststore_context() is None


# --- resolve_httpx_verify_with_truststore -----------------------------------


def test_resolve_with_truststore_disabled_returns_true(monkeypatch):
    """When ``trust_store=`` is False (the default), behaves exactly like
    ``resolve_httpx_verify`` — returns ``True`` (httpx/certifi default).
    """
    monkeypatch.delenv("HERMES_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)

    result = resolve_httpx_verify_with_truststore(trust_store=False)
    assert result is True


def test_resolve_with_truststore_enabled_returns_context(monkeypatch):
    """When ``trust_store=`` is True and truststore is available, returns an
    ``ssl.SSLContext`` instead of ``True``.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("HERMES_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)

    class _FakeSSLContext(ssl.SSLContext):
        pass

    class _FakeTruststoreModule:
        SSLContext = _FakeSSLContext

    real_import = builtins.__import__

    def _stub_import(name, *args, **kwargs):
        if name == "truststore":
            return _FakeTruststoreModule
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _stub_import)
    result = resolve_httpx_verify_with_truststore(trust_store=True)
    assert isinstance(result, ssl.SSLContext)


def test_resolve_with_truststore_not_installed_falls_back_to_true(monkeypatch):
    """When ``trust_store=`` is True but truststore is not installed,
    silently falls back to ``True`` (certifi default).
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("HERMES_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)

    real_import = builtins.__import__

    def _block_truststore(name, *args, **kwargs):
        if name == "truststore":
            raise ImportError("No module named 'truststore'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_truststore)
    result = resolve_httpx_verify_with_truststore(trust_store=True)
    assert result is True


def test_resolve_with_truststore_explicit_ca_bundle_overrides_truststore(monkeypatch, tmp_path):
    """An explicit ``ca_bundle`` still takes priority over ``trust_store``
    (matches the priority in ``resolve_httpx_verify``'s docstring).
    """
    # A minimal but valid CA cert (self-signed, CA:TRUE basic constraint)
    # so ssl.create_default_context(cafile=...) can actually load it and
    # build a working SSLContext.
    VALID_CA_PEM = (
        "-----BEGIN CERTIFICATE-----\n"
        "MIICsjCCAZqgAwIBAgIBAjANBgkqhkiG9w0BAQsFADASMRAwDgYDVQQDDAd0ZXN0\n"
        "LWNhMB4XDTI2MDEwMTAwMDAwMFoXDTMwMDEwMTAwMDAwMFowEjEQMA4GA1UEAwwH\n"
        "dGVzdC1jYTCCASIwDQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBAM+pOnxK9587\n"
        "ogBujI83LfyqYHmjTFjhP3rguQlanpPLpupgd+qShiyrIOoT957IMCWmG/RGrS/t\n"
        "5NFREEukbyXn/gSHFsUsUGgr8S9Jnoq5x9FJ8pwwsxq14gmEfdysLDNMqoqkHaOf\n"
        "CotRTso6WkPX9YSnmRA2zUFfT3AYrRvC3bazZJ6n8ioF506BwlcckmoEP0x0vepx\n"
        "ItRkWv0gt3uR/wavqLXcpmrXK7/tIrwWFIB9bZVdsqX8tGkP40rJygZiWznFx7/4\n"
        "dwg4h261ktsLdA8dEIJQbFgn4OXPU1P1HekfWK+C4CqnKZjHMyhCVXmexcfJzP0e\n"
        "2x6Y4k9mdbUCAwEAAaMTMBEwDwYDVR0TAQH/BAUwAwEB/zANBgkqhkiG9w0BAQsF\n"
        "AAOCAQEAP/i+0tCc6QxTeYCNPlMq9MhC8GziSxlKzvw2QfM1vEgiiODLTQuUIHCG\n"
        "mZ+IKnwwdMUhS141gX82nSITz72hjIUDx06wVCQAXql7zj/RTcFx0RCUYALBzCqJ\n"
        "/IAzLIP4/1nkrQa8S1pVj4sSu+BkoWivA1Ntaec04gF6LGNeBmWPfFX4nLCX/Itg\n"
        "NbGBXIKap4LVB+BxLljuo16m9IPhCWWq68XBImfnS/8iIRDplbEYHVEFcJ52noqv\n"
        "tmCaz9VArYVePEoYivgtpKYse+I3KbrsJJEJRHM5H93WQVM7M+RWVSi0UkpC/0UA\n"
        "s59nHXtJ1woNehm+ILox0ED+Rs6OOg==\n"
        "-----END CERTIFICATE-----\n"
    )
    fake = tmp_path / "corp-ca.pem"
    fake.write_text(VALID_CA_PEM, encoding="utf-8")

    result = resolve_httpx_verify_with_truststore(
        ca_bundle=str(fake), trust_store=True,
    )
    # Should return an ssl.SSLContext built from the explicit CA file,
    # NOT the truststore context.
    assert isinstance(result, ssl.SSLContext)


def test_resolve_with_truststore_ssl_verify_false_takes_priority(monkeypatch):
    """``ssl_verify: false`` (insecure mode) still takes priority over
    ``trust_store=True`` — disabling verification is explicit and intentional.
    """
    result = resolve_httpx_verify_with_truststore(
        ssl_verify=False, trust_store=True,
    )
    assert result is False
