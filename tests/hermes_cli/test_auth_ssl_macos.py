"""Tests for hermes_cli.auth._default_verify platform-aware fallback.

On macOS with Homebrew Python, the system OpenSSL cannot locate the
system trust store, so we explicitly load certifi's bundle. On other
platforms we defer to httpx's own default (which itself uses certifi).

Most tests use monkeypatching — no real SSL handshakes. A handful use
an openssl-generated self-signed cert via the `real_bundle_file`
fixture because `ssl.create_default_context(cafile=...)` parses the
bundle and refuses stubs.
"""

import shutil
import ssl
import subprocess
from pathlib import Path

import pytest

from agent.ssl_verify import CA_BUNDLE_ENV_VARS
from hermes_cli.auth import _default_verify, _resolve_verify


@pytest.fixture
def real_bundle_file(tmp_path: Path) -> str:
    """Return a path to a real openssl-generated self-signed cert.

    Skips the test when the `openssl` binary isn't on PATH, so CI images
    without it degrade gracefully instead of erroring out.
    """
    if shutil.which("openssl") is None:
        pytest.skip("openssl binary not available")
    cert = tmp_path / "ca.pem"
    key = tmp_path / "key.pem"
    result = subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key), "-out", str(cert),
            "-sha256", "-days", "1", "-nodes",
            "-subj", "/CN=test",
        ],
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(f"openssl failed: {result.stderr.decode('utf-8', 'ignore')[:200]}")
    return str(cert)


class TestDefaultVerify:
    @pytest.mark.macos_only
    def test_returns_ssl_context_on_darwin(self):
        result = _default_verify()
        assert isinstance(result, ssl.SSLContext)


    @pytest.mark.macos_only
    def test_darwin_falls_back_to_true_when_certifi_missing(self, monkeypatch):
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "certifi":
                raise ImportError("simulated missing certifi")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        assert _default_verify() is True


class TestResolveVerifyIntegration:
    """_resolve_verify should defer to _default_verify in the no-CA path."""


    @pytest.mark.linux_only
    def test_no_ca_uses_default_verify_on_linux(self, monkeypatch):
        for var in CA_BUNDLE_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        assert _resolve_verify() is True



    def test_insecure_wins_over_everything(self, monkeypatch, tmp_path):
        bundle = tmp_path / "ca.pem"
        bundle.write_text("stub")
        monkeypatch.setenv("HERMES_CA_BUNDLE", str(bundle))
        assert _resolve_verify(insecure=True) is False

    def test_env_bundle_precedence_matches_canonical_order(self, monkeypatch, tmp_path):
        paths = {}
        for env_var in CA_BUNDLE_ENV_VARS:
            path = tmp_path / f"{env_var.lower()}.pem"
            path.write_text("stub")
            paths[env_var] = str(path)
            monkeypatch.setenv(env_var, str(path))

        seen = {}
        sentinel = object()

        def fake_context(*, cafile):
            seen["cafile"] = cafile
            return sentinel

        monkeypatch.setattr(ssl, "create_default_context", fake_context)
        assert _resolve_verify() is sentinel
        assert seen["cafile"] == paths[CA_BUNDLE_ENV_VARS[0]]

    def test_curl_ca_bundle_is_honoured(self, monkeypatch, tmp_path):
        for env_var in CA_BUNDLE_ENV_VARS:
            monkeypatch.delenv(env_var, raising=False)
        bundle = tmp_path / "curl.pem"
        bundle.write_text("stub")
        monkeypatch.setenv("CURL_CA_BUNDLE", str(bundle))

        sentinel = object()
        monkeypatch.setattr(ssl, "create_default_context", lambda **kwargs: sentinel)
        assert _resolve_verify() is sentinel
