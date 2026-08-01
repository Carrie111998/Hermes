"""Tests for the preventive SSL CA bundle guard."""

from pathlib import Path

import certifi
import pytest

from agent.errors import SSLConfigurationError
from agent.ssl_guard import verify_ca_bundle, verify_ca_bundle_with_fallback


def test_healthy_bundle_passes(monkeypatch):
    """A real, non-empty certifi bundle must verify without raising."""
    for key in ("HERMES_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(key, raising=False)
    bundle = Path(certifi.where())
    assert bundle.exists()
    assert bundle.stat().st_size > 1024
    verify_ca_bundle()


def test_missing_certifi_bundle_raises_ssl_error(monkeypatch, tmp_path):
    """Point certifi.where() at a non-existent path; expect a clear error."""
    fake = tmp_path / "nope.pem"
    monkeypatch.setattr(certifi, "where", lambda: str(fake))
    with pytest.raises(SSLConfigurationError) as exc:
        verify_ca_bundle()
    message = str(exc.value).lower()
    assert "certifi" in message
    assert "missing" in message
    assert "force-reinstall" in message


def test_empty_certifi_bundle_raises_ssl_error(monkeypatch, tmp_path):
    """Empty file is treated as a corrupted bundle."""
    fake = tmp_path / "empty.pem"
    fake.write_bytes(b"")
    monkeypatch.setattr(certifi, "where", lambda: str(fake))
    with pytest.raises(SSLConfigurationError) as exc:
        verify_ca_bundle()
    assert "too small" in str(exc.value).lower()


@pytest.mark.parametrize("env_var", ["HERMES_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"])
def test_missing_explicit_ca_bundle_env_raises_before_httpx(monkeypatch, tmp_path, env_var):
    """Bad CA-bundle env vars should be reported before OpenAI/httpx init."""
    fake = tmp_path / "missing.pem"
    monkeypatch.setenv(env_var, str(fake))
    with pytest.raises(SSLConfigurationError) as exc:
        verify_ca_bundle()
    message = str(exc.value)
    assert env_var in message
    assert str(fake) in message
    assert "force-reinstall" in message


def test_invalid_explicit_ca_bundle_env_raises(monkeypatch, tmp_path):
    """An existing but invalid explicit bundle should get a user-facing error."""
    fake = tmp_path / "broken.pem"
    fake.write_text("not a cert bundle", encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(fake))
    with pytest.raises(SSLConfigurationError) as exc:
        verify_ca_bundle()
    assert "cannot be loaded" in str(exc.value)


def test_verify_ca_bundle_with_fallback_keeps_same_contract(monkeypatch, tmp_path):
    """The compatibility wrapper still rejects broken explicit CA paths."""
    fake = tmp_path / "missing.pem"
    monkeypatch.setenv("SSL_CERT_FILE", str(fake))
    with pytest.raises(SSLConfigurationError):
        verify_ca_bundle_with_fallback()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_skip_env_var_bypasses_guard(monkeypatch, tmp_path, value):
    """HERMES_SKIP_SSL_GUARD is an intentional escape hatch for managed trust stores."""
    fake = tmp_path / "missing.pem"
    monkeypatch.setenv("HERMES_SKIP_SSL_GUARD", value)
    monkeypatch.setenv("SSL_CERT_FILE", str(fake))
    verify_ca_bundle()
    verify_ca_bundle_with_fallback()


# --- truststore / NotImplementedError handling --------------------------------
# When the optional `truststore` extra injects the OS trust store, the
# SSLContext returned by ssl.create_default_context() can raise
# NotImplementedError from get_ca_certs() because the OS-backed context
# doesn't expose CA enumeration (see truststore docs).  The guard must
# NOT treat that as an empty-corrupt-bundle failure.


def test_get_ca_certs_not_implemented_does_not_raise(monkeypatch, tmp_path):
    """An SSLContext whose get_ca_certs() raises NotImplementedError must
    be treated as a successfully loaded (but uninspectable) bundle, not as
    an empty / corrupt one.  This is the truststore code path.
    """
    import ssl as _ssl

    fake = tmp_path / "corporate-ca.pem"
    # Minimal but valid CA cert (self-signed, CA:TRUE) so the file-size +
    # create_default_context checks pass before we reach the get_ca_certs
    # probe.
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
    fake.write_text(VALID_CA_PEM, encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(fake))

    real_create_default_context = _ssl.create_default_context

    class _StubContext:
        """Mimics truststore's context: load succeeds, enumeration fails."""

        def __init__(self, *args, **kwargs):
            pass

        def get_ca_certs(self):
            raise NotImplementedError("truststore does not expose CA enumeration")

    def _patched_create_default_context(*args, **kwargs):
        # Only redirect when we're loading our fake bundle; let the certifi
        # path (no cafile kwarg) go through to the real implementation.
        if kwargs.get("cafile"):
            return _StubContext()
        return real_create_default_context(*args, **kwargs)

    monkeypatch.setattr(_ssl, "create_default_context", _patched_create_default_context)

    # Must NOT raise SSLConfigurationError despite get_ca_certs() blowing up.
    verify_ca_bundle()


def test_get_ca_certs_empty_list_still_raises(monkeypatch, tmp_path):
    """A genuine empty-list return (not NotImplementedError) must still fail
    so a truly corrupt bundle is caught.  Regression guard: the
    NotImplementedError path must not silently swallow the real-empty case.
    """
    import ssl as _ssl

    fake = tmp_path / "empty-but-valid.pem"
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
    fake.write_text(VALID_CA_PEM, encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(fake))

    real_create_default_context = _ssl.create_default_context

    class _EmptyStubContext:
        def __init__(self, *args, **kwargs):
            pass

        def get_ca_certs(self):
            return []

    def _patched_create_default_context(*args, **kwargs):
        if kwargs.get("cafile"):
            return _EmptyStubContext()
        return real_create_default_context(*args, **kwargs)

    monkeypatch.setattr(_ssl, "create_default_context", _patched_create_default_context)

    with pytest.raises(SSLConfigurationError) as exc:
        verify_ca_bundle()
    assert "did not load any certificates" in str(exc.value)
