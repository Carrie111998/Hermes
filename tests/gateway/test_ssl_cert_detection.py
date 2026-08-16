"""Regression tests for gateway SSL certificate environment repair."""

from types import SimpleNamespace


def test_ensure_ssl_certs_ignores_stale_ssl_cert_file(monkeypatch, tmp_path):
    """A missing SSL_CERT_FILE should be treated as unset, not trusted."""
    import os
    import ssl
    import sys

    from gateway.run import _ensure_ssl_certs

    cert_file = tmp_path / "cacert.pem"
    cert_file.write_text("dummy cert bundle", encoding="utf-8")
    stale_file = tmp_path / "missing.pem"

    original_exists = os.path.exists

    def exists(path):
        if str(path).startswith("/etc/"):
            return False
        return original_exists(path)

    monkeypatch.setattr(os.path, "exists", exists)

    monkeypatch.setenv("SSL_CERT_FILE", str(stale_file))
    monkeypatch.setattr(
        ssl,
        "get_default_verify_paths",
        lambda: SimpleNamespace(cafile=None, openssl_cafile=None),
    )
    monkeypatch.setitem(
        sys.modules,
        "certifi",
        SimpleNamespace(where=lambda: str(cert_file)),
    )

    _ensure_ssl_certs()

    assert stale_file.exists() is False
    assert __import__("os").environ["SSL_CERT_FILE"] == str(cert_file)


def test_ensure_ssl_certs_keeps_existing_ssl_cert_file(monkeypatch, tmp_path):
    """A valid user-provided SSL_CERT_FILE must not be overwritten."""
    from gateway.run import _ensure_ssl_certs

    cert_file = tmp_path / "existing.pem"
    cert_file.write_text("dummy cert bundle", encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_file))

    _ensure_ssl_certs()

    assert __import__("os").environ["SSL_CERT_FILE"] == str(cert_file)


def test_ensure_ssl_certs_prefers_system_bundle_over_certifi(monkeypatch, tmp_path):
    """Managed roots must win over certifi after a host or Hermes update."""
    import os
    import ssl
    import sys

    from gateway.run import _ensure_ssl_certs

    system_bundle = "/etc/ssl/certs/ca-certificates.crt"
    certifi_bundle = tmp_path / "certifi.pem"
    certifi_bundle.write_text("certifi", encoding="utf-8")

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(
        ssl,
        "get_default_verify_paths",
        lambda: SimpleNamespace(cafile=None, openssl_cafile=None),
    )
    original_exists = os.path.exists

    def exists(path):
        if path == system_bundle:
            return True
        return original_exists(path)

    monkeypatch.setattr(os.path, "exists", exists)
    monkeypatch.setitem(
        sys.modules,
        "certifi",
        SimpleNamespace(where=lambda: str(certifi_bundle)),
    )

    _ensure_ssl_certs()

    assert os.environ["SSL_CERT_FILE"] == system_bundle
