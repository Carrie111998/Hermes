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
def test_final_cache_name_is_published_only_after_stream_finishes(
    module_name,
    function_name,
    cache_dir_name,
    content_type,
    tmp_path,
    monkeypatch,
):
    """A final cache path must not be visible while bytes are still streaming."""
    module = __import__(module_name, fromlist=[function_name])
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(module, cache_dir_name, lambda: cache_dir)

    response = MagicMock()
    response.headers = {"Content-Type": content_type}
    response.raise_for_status.return_value = None
    visible_during_stream = []

    def complete_chunks(*, chunk_size):
        del chunk_size
        visible_during_stream.append(
            sorted(path.name for path in cache_dir.iterdir())
        )
        yield b"complete media payload"

    response.iter_content.side_effect = complete_chunks

    with patch("requests.get", return_value=response):
        result = getattr(module, function_name)(
            "https://cdn.example.test/media"
        )

    assert result.read_bytes() == b"complete media payload"
    assert len(visible_during_stream) == 1
    assert len(visible_during_stream[0]) == 1
    assert visible_during_stream[0][0].startswith(".")
    assert visible_during_stream[0][0].endswith(".part")
    assert [path.name for path in cache_dir.iterdir()] == [result.name]
