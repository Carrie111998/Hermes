"""Regression tests for gateway SSL certificate environment repair."""

from types import SimpleNamespace


def test_macos_ca_candidates_prefer_certifi_over_compiled_default():
    from gateway.run import _ordered_ca_bundle_candidates

    candidates = _ordered_ca_bundle_candidates(
        "darwin",
        SimpleNamespace(
            cafile="/etc/ssl/cert.pem",
            openssl_cafile="/etc/ssl/cert.pem",
        ),
        "/python/site-packages/certifi/cacert.pem",
    )

    assert candidates[:2] == (
        "/python/site-packages/certifi/cacert.pem",
        "/etc/ssl/cert.pem",
    )


def test_non_macos_ca_candidates_keep_compiled_default_first():
    from gateway.run import _ordered_ca_bundle_candidates

    candidates = _ordered_ca_bundle_candidates(
        "linux",
        SimpleNamespace(
            cafile="/etc/ssl/certs/ca-certificates.crt",
            openssl_cafile=None,
        ),
        "/python/site-packages/certifi/cacert.pem",
    )

    assert candidates[:2] == (
        "/etc/ssl/certs/ca-certificates.crt",
        "/python/site-packages/certifi/cacert.pem",
    )


def test_ensure_ssl_certs_ignores_stale_ssl_cert_file(monkeypatch, tmp_path):
    """A missing SSL_CERT_FILE should be treated as unset, not trusted."""
    import ssl
    import sys

    from gateway.run import _ensure_ssl_certs

    cert_file = tmp_path / "cacert.pem"
    cert_file.write_text("dummy cert bundle", encoding="utf-8")
    stale_file = tmp_path / "missing.pem"

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


