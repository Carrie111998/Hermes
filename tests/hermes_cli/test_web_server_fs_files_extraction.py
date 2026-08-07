"""Regression tests for the wave-1 extraction of web_server.py clusters c9+c10.

Covers the pure helpers and seams moved verbatim out of ``hermes_cli.web_server``
into ``hermes_cli.web_routers.files`` (managed-files cluster c9) and
``hermes_cli.web_routers.fs`` (filesystem cluster c10), plus the legacy
re-export identity and the late-binding seam used to keep
``monkeypatch.setattr(web_server, ...)`` authoritative.
"""

from pathlib import Path

import pytest

from hermes_cli import web_server
from hermes_cli.web_routers import files as web_files
from hermes_cli.web_routers import fs as web_fs

pytest.importorskip("starlette.testclient")


# --- c9: managed-files helpers -------------------------------------------


def test_is_sensitive_filename_guard():
    assert web_files._is_sensitive_filename(".env") is True
    assert web_files._is_sensitive_filename(".env.local") is True
    assert web_files._is_sensitive_filename(".envrc") is True
    assert web_files._is_sensitive_filename("auth.json") is True
    assert web_files._is_sensitive_filename("config.yaml") is True
    assert web_files._is_sensitive_filename("mcp-tokens") is False  # dir check is _is_sensitive_path's job
    assert web_files._is_sensitive_filename("notes.txt") is False


def test_is_sensitive_path_blocks_credential_dirs():
    assert web_files._is_sensitive_path(Path("root/mcp-tokens/github.json")) is True
    assert web_files._is_sensitive_path(Path("root/pairing/device-abc")) is True
    assert web_files._is_sensitive_path(Path("root/.env")) is True
    assert web_files._is_sensitive_path(Path("root/docs/notes.md")) is False


def test_decode_data_url_roundtrip_and_rejections():
    data, mime = web_files._decode_data_url("data:text/plain;base64,aGVsbG8=")
    assert data == b"hello"
    assert mime == "text/plain"

    with pytest.raises(Exception):
        web_files._decode_data_url("not-a-data-url")
    with pytest.raises(Exception):
        web_files._decode_data_url("data:text/plain;base64,!!!not-base64!!!")
    with pytest.raises(Exception):
        web_files._decode_data_url("data:text/plain,no-base64-header")


def test_chat_image_extension_magic():
    assert web_files._chat_image_extension(b"\x89PNG\r\n\x1a\nrest") == ".png"
    assert web_files._chat_image_extension(b"RIFF\x00\x00\x00\x00WEBP") == ".webp"
    assert web_files._chat_image_extension(b"GIF87a....") == ".gif"
    assert web_files._chat_image_extension(b"plain text") is None


def test_sanitize_chat_image_filename():
    assert web_files._sanitize_chat_image_filename("../evil.txt") == "evil.txt"
    assert web_files._sanitize_chat_image_filename("a\x00b.png") == "a_b.png"
    assert web_files._sanitize_chat_image_filename("") == "pasted-image"


def test_managed_response_meta_shape():
    policy = web_files.ManagedFilesPolicy(
        default_path=Path("/tmp/root"), locked_root=None, can_change_path=True
    )
    assert web_files._managed_response_meta(policy) == {
        "root": None,
        "locked_root": None,
        "can_change_path": True,
    }
    opt_data = str(Path("/opt/data"))
    locked = web_files.ManagedFilesPolicy(
        default_path=Path("/opt/data"), locked_root=Path("/opt/data"), can_change_path=False
    )
    assert web_files._managed_response_meta(locked) == {
        "root": opt_data,
        "locked_root": opt_data,
        "can_change_path": False,
    }


# --- c10: fs helpers ------------------------------------------------------


def test_fs_mime_type_and_binary_detection():
    assert web_fs._fs_mime_type(Path("photo.png")) == "image/png"
    assert web_fs._fs_mime_type(Path("audio.mp3")) == "audio/mpeg"
    assert web_fs._fs_looks_binary(b"\x00\x01\x02") is True
    assert web_fs._fs_looks_binary(b"plain text") is False
    assert web_fs._fs_looks_binary(b"") is False


def test_fs_path_normalizes(tmp_path):
    target = tmp_path / "sub" / "file.txt"
    assert web_fs._fs_path(str(target)) == target.resolve(strict=False)
    assert web_fs._fs_path(f"file:{target}") == target.resolve(strict=False)
    with pytest.raises(Exception):
        web_fs._fs_path("")
    with pytest.raises(Exception):
        web_fs._fs_path("bad\x00path")


# --- seams: legacy re-exports + late binding ------------------------------


def test_legacy_re_exports_keep_web_server_namespace():
    # Tests and third-party code call these via web_server.<name>.
    assert web_server._is_sensitive_filename is web_files._is_sensitive_filename
    assert web_server._is_sensitive_path is web_files._is_sensitive_path
    assert web_server._decode_data_url is web_files._decode_data_url
    assert web_server._fs_mime_type is web_fs._fs_mime_type
    assert web_server.upload_managed_file_stream is web_files.upload_managed_file_stream
    assert web_server.fs_list is web_fs.fs_list
    assert web_server.ManagedFilesPolicy is web_files.ManagedFilesPolicy
    # Shared helpers that stayed in web_server are still there.
    assert callable(web_server._fs_path)
    assert callable(web_server._path_is_under)


def test_late_state_keeps_monkeypatch_authoritative(monkeypatch):
    # fs_read_data_url compares against _FS_DATA_URL_MAX_BYTES; the test seam
    # monkeypatches the web_server attribute and expects the moved handler to
    # see it through the LateState proxy.
    monkeypatch.setattr(web_server, "_FS_DATA_URL_MAX_BYTES", 3)
    assert web_fs._FS_DATA_URL_MAX_BYTES < 5
    assert web_fs._FS_DATA_URL_MAX_BYTES > 2


def test_fs_endpoints_require_auth():
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    tmp = Path.cwd() / "tests" / "hermes_cli"
    for url in (
        "/api/fs/list",
        "/api/fs/read-text",
        "/api/fs/default-cwd",
        "/api/files",
        "/api/files/read",
        "/api/chat/image-upload",
    ):
        params = {"path": str(tmp)} if url in ("/api/fs/list", "/api/fs/read-text", "/api/files", "/api/files/read") else None
        resp = client.get(url, params=params)
        assert resp.status_code == 401, f"{url} should require auth, got {resp.status_code}"
