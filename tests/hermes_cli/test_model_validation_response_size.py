"""Response-size boundaries for generic ``/models`` probes."""

from __future__ import annotations

from collections.abc import Iterable
from unittest.mock import patch

from hermes_cli import models
from hermes_cli.models import probe_api_models


class _Response:
    def __init__(self, body: bytes, *, content_length: int | None = None) -> None:
        self._body = body
        self._offset = 0
        self.headers = (
            {} if content_length is None else {"Content-Length": str(content_length)}
        )
        self.read_sizes: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            raise AssertionError("model catalog reads must always be bounded")
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


def _response_opener(responses: Iterable[_Response]):
    pending = iter(responses)
    opened_urls: list[str] = []

    def _open(request, *, timeout, **_kwargs):
        opened_urls.append(request.full_url)
        return next(pending)

    return _open, opened_urls


def _probe_with_responses(responses: list[_Response]):
    fake_open, opened_urls = _response_opener(responses)
    with patch(
        "hermes_cli.urllib_security.open_credentialed_url", side_effect=fake_open
    ):
        result = probe_api_models("key", "https://models.example.test")
    return result, opened_urls


def test_probe_accepts_catalog_at_exact_limit():
    prefix = b'{"data":[{"id":"exact-limit"}]}'
    body = prefix + b" " * (models._MODEL_CATALOG_MAX_BYTES - len(prefix))
    response = _Response(body, content_length=len(body))

    result, opened_urls = _probe_with_responses([response])

    assert result["models"] == ["exact-limit"]
    assert result["used_fallback"] is False
    assert opened_urls == ["https://models.example.test/models"]
    assert response.read_sizes
    assert max(response.read_sizes) <= 64 * 1024


def test_probe_rejects_undeclared_catalog_at_limit_plus_one():
    oversized = b"x" * (models._MODEL_CATALOG_MAX_BYTES + 1)
    responses = [_Response(oversized), _Response(oversized)]

    result, opened_urls = _probe_with_responses(responses)

    assert result["models"] is None
    assert opened_urls == [
        "https://models.example.test/models",
        "https://models.example.test/v1/models",
    ]
    assert all(response.read_sizes for response in responses)
    assert all(max(response.read_sizes) <= 64 * 1024 for response in responses)


def test_probe_rejects_declared_oversize_before_reading():
    responses = [
        _Response(b"", content_length=models._MODEL_CATALOG_MAX_BYTES + 1),
        _Response(b"", content_length=models._MODEL_CATALOG_MAX_BYTES + 1),
    ]

    result, opened_urls = _probe_with_responses(responses)

    assert result["models"] is None
    assert opened_urls == [
        "https://models.example.test/models",
        "https://models.example.test/v1/models",
    ]
    assert all(response.read_sizes == [] for response in responses)


def test_probe_tries_v1_fallback_after_oversized_first_candidate():
    oversized = _Response(b"x" * (models._MODEL_CATALOG_MAX_BYTES + 1))
    fallback = _Response(b'{"data":[{"id":"fallback-model"}]}')

    result, opened_urls = _probe_with_responses([oversized, fallback])

    assert result["models"] == ["fallback-model"]
    assert result["probed_url"] == "https://models.example.test/v1/models"
    assert result["resolved_base_url"] == "https://models.example.test/v1"
    assert result["used_fallback"] is True
    assert opened_urls == [
        "https://models.example.test/models",
        "https://models.example.test/v1/models",
    ]
