"""Tests for mTLS client certificate config on MCP HTTP/SSE transports.

Covers:

1. ``_resolve_client_cert`` helper — string, tuple, encrypted-key, validation
   errors, missing-file errors.

2. HTTP (new and legacy SDK paths) forward one normalized SSL context into
   their user-owned ``httpx.AsyncClient`` without the deprecated ``cert=``.

3. SSE path forwards that context via an ``httpx_client_factory`` without
   breaking the OAuth/headers/timeout passthrough.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import http.server
import ipaddress
import ssl
import threading
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@pytest.fixture
def mtls_material(tmp_path):
    """Create a CA plus server/client identities for a real local mTLS probe."""
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Hermes test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    def issue(name, *, server=False):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=1))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(
                x509.ExtendedKeyUsage([
                    ExtendedKeyUsageOID.SERVER_AUTH if server
                    else ExtendedKeyUsageOID.CLIENT_AUTH
                ]),
                critical=False,
            )
        )
        if server:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
        return key, builder.sign(ca_key, hashes.SHA256())

    server_key, server_cert = issue("127.0.0.1", server=True)
    client_key, client_cert = issue("Hermes client")

    def write_cert(name, cert):
        path = tmp_path / name
        path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return path

    def write_key(name, key, encryption=serialization.NoEncryption()):
        path = tmp_path / name
        path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            encryption,
        ))
        return path

    return {
        "ca": write_cert("ca.pem", ca_cert),
        "server_cert": write_cert("server.pem", server_cert),
        "server_key": write_key("server.key", server_key),
        "client_cert": write_cert("client.pem", client_cert),
        "client_key": write_key("client.key", client_key),
        "encrypted_client_key": write_key(
            "client-encrypted.key",
            client_key,
            serialization.BestAvailableEncryption(b"test-passphrase"),
        ),
    }


@contextmanager
def _serve_mtls(material):
    seen_requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):
            seen_requests.append(self.command)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(material["server_cert"], material["server_key"])
    context.load_verify_locations(cafile=material["ca"])
    context.verify_mode = ssl.CERT_REQUIRED
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}/mcp", seen_requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _patch_sdk_async_client(dummy):
    """Patch ``AsyncClient`` on whichever httpx module the MCP SDK uses.

    mcp 2.0 moved the SDK's HTTP stack to ``httpx2``, so patching
    ``httpx.AsyncClient`` no longer intercepts the client Hermes builds for
    the SDK. Resolve the module the same way production does, via
    ``tools.mcp_tool.sdk_httpx``, so these tests follow the SDK rather than
    hardcoding a distribution name.
    """
    from tools.mcp_tool import sdk_httpx

    return patch.object(sdk_httpx(), "AsyncClient", dummy)


# ---------------------------------------------------------------------------
# _resolve_client_cert helper
# ---------------------------------------------------------------------------


class TestResolveClientCert:
    def test_returns_none_when_unset(self):
        from tools.mcp_tool import _resolve_client_cert

        assert _resolve_client_cert("srv", {}) is None
        assert _resolve_client_cert("srv", {"url": "https://x"}) is None

    def test_string_form_single_pem(self, tmp_path):
        from tools.mcp_tool import _resolve_client_cert

        pem = tmp_path / "combined.pem"
        pem.write_text("dummy")

        result = _resolve_client_cert("srv", {"client_cert": str(pem)})
        assert result == str(pem)


    def test_list_form_two_elements(self, tmp_path):
        from tools.mcp_tool import _resolve_client_cert

        cert = tmp_path / "client.crt"
        key = tmp_path / "client.key"
        cert.write_text("cert")
        key.write_text("key")

        result = _resolve_client_cert("srv", {
            "client_cert": [str(cert), str(key)],
        })
        assert result == (str(cert), str(key))


    def test_password_must_be_string(self, tmp_path):
        from tools.mcp_tool import _resolve_client_cert

        cert = tmp_path / "client.crt"
        key = tmp_path / "client.key"
        cert.write_text("cert")
        key.write_text("key")

        with pytest.raises(ValueError, match=r"key passphrase.*must be a string"):
            _resolve_client_cert("srv", {
                "client_cert": [str(cert), str(key), 42],
            })


class TestBuildHttpxSSLContext:
    def test_default_https_keeps_certificate_verification_enabled(self):
        from tools.mcp_tool import _build_httpx_ssl_context

        context = _build_httpx_ssl_context("srv", True, None)

        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True

    def test_false_preserves_disabled_verification(self):
        from tools.mcp_tool import _build_httpx_ssl_context

        context = _build_httpx_ssl_context("srv", False, None)

        assert context.verify_mode == ssl.CERT_NONE
        assert context.check_hostname is False

    def test_accepts_existing_ssl_context(self):
        from tools.mcp_tool import _build_httpx_ssl_context

        existing = ssl.create_default_context()
        assert _build_httpx_ssl_context("srv", existing, None) is existing

    def test_accepts_custom_ca_directory(self, tmp_path):
        from tools.mcp_tool import _build_httpx_ssl_context

        context = _build_httpx_ssl_context("srv", str(tmp_path), None)

        assert context.verify_mode == ssl.CERT_REQUIRED

    def test_loads_combined_client_pem(self, mtls_material, tmp_path):
        from tools.mcp_tool import _build_httpx_ssl_context

        combined = tmp_path / "combined.pem"
        combined.write_bytes(
            mtls_material["client_cert"].read_bytes()
            + mtls_material["client_key"].read_bytes()
        )

        context = _build_httpx_ssl_context(
            "srv", str(mtls_material["ca"]), str(combined)
        )

        assert context.verify_mode == ssl.CERT_REQUIRED

    def test_invalid_client_key_error_is_server_scoped_and_sanitized(self, tmp_path):
        from tools.mcp_tool import _build_httpx_ssl_context

        cert = tmp_path / "client.pem"
        key = tmp_path / "secret-client.key"
        cert.write_text("not a certificate")
        key.write_text("SUPER-SECRET-KEY-CONTENT")

        with pytest.raises(ValueError) as exc_info:
            _build_httpx_ssl_context("private-server", True, (str(cert), str(key)))

        message = str(exc_info.value)
        assert "private-server" in message
        assert "SUPER-SECRET" not in message

    def test_custom_ca_and_encrypted_client_key_complete_real_mtls_handshake(
        self, mtls_material,
    ):
        from tools.mcp_tool import MCPServerTask, _resolve_client_cert

        task = MCPServerTask.__new__(MCPServerTask)
        task.name = "real-mtls"
        client_cert = _resolve_client_cert("real-mtls", {
            "client_cert": [
                str(mtls_material["client_cert"]),
                str(mtls_material["encrypted_client_key"]),
                "test-passphrase",
            ],
        })

        with _serve_mtls(mtls_material) as (url, seen_requests):
            asyncio.run(task._preflight_content_type(
                url,
                ssl_verify=str(mtls_material["ca"]),
                client_cert=client_cert,
                timeout=5,
            ))
        assert seen_requests == ["HEAD"]


# ---------------------------------------------------------------------------
# HTTP transport — cert forwarded into httpx.AsyncClient
# ---------------------------------------------------------------------------


class TestHTTPClientCert:
    def test_tls_context_forwarded_to_async_client(self, mtls_material):
        """The new-SDK HTTP path passes one normalized TLS context."""
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("remote")
        captured: dict = {}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class DummyTransportCtx:
            async def __aenter__(self):
                return MagicMock(), MagicMock(), (lambda: None)

            async def __aexit__(self, *a):
                return False

        class DummySession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def initialize(self):
                return None

        async def _discover_tools(self):
            self._shutdown_event.set()

        async def _drive():
            with patch("tools.mcp_tool._MCP_HTTP_AVAILABLE", True), \
                 patch("tools.mcp_tool._MCP_NEW_HTTP", True), \
                 _patch_sdk_async_client(DummyAsyncClient), \
                 patch("tools.mcp_tool.streamable_http_client",
                       return_value=DummyTransportCtx()), \
                 patch("tools.mcp_tool.ClientSession", DummySession), \
                 patch.object(MCPServerTask, "_discover_tools", _discover_tools):
                await server._run_http({
                    "url": "https://example.com/mcp",
                    "ssl_verify": str(mtls_material["ca"]),
                    "client_cert": str(mtls_material["client_cert"]),
                    "client_key": str(mtls_material["client_key"]),
                })

        asyncio.run(_drive())
        assert isinstance(captured["verify"], ssl.SSLContext)
        assert "cert" not in captured


    def test_missing_cert_file_surfaces_clear_error(self, tmp_path):
        """A missing cert file fails fast with a server-scoped error message."""
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("remote")

        async def _drive():
            with patch("tools.mcp_tool._MCP_HTTP_AVAILABLE", True), \
                 patch("tools.mcp_tool._MCP_NEW_HTTP", True):
                await server._run_http({
                    "url": "https://example.com/mcp",
                    "client_cert": str(tmp_path / "nope.pem"),
                })

        with pytest.raises(FileNotFoundError, match=r"remote.*client_cert.*not found"):
            asyncio.run(_drive())


class TestLegacyHTTPClientCert:
    def test_legacy_transport_receives_tls_context_factory(self, mtls_material):
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("legacy-remote")
        captured: dict = {}

        class DummyTransportCtx:
            async def __aenter__(self):
                return MagicMock(), MagicMock(), (lambda: None)

            async def __aexit__(self, *args):
                return False

        def fake_transport(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return DummyTransportCtx()

        class DummySession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def initialize(self):
                return None

        async def discover_and_stop(self):
            self._shutdown_event.set()

        async def drive():
            with patch("tools.mcp_tool._MCP_HTTP_AVAILABLE", True), \
                 patch("tools.mcp_tool._MCP_NEW_HTTP", False), \
                 patch("tools.mcp_tool.streamablehttp_client", fake_transport, create=True), \
                 patch("tools.mcp_tool.ClientSession", DummySession), \
                 patch.object(MCPServerTask, "_discover_tools", discover_and_stop):
                await server._run_http({
                    "url": "https://example.com/mcp",
                    "headers": {"X-Test": "preserved"},
                    "connect_timeout": 17,
                    "ssl_verify": str(mtls_material["ca"]),
                    "client_cert": str(mtls_material["client_cert"]),
                    "client_key": str(mtls_material["client_key"]),
                })

        asyncio.run(drive())
        assert captured["headers"]["X-Test"] == "preserved"
        assert captured["timeout"] == 17.0
        assert "verify" not in captured
        assert "cert" not in captured

        client_kwargs = {}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                client_kwargs.update(kwargs)

        from tools.mcp_tool import sdk_httpx

        with _patch_sdk_async_client(DummyAsyncClient):
            captured["httpx_client_factory"](
                headers={"X-Factory": "preserved"},
                timeout=sdk_httpx().Timeout(17),
                auth=None,
            )

        assert isinstance(client_kwargs["verify"], ssl.SSLContext)
        assert "cert" not in client_kwargs
        assert client_kwargs["headers"] == {"X-Factory": "preserved"}


# ---------------------------------------------------------------------------
# SSE transport — cert + verify routed via httpx_client_factory
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_sse_client():
    """Replace ``sse_client`` with a MagicMock that records its kwargs.

    Returns the captured kwargs dict so tests can assert how ``_run_http``
    called it.
    """
    captured_kwargs: dict = {}

    class _FakeStream:
        def __init__(self):
            self._read = AsyncMock()
            self._write = AsyncMock()

        async def __aenter__(self):
            return (self._read, self._write)

        async def __aexit__(self, *a):
            return False

    def fake_sse_client(**kwargs):
        captured_kwargs.clear()
        captured_kwargs.update(kwargs)
        return _FakeStream()

    class _FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            mock_session = MagicMock()
            mock_session.initialize = AsyncMock()
            return mock_session

        async def __aexit__(self, *a):
            return False

    with patch("tools.mcp_tool.sse_client", new=fake_sse_client), \
         patch("tools.mcp_tool.ClientSession", new=_FakeSession):
        yield captured_kwargs


class TestSSEClientCert:
    def test_default_https_uses_normalized_context_factory(self, patch_sse_client):
        """Default SSE HTTPS also flows through the centralized TLS context."""
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("sse-test")
        server._auth_type = ""
        server._sampling = None

        async def drive():
            with patch.object(MCPServerTask, "_wait_for_lifecycle_event",
                              new=AsyncMock(return_value="shutdown")), \
                 patch.object(MCPServerTask, "_discover_tools", new=AsyncMock()):
                await asyncio.wait_for(
                    server._run_http({
                        "url": "https://example.com/mcp/sse",
                        "transport": "sse",
                    }),
                    timeout=2.0,
                )

        asyncio.run(drive())
        assert "httpx_client_factory" in patch_sse_client

    def test_factory_injected_when_cert_set(self, patch_sse_client, mtls_material):
        """With client_cert set, an httpx_client_factory is injected that
        applies the cert (and follow_redirects=True to match the SDK)."""
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("sse-test")
        server._auth_type = ""
        server._sampling = None

        async def drive():
            with patch.object(MCPServerTask, "_wait_for_lifecycle_event",
                              new=AsyncMock(return_value="shutdown")), \
                 patch.object(MCPServerTask, "_discover_tools", new=AsyncMock()):
                await asyncio.wait_for(
                    server._run_http({
                        "url": "https://example.com/mcp/sse",
                        "transport": "sse",
                        "client_cert": str(mtls_material["client_cert"]),
                        "client_key": str(mtls_material["client_key"]),
                    }),
                    timeout=2.0,
                )

        asyncio.run(drive())

        factory = patch_sse_client.get("httpx_client_factory")
        assert factory is not None, "expected httpx_client_factory to be injected"

        # Invoke the factory the way the SDK would; capture the resulting
        # httpx.AsyncClient kwargs.
        captured_client_kwargs: dict = {}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                captured_client_kwargs.update(kwargs)

        from tools.mcp_tool import sdk_httpx
        with _patch_sdk_async_client(DummyAsyncClient):
            factory(headers={"x": "y"}, timeout=sdk_httpx().Timeout(30.0), auth=None)

        assert isinstance(captured_client_kwargs["verify"], ssl.SSLContext)
        assert "cert" not in captured_client_kwargs
        assert captured_client_kwargs["follow_redirects"] is True
        assert captured_client_kwargs["headers"] == {"x": "y"}

    def test_factory_forwards_custom_ca_context(self, patch_sse_client, mtls_material):
        """A custom CA path is normalized before reaching the SSE client."""
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("sse-test")
        server._auth_type = ""
        server._sampling = None

        async def drive():
            with patch.object(MCPServerTask, "_wait_for_lifecycle_event",
                              new=AsyncMock(return_value="shutdown")), \
                 patch.object(MCPServerTask, "_discover_tools", new=AsyncMock()):
                await asyncio.wait_for(
                    server._run_http({
                        "url": "https://example.com/mcp/sse",
                        "transport": "sse",
                        "ssl_verify": str(mtls_material["ca"]),
                    }),
                    timeout=2.0,
                )

        asyncio.run(drive())

        factory = patch_sse_client.get("httpx_client_factory")
        assert factory is not None

        captured_client_kwargs: dict = {}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                captured_client_kwargs.update(kwargs)

        with _patch_sdk_async_client(DummyAsyncClient):
            factory(headers=None, timeout=None, auth=None)

        assert isinstance(captured_client_kwargs["verify"], ssl.SSLContext)
        assert "cert" not in captured_client_kwargs
