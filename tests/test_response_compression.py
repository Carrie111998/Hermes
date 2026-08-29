"""Tests for selective HTTP response compression."""

import pytest
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from hermes_cli.response_compression import SelectiveGZipMiddleware


def _large_payload(request):
    return JSONResponse({"payload": "compressible-value " * 2_000})


def _small_payload(request):
    return JSONResponse({"ok": True})


def _html_payload(request):
    return HTMLResponse("<html>" + ("safe-content " * 2_000) + "</html>")


async def _stream_payload(request):
    async def chunks():
        yield b'{"payload":"'
        yield (b"compressible-value " * 2_000)
        yield b'"}'

    return StreamingResponse(chunks(), media_type="application/json")


def _sensitive_payload(request):
    return JSONResponse({"secret": "sensitive-value " * 2_000})


@pytest.fixture
def client():
    app = Starlette(
        routes=[
            Route("/large", _large_payload),
            Route("/small", _small_payload),
            Route("/html", _html_payload),
            Route("/stream", _stream_payload),
            Route("/api/env/reveal", _sensitive_payload),
            Route("/api/config/raw", _sensitive_payload),
            Route("/auth/native/token", _sensitive_payload),
        ]
    )
    app.add_middleware(SelectiveGZipMiddleware, minimum_size=1024)
    with TestClient(app) as test_client:
        yield test_client


def test_compresses_large_json_and_preserves_vary(client):
    response = client.get("/large", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in response.headers["vary"]
    assert int(response.headers["content-length"]) < len(response.content)
    assert response.json()["payload"].startswith("compressible-value")


def test_does_not_compress_small_or_html_responses(client):
    small = client.get("/small", headers={"Accept-Encoding": "gzip"})
    html = client.get("/html", headers={"Accept-Encoding": "gzip"})

    assert "content-encoding" not in small.headers
    assert "content-encoding" not in html.headers


def test_respects_explicit_gzip_quality_zero(client):
    response = client.get("/large", headers={"Accept-Encoding": "gzip; q=0"})

    assert "content-encoding" not in response.headers


def test_compresses_streaming_json_without_content_length(client):
    response = client.get("/stream", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert "content-length" not in response.headers
    assert response.json()["payload"].startswith("compressible-value")


def test_does_not_compress_sensitive_dashboard_auth_json(client):
    reveal = client.get("/api/env/reveal", headers={"Accept-Encoding": "gzip"})
    raw_config = client.get("/api/config/raw", headers={"Accept-Encoding": "gzip"})
    native_token = client.get("/auth/native/token", headers={"Accept-Encoding": "gzip"})

    assert "content-encoding" not in reveal.headers
    assert "content-encoding" not in raw_config.headers
    assert "content-encoding" not in native_token.headers
