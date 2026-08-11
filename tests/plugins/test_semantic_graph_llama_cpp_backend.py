"""Unit contracts for the opt-in llama.cpp embedding adapter."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import pytest

from plugins.semantic_graph.config import load_config
from plugins.semantic_graph.embedding import EmbeddingBackendError


def _backend_class():
    from plugins.semantic_graph.embedding import LlamaCppEmbeddingBackend

    return LlamaCppEmbeddingBackend


def _backend(endpoint: str, **overrides: object):
    options: dict[str, object] = {
        "endpoint": endpoint,
        "model": "test-model",
        "revision": "test-revision",
        "dimensions": 3,
        "serializer_version": 1,
        "timeout_seconds": 0.5,
    }
    options.update(overrides)
    return _backend_class()(**options)


def _embedding_response(
    vectors: list[list[float]],
    *,
    indices: list[int] | None = None,
    model: str = "test-model",
) -> bytes:
    return json.dumps(
        {
            "object": "list",
            "model": model,
            "data": [
                {"object": "embedding", "index": index, "embedding": vector}
                for index, vector in zip(
                    indices or list(range(len(vectors))), vectors, strict=True
                )
            ],
        },
        allow_nan=True,
    ).encode("utf-8")


@contextmanager
def _embedding_server(
    responses: list[tuple[int, bytes]],
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    received: list[dict[str, Any]] = []
    queued = list(responses)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            received.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "payload": json.loads(raw.decode("utf-8")),
                }
            )
            status, body = queued.pop(0) if queued else (500, b"missing test response")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}", received
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_llama_cpp_backend_constructor_performs_no_network_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_class = _backend_class()
    import plugins.semantic_graph.embedding.llama_cpp as llama_cpp

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("constructor performed network I/O")

    monkeypatch.setattr(llama_cpp.urllib.request, "urlopen", fail_if_called)

    backend = backend_class(
        endpoint="http://127.0.0.1:8082",
        model="test-model",
        revision="test-revision",
        dimensions=3,
        serializer_version=1,
        timeout_seconds=0.5,
    )

    assert backend.available() is True
    assert backend.identity.namespace == "llama.cpp:test-model:test-revision:d3:s1"


def test_llama_cpp_backend_rejects_remote_endpoint_by_default() -> None:
    with pytest.raises(EmbeddingBackendError, match="loopback"):
        _backend("https://example.com")


def test_llama_cpp_backend_posts_openai_v1_embeddings_payload() -> None:
    with _embedding_server([(200, _embedding_response([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]))]) as (
        endpoint,
        received,
    ):
        backend = _backend(endpoint)
        assert backend.embed_documents(["first", "second"]) == [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ]

    assert len(received) == 1
    request = received[0]
    assert request["path"] == "/v1/embeddings"
    assert request["payload"] == {
        "model": "test-model",
        "input": ["first", "second"],
        "encoding_format": "float",
    }
    assert str(request["headers"]["Content-Type"]).startswith("application/json")


def test_llama_cpp_backend_restores_batch_order_by_index() -> None:
    with _embedding_server(
        [(200, _embedding_response([[0.0, 1.0, 0.0], [1.0, 0.0, 0.0]], indices=[1, 0]))]
    ) as (endpoint, _received):
        backend = _backend(endpoint)
        assert backend.embed_documents(["first", "second"]) == [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ]


def test_llama_cpp_backend_rejects_missing_batch_entry() -> None:
    with _embedding_server([(200, _embedding_response([[1.0, 0.0, 0.0]]))]) as (
        endpoint,
        _received,
    ):
        with pytest.raises(EmbeddingBackendError, match="count"):
            _backend(endpoint).embed_documents(["first", "second"])


def test_llama_cpp_backend_rejects_dimension_mismatch() -> None:
    with _embedding_server([(200, _embedding_response([[1.0, 0.0]]))]) as (
        endpoint,
        _received,
    ):
        with pytest.raises(EmbeddingBackendError, match="dimension"):
            _backend(endpoint).embed_query("first")


def test_llama_cpp_backend_rejects_non_finite_values() -> None:
    with _embedding_server([(200, _embedding_response([[float("nan"), 0.0, 1.0]]))]) as (
        endpoint,
        _received,
    ):
        with pytest.raises(EmbeddingBackendError, match="NaN or infinity"):
            _backend(endpoint).embed_query("first")


def test_llama_cpp_backend_rejects_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_class = _backend_class()
    monkeypatch.setattr(backend_class, "_MAX_RESPONSE_BYTES", 8)
    with _embedding_server([(200, b'{"data": ["more than eight bytes"]}')]) as (
        endpoint,
        _received,
    ):
        with pytest.raises(EmbeddingBackendError, match="too large"):
            _backend(endpoint).embed_query("first")


def test_llama_cpp_backend_rejects_reported_model_alias_mismatch() -> None:
    with _embedding_server(
        [(200, _embedding_response([[1.0, 0.0, 0.0]], model="different-model"))]
    ) as (endpoint, _received):
        with pytest.raises(EmbeddingBackendError, match="model alias"):
            _backend(endpoint).embed_query("first")


def test_llama_cpp_backend_redacts_input_from_errors() -> None:
    secret_input = "do-not-include-this-input-in-an-error"
    with _embedding_server([(500, secret_input.encode("utf-8"))]) as (endpoint, _received):
        with pytest.raises(EmbeddingBackendError) as exc_info:
            _backend(endpoint).embed_query(secret_input)

    assert secret_input not in str(exc_info.value)


def test_llama_cpp_backend_returns_defensive_vector_copies() -> None:
    response = _embedding_response([[1.0, 0.0, 0.0]])
    with _embedding_server([(200, response), (200, response)]) as (endpoint, _received):
        backend = _backend(endpoint)
        first = backend.embed_query("first")
        first[0] = 99.0
        assert backend.embed_query("first") == [1.0, 0.0, 0.0]


def test_embedding_config_defaults_disabled_and_loopback_only() -> None:
    config = load_config({"embedding": {}})

    assert config.embedding.enabled is False
    assert config.embedding.backend == "llama_cpp"
    assert urlparse(config.embedding.endpoint).hostname == "127.0.0.1"
    assert config.embedding.model == "nsfw-bge-m3-v5-q6_k"
    assert config.embedding.revision == ""
    assert config.embedding.dimensions == 1024
    assert config.embedding.serializer_version == 1
    assert config.embedding.timeout_seconds == 5.0
    assert config.embedding.allow_remote is False


@pytest.mark.parametrize("dimensions", [0, -1, "not-an-integer"])
def test_embedding_config_rejects_invalid_dimensions(dimensions: object) -> None:
    with pytest.raises(ValueError, match="embedding.dimensions"):
        load_config({"embedding": {"dimensions": dimensions}})


@pytest.mark.parametrize("timeout_seconds", [0, -1.0, "not-a-number"])
def test_embedding_config_rejects_nonpositive_timeout(timeout_seconds: object) -> None:
    with pytest.raises(ValueError, match="embedding.timeout_seconds"):
        load_config({"embedding": {"timeout_seconds": timeout_seconds}})
