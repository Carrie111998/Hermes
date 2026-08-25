"""Local Bot API (``--local``) media handling must never buffer whole files.

A self-hosted ``telegram-bot-api`` raises the upload cap from 50MB to 2000MB
and answers ``getFile`` with an absolute server-side path instead of a
download URL. Both directions therefore need to avoid materialising the
payload in memory, or a 2GB transfer OOMs a small host:

  * outbound — hand PTB the path STRING. ``parse_file_input`` only applies the
    ``file://`` optimization to ``str``/``Path``; a file HANDLE falls through
    to ``InputFile(handle)``, whose ``load_file()`` calls ``.read()``.
  * inbound — copy from the server-reported path with ``shutil.copyfile``
    instead of ``download_as_bytearray()``.

Cloud-mode behaviour (the default) must be completely unchanged.
"""

import os

import pytest

from gateway.platforms.base import (
    CachedMedia,
    cache_media_bytes,
    cache_media_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _adapter(local_mode: bool, base_url: str = "", **extra):
    """Build a bare TelegramAdapter without running __init__/connect.

    ``name`` is a read-only property on the adapter, so it is left alone;
    only the attributes the code under test reads are set.
    """
    from plugins.platforms.telegram.adapter import TelegramAdapter

    a = TelegramAdapter.__new__(TelegramAdapter)
    a._local_mode = local_mode
    return a


# ---------------------------------------------------------------------------
# Outbound: _media_upload_source
# ---------------------------------------------------------------------------

def test_local_mode_yields_path_string_not_handle(tmp_path):
    """Local mode must yield a str path so PTB uses the file:// URI."""
    f = tmp_path / "movie.mp4"
    f.write_bytes(b"\x00" * 1024)

    with _adapter(local_mode=True)._media_upload_source(str(f)) as (src, rewind):
        assert isinstance(src, str), "local mode must pass a path, not a handle"
        assert src == os.path.abspath(str(f))
        assert not hasattr(src, "read"), "a readable handle would be buffered by PTB"
        # A path is stateless, so there is nothing to rewind on retry.
        assert rewind is None


def test_cloud_mode_yields_open_handle(tmp_path):
    """Cloud mode keeps the historical handle-based upload."""
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"abc123")

    with _adapter(local_mode=False)._media_upload_source(str(f)) as (src, rewind):
        assert hasattr(src, "read"), "cloud mode should still pass a file object"
        assert src.read() == b"abc123"
        assert callable(rewind)
        rewind()
        assert src.read() == b"abc123", "rewind must reset the handle for retries"


def test_cloud_mode_closes_handle_on_exit(tmp_path):
    """The context manager must not leak file descriptors."""
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")

    with _adapter(local_mode=False)._media_upload_source(str(f)) as (src, _):
        assert not src.closed
    assert src.closed


def test_local_mode_does_not_open_the_file(tmp_path, monkeypatch):
    """The whole point: local mode must not read the file at all."""
    f = tmp_path / "huge.mp4"
    f.write_bytes(b"y" * 4096)

    real_open = open
    opened = []

    def _tracking_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _tracking_open)
    with _adapter(local_mode=True)._media_upload_source(str(f)) as (src, _):
        assert isinstance(src, str)
    assert str(f) not in opened, "local mode must never open the payload"


def test_missing_local_mode_attr_falls_back_to_cloud(tmp_path):
    """An adapter built via __new__ (no __init__) must not explode.

    Several call sites construct the adapter without running __init__, so
    ``_local_mode`` can be absent. Reading it unguarded raised AttributeError
    and sank the entire send into the text-only fallback.
    """
    from plugins.platforms.telegram.adapter import TelegramAdapter

    bare = TelegramAdapter.__new__(TelegramAdapter)
    assert not hasattr(bare, "_local_mode")

    f = tmp_path / "clip.mp4"
    f.write_bytes(b"q")
    with bare._media_upload_source(str(f)) as (src, rewind):
        assert hasattr(src, "read"), "must default to cloud-mode handle"
        assert callable(rewind)


def test_local_mode_relative_path_is_absolutised(tmp_path, monkeypatch):
    """PTB's file:// URI needs an absolute path; relative input must resolve."""
    f = tmp_path / "rel.mp4"
    f.write_bytes(b"z")
    monkeypatch.chdir(tmp_path)

    with _adapter(local_mode=True)._media_upload_source("rel.mp4") as (src, _):
        assert os.path.isabs(src)
        assert src == str(f)


# ---------------------------------------------------------------------------
# Inbound: cache_media_path
# ---------------------------------------------------------------------------

def test_cache_media_path_streams_video_without_reading_bytes(tmp_path, monkeypatch):
    """A video must be copied, never slurped via read_bytes()."""
    src = tmp_path / "in.mp4"
    src.write_bytes(b"v" * 2048)

    copied = {}

    def _fake_copyfile(a, b):
        copied["args"] = (str(a), str(b))
        with open(a, "rb") as fh_in, open(b, "wb") as fh_out:
            fh_out.write(fh_in.read())

    monkeypatch.setattr("gateway.platforms.base.shutil.copyfile", _fake_copyfile)

    cached = cache_media_path(str(src), filename="in.mp4", mime_type="video/mp4")

    assert cached is not None
    assert cached.kind == "video"
    assert copied["args"][0] == str(src), "must copy, not buffer"
    assert os.path.isfile(cached.local_path)
    assert open(cached.local_path, "rb").read() == b"v" * 2048


def test_cache_media_path_matches_bytes_path_classification(tmp_path):
    """Streamed and buffered paths must classify a file identically."""
    for name, mime, expect in [
        ("a.mp4", "video/mp4", "video"),
        ("a.ogg", "audio/ogg", "audio"),
        ("a.pdf", "application/pdf", "document"),
        ("a.bin", "", "document"),
    ]:
        src = tmp_path / f"src_{name}"
        src.write_bytes(b"data")

        streamed = cache_media_path(str(src), filename=name, mime_type=mime)
        buffered = cache_media_bytes(b"data", filename=name, mime_type=mime)

        assert streamed is not None and buffered is not None
        assert streamed.kind == buffered.kind == expect, name
        assert streamed.media_type == buffered.media_type, name


def test_cache_media_path_missing_file_returns_none(tmp_path):
    """A dangling server path must degrade gracefully, not raise."""
    assert cache_media_path(str(tmp_path / "nope.mp4"), filename="nope.mp4") is None


def test_cache_media_path_enforces_size_cap(tmp_path, monkeypatch):
    """The inbound cap applies to the streamed path too, via stat()."""
    src = tmp_path / "big.mp4"
    src.write_bytes(b"x" * 4096)

    monkeypatch.setattr(
        "gateway.platforms.base.get_inbound_media_max_bytes", lambda: 1024
    )
    with pytest.raises(ValueError, match="too large"):
        cache_media_path(str(src), filename="big.mp4", mime_type="video/mp4")


def test_cache_media_path_rejects_path_traversal(tmp_path):
    """A malicious filename must not escape the document cache."""
    src = tmp_path / "ok.bin"
    src.write_bytes(b"d")

    cached = cache_media_path(str(src), filename="../../etc/passwd")
    assert cached is not None
    # Sanitised to a basename inside the cache dir, never an escape.
    assert "etc" not in os.path.dirname(cached.local_path).split(os.sep)[-1]
    assert os.path.basename(cached.local_path).endswith("passwd")


def test_cached_media_local_path_defaults_to_path():
    """Back-compat: constructing without local_path mirrors path."""
    cm = CachedMedia("/x/y.mp4", "video/mp4", "video", "y.mp4")
    assert cm.local_path == "/x/y.mp4"


def test_cached_media_local_path_survives_sandbox_translation():
    """local_path must stay host-real even when path is container-visible."""
    cm = CachedMedia(
        "/root/.hermes/cache/videos/v.mp4",
        "video/mp4",
        "video",
        "v.mp4",
        local_path="/home/and/.hermes/cache/videos/v.mp4",
    )
    assert cm.path != cm.local_path
    assert cm.local_path.startswith("/home/and")
