"""Wire-level tests for credential-safe stdlib urllib redirects."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import urllib.error
import urllib.request

import pytest

from hermes_cli.urllib_security import (
    SafeCredentialRedirectHandler,
    URLResponseBodyTooLarge,
    open_credentialed_url,
    read_credentialed_url_bytes_limited,
    url_origin,
)


class _Response:
    def __init__(
        self,
        payload: bytes = b"{}",
        *,
        content_length: int | str | None = None,
    ) -> None:
        self._payload = payload
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.read_sizes: list[int] = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True
        return False

    def close(self):
        self.closed = True

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size is None or size < 0:
            result = self._payload
            self._payload = b""
            return result
        result = self._payload[:size]
        self._payload = self._payload[size:]
        return result


class _BoundedRedirectHandler(BaseHTTPRequestHandler):
    redirect_body = b""
    redirect_content_length = True
    target_requests = 0

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/target")
            if type(self).redirect_content_length:
                self.send_header(
                    "Content-Length",
                    str(len(type(self).redirect_body)),
                )
            self.end_headers()
            try:
                self.wfile.write(type(self).redirect_body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        type(self).target_requests += 1
        body = b'{"data":[]}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


def _bounded_redirect_server(
    body: bytes,
    *,
    content_length: bool = True,
):
    _BoundedRedirectHandler.redirect_body = body
    _BoundedRedirectHandler.redirect_content_length = content_length
    _BoundedRedirectHandler.target_requests = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BoundedRedirectHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


class _RecordingHandler(BaseHTTPRequestHandler):
    redirect_to = ""
    redirect_status = 302
    requests: list[tuple[str, dict[str, str]]] = []

    def _record(self) -> None:
        type(self).requests.append(
            (self.command, {name.lower(): value for name, value in self.headers.items()})
        )

    def do_GET(self):
        if self.path.startswith("/redirect"):
            self.send_response(type(self).redirect_status)
            self.send_header("Location", type(self).redirect_to)
            self.end_headers()
            return
        self._record()
        body = json.dumps({"data": []}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/redirect":
            self.send_response(type(self).redirect_status)
            self.send_header("Location", type(self).redirect_to)
            self.end_headers()
            return
        self._record()
        self.send_response(200)
        self.end_headers()

    def log_message(self, _format, *_args):
        pass


def _server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def _credential_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer secret",
        "Cookie": "session=secret",
        "CF-Access-Client-Secret": "cloudflare-secret",
        "X-Custom-Auth": "tenant-secret",
        "Accept": "application/json",
        "User-Agent": "hermes-test",
    }




def test_cross_host_redirect_drops_arbitrary_credentials_on_wire():
    source = _server()
    sink = _server()
    _RecordingHandler.requests = []
    _RecordingHandler.redirect_status = 302
    _RecordingHandler.redirect_to = f"http://localhost:{sink.server_port}/sink"
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{source.server_port}/redirect",
            headers=_credential_headers(),
        )
        with open_credentialed_url(request, timeout=3) as response:
            response.read()
    finally:
        source.shutdown()
        sink.shutdown()

    method, headers = _RecordingHandler.requests[-1]
    assert method == "GET"
    assert headers["accept"] == "application/json"
    assert headers["user-agent"] == "hermes-test"
    for name in (
        "authorization",
        "cookie",
        "cf-access-client-secret",
        "x-custom-auth",
    ):
        assert name not in headers


def test_same_host_different_port_drops_credentials_on_wire():
    source = _server()
    sink = _server()
    _RecordingHandler.requests = []
    _RecordingHandler.redirect_status = 302
    _RecordingHandler.redirect_to = f"http://127.0.0.1:{sink.server_port}/sink"
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{source.server_port}/redirect",
            headers=_credential_headers(),
        )
        with open_credentialed_url(request, timeout=3) as response:
            response.read()
    finally:
        source.shutdown()
        sink.shutdown()

    _, headers = _RecordingHandler.requests[-1]
    assert "authorization" not in headers
    assert "cf-access-client-secret" not in headers






def test_post_307_remains_rejected_by_urllib():
    request = urllib.request.Request(
        "https://models.example.test/load",
        data=b"{}",
        headers=_credential_headers(),
        method="POST",
    )
    handler = SafeCredentialRedirectHandler(request.full_url)
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(
            request,
            None,
            307,
            "Temporary Redirect",
            {},
            "https://other.example.test/load",
        )


def test_explicit_opener_factory_is_instrumentable_without_security_bypass():
    calls = []

    class _Opener:
        def open(self, request, *, timeout):
            calls.append((request.full_url, timeout))
            return _Response()

    def factory(*handlers):
        assert any(isinstance(h, SafeCredentialRedirectHandler) for h in handlers)
        return _Opener()

    request = urllib.request.Request(
        "https://models.example.test/models", headers={"Authorization": "secret"}
    )
    with open_credentialed_url(request, timeout=7, opener_factory=factory):
        pass
    assert calls == [("https://models.example.test/models", 7)]


def test_limited_read_rejects_declared_oversize_before_body_read():
    response = _Response(b"unread", content_length=9)

    class _Opener:
        def open(self, _request, *, timeout):
            assert timeout == 7
            return response

    request = urllib.request.Request("https://models.example.test/models")
    with pytest.raises(URLResponseBodyTooLarge, match="exceeds 8 bytes"):
        read_credentialed_url_bytes_limited(
            request,
            timeout=7,
            max_bytes=8,
            opener_factory=lambda *_handlers: _Opener(),
        )

    assert response.read_sizes == []
    assert response.closed is True


def test_limited_read_keeps_only_the_exact_final_response_budget():
    response = _Response(b"12345678")

    class _Opener:
        def open(self, _request, *, timeout):
            assert timeout == 7
            return response

    request = urllib.request.Request("https://models.example.test/models")
    assert read_credentialed_url_bytes_limited(
        request,
        timeout=7,
        max_bytes=8,
        opener_factory=lambda *_handlers: _Opener(),
    ) == b"12345678"
    assert response.read_sizes == [9, 1]
    assert response.closed is True


@pytest.mark.parametrize("content_length", [True, False])
def test_limited_read_follows_redirect_without_unbounded_drain(content_length):
    server = _bounded_redirect_server(
        b"x" * 65,
        content_length=content_length,
    )
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/redirect"
        )
        body = read_credentialed_url_bytes_limited(
            request,
            timeout=3,
            max_bytes=64,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert body == b'{"data":[]}'
    assert _BoundedRedirectHandler.target_requests == 1


def test_limited_read_preserves_normal_urllib_redirect_following():
    server = _bounded_redirect_server(b"go")
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/redirect"
        )
        body = read_credentialed_url_bytes_limited(
            request,
            timeout=3,
            max_bytes=64,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert body == b'{"data":[]}'
    assert _BoundedRedirectHandler.target_requests == 1


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_every_get_redirect_status_clamps_the_discarded_body(status):
    response = _Response(b"x" * 65)
    final_response = _Response(b'{"data":[]}')
    opened: list[tuple[str, str]] = []
    handler = SafeCredentialRedirectHandler(
        "https://models.example.test/models",
        max_redirect_response_bytes=64,
    )

    class _Parent:
        def open(self, request, *, timeout):
            opened.append((request.get_method(), request.full_url))
            assert timeout == 7
            return final_response

    handler.parent = _Parent()
    request = urllib.request.Request("https://models.example.test/models")
    request.timeout = 7

    result = getattr(handler, f"http_error_{status}")(
        request,
        response,
        status,
        "redirect",
        {"location": "/target"},
    )

    assert result is final_response
    assert response.read_sizes == [64]
    assert response.closed is True
    assert opened == [("GET", "https://models.example.test/target")]


@pytest.mark.parametrize("status", [301, 302, 303])
def test_post_redirect_rewrite_is_unchanged_when_drain_is_bounded(status):
    response = _Response(b"go")
    final_response = _Response()
    redirected = []
    handler = SafeCredentialRedirectHandler(
        "https://models.example.test/load",
        max_redirect_response_bytes=64,
    )

    class _Parent:
        def open(self, request, *, timeout):
            redirected.append(request)
            assert timeout == 7
            return final_response

    handler.parent = _Parent()
    request = urllib.request.Request(
        "https://models.example.test/load",
        data=b"{}",
        method="POST",
    )
    request.timeout = 7

    result = getattr(handler, f"http_error_{status}")(
        request,
        response,
        status,
        "redirect",
        {"location": "/target"},
    )

    assert result is final_response
    assert response.read_sizes == [64]
    assert redirected[0].get_method() == "GET"
    assert redirected[0].data is None


@pytest.mark.parametrize("status", [307, 308])
def test_rejected_post_redirect_body_remains_readable(status):
    response = _Response(b"rejection body")
    handler = SafeCredentialRedirectHandler(
        "https://models.example.test/load",
        max_redirect_response_bytes=4,
    )
    request = urllib.request.Request(
        "https://models.example.test/load",
        data=b"{}",
        method="POST",
    )
    request.timeout = 7

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        getattr(handler, f"http_error_{status}")(
            request,
            response,
            status,
            "redirect",
            {"location": "/target"},
        )

    assert exc_info.value.read() == b"rejection body"
    assert response.read_sizes == [-1]






def test_installed_request_processor_cannot_resurrect_cross_origin_secret(
    monkeypatch,
):
    source = _server()
    sink = _server()
    _RecordingHandler.requests = []
    _RecordingHandler.redirect_status = 302
    _RecordingHandler.redirect_to = f"http://localhost:{sink.server_port}/sink"

    class SecretProcessor(urllib.request.BaseHandler):
        handler_order = float("inf")  # type: ignore[assignment]

        def http_request(self, request):
            request.add_header("X-Installed-Secret", "must-not-cross")
            return request

    installed = urllib.request.build_opener(SecretProcessor())
    installed.addheaders = [("X-Opener-Secret", "also-must-not-cross")]
    monkeypatch.setattr(urllib.request, "_opener", installed)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{source.server_port}/redirect",
            headers={"Authorization": "Bearer secret"},
        )
        with open_credentialed_url(request, timeout=3) as response:
            response.read()
    finally:
        source.shutdown()
        sink.shutdown()

    _, headers = _RecordingHandler.requests[-1]
    assert "authorization" not in headers
    assert "x-installed-secret" not in headers
    assert "x-opener-secret" not in headers


def test_multihop_redirects_never_resurrect_credentials():
    request = urllib.request.Request(
        "https://a.example.test/models", headers=_credential_headers()
    )
    handler = SafeCredentialRedirectHandler(request.full_url)

    same_origin = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://a.example.test/step-two",
    )
    assert same_origin is not None
    same_headers = {
        name.lower(): value for name, value in same_origin.header_items()
    }
    assert "authorization" in same_headers

    cross_origin = handler.redirect_request(
        same_origin,
        None,
        302,
        "Found",
        {},
        "https://b.example.test/step-three",
    )
    assert cross_origin is not None
    cross_headers = {
        name.lower(): value for name, value in cross_origin.header_items()
    }
    assert "authorization" not in cross_headers
    assert "cf-access-client-secret" not in cross_headers

    returned = handler.redirect_request(
        cross_origin,
        None,
        302,
        "Found",
        {},
        "https://a.example.test/final",
    )
    assert returned is not None
    returned_headers = {
        name.lower(): value for name, value in returned.header_items()
    }
    assert "authorization" not in returned_headers
    assert "cf-access-client-secret" not in returned_headers


def test_probe_api_models_drops_custom_credentials_on_wire():
    from hermes_cli.models import probe_api_models

    source = _server()
    sink = _server()
    _RecordingHandler.requests = []
    _RecordingHandler.redirect_status = 302
    _RecordingHandler.redirect_to = f"http://localhost:{sink.server_port}/sink"
    try:
        result = probe_api_models(
            "provider-key",
            f"http://127.0.0.1:{source.server_port}/redirect/..",
            timeout=3,
            request_headers={
                "CF-Access-Client-Secret": "cloudflare-secret",
                "X-Custom-Auth": "tenant-secret",
            },
        )
    finally:
        source.shutdown()
        sink.shutdown()

    assert result["models"] == []
    _, headers = _RecordingHandler.requests[-1]
    assert "authorization" not in headers
    assert "cf-access-client-secret" not in headers
    assert "x-custom-auth" not in headers


class _LmStudioSourceHandler(BaseHTTPRequestHandler):
    redirect_to = ""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(302)
        self.send_header("Location", type(self).redirect_to)
        self.end_headers()

    def log_message(self, format, *_args):
        pass


def test_anthropic_profile_drops_x_api_key_on_redirect(monkeypatch):
    import importlib

    AnthropicProfile = importlib.import_module(
        "plugins.model-providers.anthropic"
    ).AnthropicProfile

    source = _server()
    sink = _server()
    _RecordingHandler.requests = []
    _RecordingHandler.redirect_status = 302
    _RecordingHandler.redirect_to = f"http://localhost:{sink.server_port}/sink"

    original_request = urllib.request.Request

    def local_anthropic_request(url, *args, **kwargs):
        if url == "https://api.anthropic.com/v1/models":
            url = f"http://127.0.0.1:{source.server_port}/redirect"
        return original_request(url, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "Request", local_anthropic_request)
    try:
        result = AnthropicProfile(name="anthropic").fetch_models(
            api_key="anthropic-secret", timeout=3
        )
    finally:
        source.shutdown()
        sink.shutdown()

    assert result == []
    _, headers = _RecordingHandler.requests[-1]
    assert "x-api-key" not in headers
    assert headers["accept"] == "application/json"


def test_azure_catalog_probe_drops_api_key_and_bearer_on_redirect():
    from hermes_cli import azure_detect

    source = _server()
    sink = _server()
    _RecordingHandler.requests = []
    _RecordingHandler.redirect_status = 302
    _RecordingHandler.redirect_to = f"http://localhost:{sink.server_port}/sink"
    try:
        status, body = azure_detect._http_get_json(
            f"http://127.0.0.1:{source.server_port}/redirect", "azure-secret", timeout=3
        )
    finally:
        source.shutdown()
        sink.shutdown()

    assert status == 200
    assert body == {"data": []}
    _, headers = _RecordingHandler.requests[-1]
    assert "authorization" not in headers
    assert "api-key" not in headers


def test_azure_anthropic_probe_drops_api_key_and_bearer_on_redirect():
    from hermes_cli import azure_detect

    sink = _server()
    source = ThreadingHTTPServer(("127.0.0.1", 0), _LmStudioSourceHandler)
    Thread(target=source.serve_forever, daemon=True).start()
    _RecordingHandler.requests = []
    _LmStudioSourceHandler.redirect_to = f"http://localhost:{sink.server_port}/sink"
    try:
        azure_detect._probe_anthropic_messages(
            f"http://127.0.0.1:{source.server_port}", "azure-secret"
        )
    finally:
        source.shutdown()
        sink.shutdown()

    _, headers = _RecordingHandler.requests[-1]
    assert "authorization" not in headers
    assert "api-key" not in headers


