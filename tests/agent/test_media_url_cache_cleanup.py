"""Regression tests for failed streamed media URL materialization (#83119)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests


@pytest.mark.parametrize(
    ("module_name", "function_name", "cache_dir_name", "content_type"),
    (
        (
            "agent.image_gen_provider",
            "save_url_image",
            "_images_cache_dir",
            "image/png",
        ),
        (
            "agent.video_gen_provider",
            "save_url_video",
            "_videos_cache_dir",
            "video/mp4",
        ),
    ),
)
def test_truncated_stream_does_not_leave_partial_cache_file(
    module_name,
    function_name,
    cache_dir_name,
    content_type,
    tmp_path,
    monkeypatch,
):
    """Iterator failures must remove files that were never complete."""
    module = __import__(module_name, fromlist=[function_name])
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(module, cache_dir_name, lambda: cache_dir)

    response = MagicMock()
    response.headers = {"Content-Type": content_type}
    response.raise_for_status.return_value = None

    def truncated_chunks(*, chunk_size):
        del chunk_size
        yield b"partial media payload"
        raise requests.exceptions.ChunkedEncodingError(
            "connection closed before EOF"
        )

    response.iter_content.side_effect = truncated_chunks

    with (
        patch("requests.get", return_value=response),
        pytest.raises(requests.exceptions.ChunkedEncodingError),
    ):
        getattr(module, function_name)("https://cdn.example.test/media")

    assert list(cache_dir.iterdir()) == []
