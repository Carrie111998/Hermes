"""Real MCP SDK/HTTPX witnesses for the public OAuth transport selectors.

The server below is a deterministic local protocol peer.  The load-bearing rows
invoke ``MCPServerTask._run_http`` and the installed MCP factories directly;
transport factories, ``httpx.AsyncClient``, ``ClientSession``, and the SDK auth
generator are not replaced.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import ssl
import socket
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


pytest.importorskip("mcp.client.auth.oauth2")


def _certificate_bundle(tmp_path):
    """Create a local CA, server cert, and client cert for real TLS tests."""
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mcp-test-ca")])
    ca_cert = (
        x509
        .CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    def signed_leaf(common_name, usages):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        cert = (
            x509
            .CertificateBuilder()
            .subject_name(name)
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]),
                critical=False,
            )
            .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        return key, cert

    server_key, server_cert = signed_leaf(
        "localhost", [ExtendedKeyUsageOID.SERVER_AUTH]
    )
    client_key, client_cert = signed_leaf(
        "mcp-test-client", [ExtendedKeyUsageOID.CLIENT_AUTH]
    )
    paths = {}
    for name, value, is_key in (
        ("ca.pem", ca_cert, False),
        ("server.pem", server_cert, False),
        ("server-key.pem", server_key, True),
        ("client.pem", client_cert, False),
        ("client-key.pem", client_key, True),
    ):
        path = tmp_path / name
        if is_key:
            data = value.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        else:
            data = value.public_bytes(serialization.Encoding.PEM)
        path.write_bytes(data)
        paths[name] = path
    return paths


class LocalHTTPSRecorder:
    def __init__(self, paths):
        self.paths = paths
        self.requests = []
        self.errors = []
        self._server = None

    async def __aenter__(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.paths["server.pem"], self.paths["server-key.pem"])
        # The TLS handshake remains CA-validated; application-level rejection
        # below gives the missing-client-cert row a deterministic HTTP result.
        context.verify_mode = ssl.CERT_OPTIONAL
        context.load_verify_locations(self.paths["ca.pem"])
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, ssl=context
        )
        return self

    async def __aexit__(self, *_args):
        self._server.close()
        await self._server.wait_closed()

    @property
    def base_url(self):
        return f"https://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    async def _handle(self, reader, writer):
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {
                key.lower(): value.strip()
                for key, value in (
                    line.split(":", 1) for line in lines[1:] if ":" in line
                )
            }
            length = int(headers.get("content-length", "0"))
            body = await reader.readexactly(length) if length else b""
            self.requests.append((method, target, headers, body))
            peer_cert = writer.get_extra_info("ssl_object").getpeercert(
                binary_form=True
            )
            if target == "/redirect":
                writer.write(
                    b"HTTP/1.1 302 Found\r\nLocation: /redirected-mcp\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                )
                await writer.drain()
                return
            if not peer_cert:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            payload = {"ok": True, "path": target}
            response = (
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(json.dumps(payload))}\r\nConnection: close\r\n\r\n".encode()
                + json.dumps(payload).encode()
            )
            writer.write(response)
            await writer.drain()
        except BaseException as exc:
            self.errors.append(repr(exc))
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ssl.SSLError:
                pass


class LocalMCPPeer:
    def __init__(self, *, sse: bool = False, mode: str = "success") -> None:
        self.sse = sse
        self.mode = mode
        self.requests: list[tuple[str, str, dict[str, str], dict | None]] = []
        self.handshake_seen = asyncio.Event()
        self.connection_seen = asyncio.Event()
        self.ready = asyncio.Event()
        self.connection_closed = asyncio.Event()
        self._open_connections = 0
        self._handshake_gate = asyncio.Event()
        self._writers: set[asyncio.StreamWriter] = set()
        self._sse_queue: asyncio.Queue[str] = asyncio.Queue()
        self._sse_writer = None
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> "LocalMCPPeer":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_args) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()
        for writer in tuple(self._writers):
            writer.close()
        if self._writers:
            await asyncio.gather(
                *(writer.wait_closed() for writer in tuple(self._writers)),
                return_exceptions=True,
            )
        if self._open_connections:
            await asyncio.sleep(0)

    async def close_connections(self) -> None:
        """Close retained peer sockets to make cancellation observation bounded."""
        for writer in tuple(self._writers):
            writer.close()
        await asyncio.sleep(0)

    @property
    def url(self) -> str:
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return (
            f"http://127.0.0.1:{port}/sse"
            if self.sse
            else f"http://127.0.0.1:{port}/mcp"
        )

    @property
    def base_url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._open_connections += 1
        self._writers.add(writer)
        self.connection_seen.set()
        try:
            while not reader.at_eof():
                try:
                    raw_headers = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                lines = raw_headers.decode("latin-1").split("\r\n")
                method, target, _version = lines[0].split(" ", 2)
                headers = {
                    key.lower(): value.strip()
                    for key, value in (
                        line.split(":", 1) for line in lines[1:] if ":" in line
                    )
                }
                length = int(headers.get("content-length", "0"))
                body = await reader.readexactly(length) if length else b""
                payload = json.loads(body) if body else None
                self.requests.append((method, target, headers, payload))

                if method == "GET" and "oauth-protected-resource" in target:
                    await self._write(
                        writer,
                        200,
                        json.dumps({
                            "resource": self.url,
                            "authorization_servers": [self.base_url],
                        }).encode(),
                        content_type="application/json",
                    )
                    continue
                if method == "GET" and "oauth-authorization-server" in target:
                    await self._write(
                        writer,
                        200,
                        json.dumps({
                            "issuer": self.base_url,
                            "authorization_endpoint": self.base_url + "/authorize",
                            "token_endpoint": self.base_url + "/token",
                            "registration_endpoint": self.base_url + "/register",
                            "code_challenge_methods_supported": ["S256"],
                        }).encode(),
                        content_type="application/json",
                    )
                    continue

                if method == "GET" and self.sse and target.startswith("/sse"):
                    self._sse_writer = writer
                    await self._write(
                        writer,
                        200,
                        b"event: endpoint\ndata: /messages?sessionId=local\n\n",
                        content_type="text/event-stream",
                        extra={"cache-control": "no-cache"},
                    )
                    while not writer.is_closing():
                        disconnect_task = asyncio.create_task(reader.read())
                        message_task = asyncio.create_task(self._sse_queue.get())
                        done, pending = await asyncio.wait(
                            {disconnect_task, message_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                        if disconnect_task in done:
                            return
                        message = message_task.result()
                        await writer.drain()
                        event = (
                            b"event: message\n" + b"data: " + message.encode() + b"\n\n"
                        )
                        writer.write(f"{len(event):X}\r\n".encode() + event + b"\r\n")
                    return

                if method == "GET":
                    await self._write(
                        writer,
                        200,
                        b"",
                        content_type="text/event-stream",
                        extra={"cache-control": "no-cache"},
                    )
                    await writer.drain()
                    await reader.read()
                    return

                if method == "DELETE":
                    await self._write(writer, 204, b"")
                    continue

                if method != "POST":
                    await self._write(writer, 404, b"")
                    continue

                if payload and payload.get("method") == "initialize":
                    self.handshake_seen.set()
                    if self.mode == "handshake_stall":
                        await self._handshake_gate.wait()
                    if self.mode == "handshake_failure":
                        await self._write(writer, 503, b"handshake failed")
                        return

                response = self._rpc_response(payload)
                if response is None:
                    await self._write(writer, 202, b"")
                elif self.sse:
                    await self._write(writer, 202, b"")
                    await self._sse_queue.put(
                        json.dumps(response, separators=(",", ":"))
                    )
                    if payload.get("method") == "tools/list":
                        self.ready.set()
                    if (
                        self.mode == "stream_drop"
                        and payload.get("method") == "initialize"
                    ):
                        writer.close()
                        await writer.wait_closed()
                        return
                else:
                    await self._write(
                        writer,
                        200,
                        json.dumps(response, separators=(",", ":")).encode(),
                        content_type="application/json",
                        extra={"mcp-session-id": "local"},
                    )
                    if (
                        self.mode == "stream_drop"
                        and payload.get("method") == "initialize"
                    ):
                        writer.close()
                        await writer.wait_closed()
                        return
                    if payload.get("method") == "tools/list":
                        self.ready.set()
        finally:
            if self._sse_writer is writer:
                self._sse_writer = None
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self._open_connections -= 1
            self._writers.discard(writer)
            if self._open_connections == 0:
                self.connection_closed.set()

    @staticmethod
    def _rpc_response(payload: dict | None) -> dict | None:
        if not payload or "id" not in payload:
            return None
        method = payload.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "local-peer", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": []}
        else:
            result = {}
        return {"jsonrpc": "2.0", "id": payload["id"], "result": result}

    @staticmethod
    async def _write(
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        *,
        content_type: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> None:
        reason = {200: "OK", 202: "Accepted", 204: "No Content", 404: "Not Found"}[
            status
        ]
        streaming = content_type == "text/event-stream"
        headers = {"HTTP/1.1": "", "Connection": "keep-alive"}
        if streaming:
            headers["Transfer-Encoding"] = "chunked"
        else:
            headers["Content-Length"] = str(len(body))
        if content_type:
            headers["Content-Type"] = content_type
        headers.update(extra or {})
        head = f"HTTP/1.1 {status} {reason}\r\n" + "".join(
            f"{key}: {value}\r\n" for key, value in headers.items() if key != "HTTP/1.1"
        )
        payload = body
        if streaming and body:
            payload = f"{len(body):X}\r\n".encode() + body + b"\r\n"
        writer.write(head.encode() + b"\r\n" + payload)
        await writer.drain()


async def _seed_oauth(tmp_path, monkeypatch, name: str) -> None:
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
    from pydantic import AnyUrl

    from tools.mcp_oauth import HermesTokenStorage

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    storage = HermesTokenStorage(name)
    await storage.set_tokens(
        OAuthToken(
            access_token="real-access",
            token_type="Bearer",
            expires_in=3600,
            refresh_token="real-refresh",
        )
    )
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="real-client",
            redirect_uris=[AnyUrl("http://127.0.0.1:1/callback")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
    )


async def _run_public_selector(
    peer: LocalMCPPeer,
    transport: str,
    auth_type: str,
    monkeypatch,
    tmp_path,
    *,
    timeout: float = 2,
    lifecycle_event: asyncio.Event | None = None,
) -> None:
    import tools.mcp_tool as tool_module
    from tools.mcp_oauth_manager import reset_manager_for_tests

    reset_manager_for_tests()
    if auth_type == "oauth":
        await _seed_oauth(tmp_path, monkeypatch, "real-peer")
    monkeypatch.setattr(tool_module, "_MCP_NEW_HTTP", transport == "current")

    # Lifecycle is the task-level owner boundary; transport/session entry,
    # initialize, tools/list, and all context exits remain real.
    async def stop(_self):
        if lifecycle_event is not None:
            await lifecycle_event.wait()
        return "shutdown"

    monkeypatch.setattr(tool_module.MCPServerTask, "_wait_for_lifecycle_event", stop)
    task = tool_module.MCPServerTask("real-peer")
    task._auth_type = auth_type
    config = {
        "url": peer.url,
        "transport": "sse" if peer.sse else "streamable_http",
        "connect_timeout": timeout,
        "ssl_verify": False,
        "headers": {"x-witness": "real"},
        "oauth": {},
    }
    await task._run_http(config)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["current", "legacy", "sse"])
@pytest.mark.parametrize("auth_type", ["none", "oauth"])
async def test_public_real_sdk_transport_lifecycle(
    transport, auth_type, monkeypatch, tmp_path
):
    """All selectors use their installed SDK clients and real wire responses."""
    peer = LocalMCPPeer(sse=transport == "sse")
    async with peer:
        await _run_public_selector(
            peer,
            transport,
            auth_type,
            monkeypatch,
            tmp_path,
        )
    assert any(method == "POST" for method, *_ in peer.requests)
    if auth_type == "oauth":
        assert any(
            "bearer real-access" == headers.get("authorization", "").lower()
            for _, _, headers, _ in peer.requests
        )
    else:
        assert not any("authorization" in headers for _, _, headers, _ in peer.requests)
    if transport in {"current", "legacy"}:
        assert any(method == "DELETE" for method, *_ in peer.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["current", "legacy", "sse"])
@pytest.mark.parametrize("auth_type", ["none", "oauth"])
async def test_real_handshake_timeout_failure_and_reuse(
    transport, auth_type, monkeypatch, tmp_path
):
    """A real stalled/failed handshake closes and permits a fresh run."""
    for mode in ("handshake_stall", "handshake_failure"):
        peer = LocalMCPPeer(sse=transport == "sse", mode=mode)
        async with peer:
            with pytest.raises(BaseException):
                await _run_public_selector(
                    peer,
                    transport,
                    auth_type,
                    monkeypatch,
                    tmp_path,
                    timeout=0.15,
                )
            peer._handshake_gate.set()
            peer.mode = "success"
            peer.connection_closed = asyncio.Event()
            peer.ready = asyncio.Event()
            peer.handshake_seen = asyncio.Event()
            peer.connection_seen = asyncio.Event()
            await _run_public_selector(
                peer, transport, auth_type, monkeypatch, tmp_path, timeout=2
            )
        assert peer._open_connections == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["current", "legacy", "sse"])
@pytest.mark.parametrize("auth_type", ["none", "oauth"])
async def test_real_stream_drop_failure_and_reuse(
    transport, auth_type, monkeypatch, tmp_path
):
    """A peer-side stream drop is observed through each installed selector."""
    peer = LocalMCPPeer(sse=transport == "sse", mode="stream_drop")
    async with peer:
        try:
            await _run_public_selector(
                peer, transport, auth_type, monkeypatch, tmp_path, timeout=1
            )
        except BaseException:
            pass
        peer.mode = "success"
        peer.connection_closed = asyncio.Event()
        peer.ready = asyncio.Event()
        peer.handshake_seen = asyncio.Event()
        await _run_public_selector(
            peer, transport, auth_type, monkeypatch, tmp_path, timeout=2
        )
    assert peer._open_connections == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["current", "legacy", "sse"])
@pytest.mark.parametrize("auth_type", ["none", "oauth"])
async def test_real_cancellation_during_handshake_and_live_session(
    transport, auth_type, monkeypatch, tmp_path
):
    """Cancellation at both barriers closes real transport/session resources."""
    peer = LocalMCPPeer(sse=transport == "sse", mode="handshake_stall")
    async with peer:
        task = asyncio.create_task(
            _run_public_selector(
                peer, transport, auth_type, monkeypatch, tmp_path, timeout=2
            )
        )
        await asyncio.wait_for(peer.connection_seen.wait(), timeout=5)
        task.cancel()
        peer._handshake_gate.set()
        await peer.close_connections()
        with pytest.raises(BaseException):
            await asyncio.wait_for(task, timeout=5)

        peer.mode = "success"
        peer.connection_closed = asyncio.Event()
        peer.ready = asyncio.Event()
        peer.handshake_seen = asyncio.Event()
        peer.connection_seen = asyncio.Event()
        live_cancel = asyncio.Event()
        live_task = asyncio.create_task(
            _run_public_selector(
                peer,
                transport,
                auth_type,
                monkeypatch,
                tmp_path,
                timeout=2,
                lifecycle_event=live_cancel,
            )
        )
        await peer.ready.wait()
        live_task.cancel()
        await peer.close_connections()
        with pytest.raises(BaseException):
            await asyncio.wait_for(live_task, timeout=5)

        live_cancel.set()
        peer.connection_closed = asyncio.Event()
        peer.ready = asyncio.Event()
        peer.handshake_seen = asyncio.Event()
        await _run_public_selector(
            peer, transport, auth_type, monkeypatch, tmp_path, timeout=2
        )
    assert peer._open_connections == 0


@pytest.mark.asyncio
async def test_real_https_custom_ca_mtls_control_and_data_wire(tmp_path):
    """Real HTTPX policy reaches control/data requests and rejects bad TLS."""
    import httpx

    paths = _certificate_bundle(tmp_path)
    client_bundle = tmp_path / "client-bundle.pem"
    client_bundle.write_bytes(
        paths["client.pem"].read_bytes() + paths["client-key.pem"].read_bytes()
    )
    client_context = ssl.create_default_context(cafile=str(paths["ca.pem"]))
    client_context.load_cert_chain(str(client_bundle))
    async with LocalHTTPSRecorder(paths) as peer:
        timeout = httpx.Timeout(1.0, connect=0.5, read=1.0)
        response_hooks = []

        async def capture_response(response):
            response_hooks.append(response)

        async def add_wire_header(request):
            request.headers["x-hermes-wire-hook"] = "configured"

        async with httpx.AsyncClient(
            verify=client_context,
            timeout=timeout,
            follow_redirects=True,
            event_hooks={"request": [add_wire_header], "response": [capture_response]},
        ) as client:
            control_paths = (
                "/.well-known/oauth-protected-resource/mcp",
                "/.well-known/oauth-authorization-server",
                "/register",
                "/client-metadata.json",
                "/token",
                "/refresh",
            )
            for path in control_paths:
                try:
                    response = await client.get(peer.base_url + path)
                except httpx.HTTPError as exc:
                    raise AssertionError(peer.errors) from exc
                assert response.status_code == 200
            response = await client.get(
                peer.base_url + "/mcp", headers={"authorization": "Bearer resource"}
            )
            assert response.status_code == 200
            redirect_response = await client.get(peer.base_url + "/redirect")
            assert redirect_response.status_code == 200

        assert len(peer.requests) == len(control_paths) + 3
        for _method, target, headers, _body in peer.requests:
            assert headers["x-hermes-wire-hook"] == "configured"
            if target == "/mcp":
                assert headers["authorization"] == "Bearer resource"
            else:
                assert "authorization" not in headers
        assert any(response.status_code == 302 for response in response_hooks)

        bad_ca = tmp_path / "wrong-ca.pem"
        bad_ca.write_bytes(paths["client.pem"].read_bytes())
        with pytest.raises(httpx.HTTPError):
            async with httpx.AsyncClient(
                verify=str(bad_ca),
                cert=str(client_bundle),
                timeout=timeout,
            ) as client:
                await client.get(peer.base_url + "/control")

        with pytest.raises(httpx.HTTPError):
            async with httpx.AsyncClient(
                verify=str(paths["ca.pem"]), timeout=timeout
            ) as client:
                response = await client.get(peer.base_url + "/control")
                response.raise_for_status()

    with pytest.raises(httpx.HTTPError):
        async with httpx.AsyncClient(timeout=httpx.Timeout(0.2, connect=0.1)) as client:
            await client.get("http://127.0.0.1:1/timeout")


@pytest.mark.asyncio
async def test_real_httpx_control_plane_and_rotating_refresh(tmp_path, monkeypatch):
    """The installed SDK driver captures discovery, DCR, token, and refresh wire rows."""
    import httpx
    from mcp.shared.auth import (
        OAuthClientInformationFull,
        OAuthClientMetadata,
        OAuthMetadata,
        OAuthToken,
    )
    from pydantic import AnyUrl

    from tools.mcp_oauth import HermesTokenStorage
    from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS, reset_manager_for_tests

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reset_manager_for_tests()
    requests: list[httpx.Request] = []
    token_calls = 0
    callback_state: str | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        requests.append(request)
        path = request.url.path
        if path == "/mcp":
            if any(r.url.path == "/token" for r in requests):
                return httpx.Response(200, request=request, json={"ok": True})
            return httpx.Response(
                401,
                request=request,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://resource.test/.well-known/oauth-protected-resource/mcp"'
                },
            )
        if "oauth-protected-resource" in path:
            return httpx.Response(
                200,
                request=request,
                json={
                    "resource": "https://resource.test/mcp",
                    "authorization_servers": ["https://resource.test"],
                },
            )
        if "oauth-authorization-server" in path:
            return httpx.Response(
                200,
                request=request,
                json={
                    "issuer": "https://resource.test",
                    "authorization_endpoint": "https://resource.test/authorize",
                    "token_endpoint": "https://resource.test/token",
                    "registration_endpoint": "https://resource.test/register",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if path == "/register":
            return httpx.Response(
                201,
                request=request,
                json={
                    "client_id": "registered-client",
                    "redirect_uris": ["http://127.0.0.1:1/callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
            )
        if path == "/token":
            token_calls += 1
            return httpx.Response(
                200,
                request=request,
                json={
                    "access_token": "rotated-access",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "rotated-refresh",
                },
            )
        return httpx.Response(404, request=request)

    storage = HermesTokenStorage("control")

    async def redirect_handler(url: str) -> None:
        nonlocal callback_state
        callback_state = parse_qs(urlsplit(url).query)["state"][0]

    async def callback_handler() -> tuple[str, str]:
        assert callback_state is not None
        return "authorization-code", callback_state

    provider = _HERMES_PROVIDER_CLS(
        server_name="control",
        server_url="https://resource.test/mcp",
        client_metadata=OAuthClientMetadata(
            redirect_uris=[AnyUrl("http://127.0.0.1:1/callback")],
            client_name="Hermes Agent",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    provider._hermes_transport_options = {
        "connect_timeout": 3,
        "ssl_verify": False,
        "client_cert": ("client.pem", "client.key"),
        "headers": {"x-control": "yes"},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), auth=provider
    ) as client:
        response = await client.get("https://resource.test/mcp")
    assert response.status_code == 200
    assert token_calls == 1
    control_paths = {"/register", "/token"}
    control_paths.update(
        request.url.path for request in requests if "well-known" in request.url.path
    )
    assert control_paths.issubset({request.url.path for request in requests})
    assert all(
        "authorization" not in request.headers
        for request in requests
        if request.url.path != "/mcp"
    )
    assert any(
        request.url.path == "/mcp"
        and request.headers.get("authorization") == "Bearer rotated-access"
        for request in requests
    )

    cimd_storage = HermesTokenStorage("cimd")
    cimd_requests: list[httpx.Request] = []
    cimd_mcp_calls = 0

    async def cimd_handler(request: httpx.Request) -> httpx.Response:
        nonlocal cimd_mcp_calls
        cimd_requests.append(request)
        if request.url.path == "/mcp":
            cimd_mcp_calls += 1
            if cimd_mcp_calls == 1:
                return httpx.Response(
                    401,
                    request=request,
                    headers={
                        "WWW-Authenticate": 'Bearer resource_metadata="https://resource.test/.well-known/oauth-protected-resource/mcp"'
                    },
                )
            return httpx.Response(200, request=request, json={"ok": True})
        if "oauth-protected-resource" in request.url.path:
            return httpx.Response(
                200,
                request=request,
                json={
                    "resource": "https://resource.test/mcp",
                    "authorization_servers": ["https://resource.test"],
                },
            )
        if "oauth-authorization-server" in request.url.path:
            return httpx.Response(
                200,
                request=request,
                json={
                    "issuer": "https://resource.test",
                    "authorization_endpoint": "https://resource.test/authorize",
                    "token_endpoint": "https://resource.test/token",
                    "client_id_metadata_document_supported": True,
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if request.url.path == "/token":
            return httpx.Response(
                200,
                request=request,
                json={
                    "access_token": "cimd-access",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "cimd-refresh",
                },
            )
        return httpx.Response(404, request=request)

    cimd_provider = _HERMES_PROVIDER_CLS(
        server_name="cimd",
        server_url="https://resource.test/mcp",
        client_metadata=OAuthClientMetadata(
            redirect_uris=[AnyUrl("http://127.0.0.1:1/callback")],
            client_name="Hermes Agent",
        ),
        storage=cimd_storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        client_metadata_url="https://resource.test/cimd.json",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(cimd_handler), auth=cimd_provider
    ) as client:
        cimd_response = await client.get("https://resource.test/mcp")
    assert cimd_response.status_code == 200
    assert any(request.url.path == "/token" for request in cimd_requests)
    assert not any(request.url.path == "/register" for request in cimd_requests)
    assert all(
        "authorization" not in request.headers
        for request in cimd_requests
        if request.url.path != "/mcp"
    )

    # The same public driver must perform one insufficient-scope step-up and
    # retry, rather than treating 403 as an ordinary data-plane failure.
    step_requests: list[httpx.Request] = []
    step_token_calls = 0
    step_mcp_calls = 0

    async def step_handler(request: httpx.Request) -> httpx.Response:
        nonlocal step_token_calls, step_mcp_calls
        step_requests.append(request)
        if request.url.path == "/mcp":
            step_mcp_calls += 1
            if step_mcp_calls == 1:
                return httpx.Response(
                    403,
                    request=request,
                    headers={
                        "WWW-Authenticate": (
                            'Bearer error="insufficient_scope", scope="elevated"'
                        )
                    },
                )
            return httpx.Response(200, request=request, json={"ok": True})
        if request.url.path == "/token":
            step_token_calls += 1
            return httpx.Response(
                200,
                request=request,
                json={
                    "access_token": "stepup-access",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "stepup-refresh",
                },
            )
        return httpx.Response(404, request=request)

    stepup_provider = _HERMES_PROVIDER_CLS(
        server_name="control-stepup",
        server_url="https://resource.test/mcp",
        client_metadata=OAuthClientMetadata(
            redirect_uris=[AnyUrl("http://127.0.0.1:1/callback")],
            client_name="Hermes Agent",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(step_handler), auth=stepup_provider
    ) as client:
        step_response = await client.get("https://resource.test/mcp")
    assert step_response.status_code == 200
    assert step_token_calls == 1
    assert all(
        "authorization" not in request.headers
        for request in step_requests
        if request.url.path == "/token"
    )
    assert sum(request.url.path == "/mcp" for request in step_requests) == 2

    # A second public driver run starts with an expired persisted pair and
    # proves refresh rotation is durable before the data-plane retry.
    refresh_storage = HermesTokenStorage("refresh")
    await refresh_storage.set_tokens(
        OAuthToken(
            access_token="expired-access",
            token_type="Bearer",
            expires_in=0,
            refresh_token="old-refresh",
        )
    )
    await refresh_storage.set_client_info(
        OAuthClientInformationFull(
            client_id="refresh-client",
            redirect_uris=[AnyUrl("http://127.0.0.1:1/callback")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
    )
    refresh_storage.save_oauth_metadata(
        OAuthMetadata.model_validate({
            "issuer": "https://resource.test",
            "authorization_endpoint": "https://resource.test/authorize",
            "token_endpoint": "https://resource.test/token",
        })
    )
    refresh_provider = _HERMES_PROVIDER_CLS(
        server_name="refresh",
        server_url="https://resource.test/mcp",
        client_metadata=OAuthClientMetadata(
            redirect_uris=[AnyUrl("http://127.0.0.1:1/callback")],
            client_name="Hermes Agent",
        ),
        storage=refresh_storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    refresh_requests: list[httpx.Request] = []

    async def refresh_handler(request: httpx.Request) -> httpx.Response:
        refresh_requests.append(request)
        if request.url.path == "/token":
            return httpx.Response(
                200,
                request=request,
                json={
                    "access_token": "fresh-access",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "fresh-refresh",
                },
            )
        return httpx.Response(200, request=request, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(refresh_handler), auth=refresh_provider
    ) as client:
        assert (await client.get("https://resource.test/mcp")).status_code == 200
    saved = await refresh_storage.get_tokens()
    assert saved is not None
    assert saved.access_token == "fresh-access"
    assert saved.refresh_token == "fresh-refresh"
    assert all(
        "authorization" not in request.headers
        for request in refresh_requests
        if request.url.path == "/token"
    )


@pytest.mark.asyncio
async def test_callback_timeout_joins_listener_and_reuses_port(monkeypatch):
    """A cancelled/expired waiter leaves no named worker or reservation behind."""
    import tools.mcp_oauth as oauth_module

    monkeypatch.setattr(oauth_module, "_is_interactive", lambda: True)
    monkeypatch.setattr(
        oauth_module, "_raise_if_non_interactive", lambda _message: None
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    for _ in range(2):
        waiter = oauth_module._make_callback_waiter(port, timeout=0)
        with pytest.raises(oauth_module.OAuthNonInteractiveError):
            await waiter()
        assert port not in oauth_module._reserved_sockets
        assert not any(
            thread.name == "mcp-oauth-callback-listener"
            for thread in threading.enumerate()
        )


def test_unsupported_paste_handle_is_disabled_without_blocking_read(monkeypatch):
    """An interactive-looking unsupported handle must not spawn blocking I/O."""
    import tools.mcp_oauth as oauth_module

    called = False

    class UnsupportedStdin:
        def fileno(self):
            raise OSError("no native descriptor")

        def readline(self):
            nonlocal called
            called = True
            raise AssertionError("unsupported handles must not call readline")

    monkeypatch.setattr(oauth_module.sys, "stdin", UnsupportedStdin())
    stop = threading.Event()
    oauth_module._paste_callback_reader({}, stop)
    assert called is False
