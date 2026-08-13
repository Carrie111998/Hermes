import json

import httpx
import pytest

from agent import model_metadata


class _ChunkStream(httpx.SyncByteStream):
    def __init__(self, chunks: int, chunk_size: int = 1024 * 1024):
        self.chunks = chunks
        self.chunk = b"x" * chunk_size
        self.pulled = 0
        self.closed = False

    def __iter__(self):
        for _ in range(self.chunks):
            self.pulled += 1
            yield self.chunk

    def close(self) -> None:
        self.closed = True


def _install_transport(monkeypatch, handler):
    real_client = httpx.Client
    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_factory)


def test_detect_local_server_type_stops_oversized_tags_stream(monkeypatch):
    oversized = _ChunkStream(chunks=32)

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, stream=oversized)
        return httpx.Response(404, json={})

    _install_transport(monkeypatch, handler)
    model_metadata._endpoint_probe_path_cache.clear()

    assert model_metadata.detect_local_server_type("http://127.0.0.1:11434") is None
    assert oversized.closed is True
    assert oversized.pulled < oversized.chunks


def test_ollama_show_stops_oversized_stream(monkeypatch):
    oversized = _ChunkStream(chunks=32)
    _install_transport(
        monkeypatch,
        lambda _request: httpx.Response(200, stream=oversized),
    )

    result = model_metadata._query_ollama_api_show_uncached(
        "model", "http://127.0.0.1:11434"
    )

    assert result is None
    assert oversized.closed is True
    assert oversized.pulled < oversized.chunks


def test_httpx_probe_rejects_encoded_response_before_body_read(monkeypatch):
    encoded = _ChunkStream(chunks=1)
    _install_transport(
        monkeypatch,
        lambda _request: httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=encoded
        ),
    )

    assert (
        model_metadata._query_ollama_api_show_uncached(
            "model", "http://127.0.0.1:11434"
        )
        is None
    )
    assert encoded.pulled == 0
    assert encoded.closed is True


class _RequestsStream:
    def __init__(self, chunks, *, headers=None):
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.status_code = 200
        self.ok = True
        self.closed = False
        self.pulled = 0
        self._content = False
        self._content_consumed = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=1, **_kwargs):
        assert chunk_size == 64 * 1024
        for chunk in self._chunks:
            self.pulled += 1
            yield chunk

    def json(self):
        return json.loads(self._content.decode())

    def close(self):
        self.closed = True


@pytest.mark.parametrize("path", ["generic", "lmstudio", "props"])
def test_requests_metadata_paths_stop_oversized_stream(monkeypatch, path):
    oversized = []

    def oversized_response():
        response = _RequestsStream(
            [b"x" * (1024 * 1024)] * 32,
            headers={"content-type": "application/json"},
        )
        oversized.append(response)
        return response

    model_metadata._endpoint_model_metadata_cache.clear()
    model_metadata._endpoint_model_metadata_cache_time.clear()
    monkeypatch.setattr(
        model_metadata,
        "detect_local_server_type",
        lambda *_args, **_kwargs: "lm-studio" if path == "lmstudio" else None,
    )

    def fake_get(url, **kwargs):
        assert kwargs["stream"] is True
        assert kwargs["headers"]["Accept-Encoding"] == "identity"
        if path == "props" and url.endswith("/models"):
            return _RequestsStream([b'{"data":[{"id":"model","owned_by":"llamacpp"}]}'])
        return oversized_response()

    monkeypatch.setattr(model_metadata.requests, "get", fake_get)

    result = model_metadata.fetch_endpoint_model_metadata(
        "http://127.0.0.1:11434/v1", force_refresh=True
    )

    assert result == ({"model": {"name": "model"}} if path == "props" else {})
    assert oversized
    assert all(response.closed for response in oversized)
    assert all(response.pulled < len(response._chunks) for response in oversized)
