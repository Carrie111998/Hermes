"""Wave-1 s4 extraction regression tests for tui_gateway/server.py.

Covers the pure helpers moved verbatim into ``tui_gateway/attachments_mixin.py``
(shard-plan s4 clusters c9 + c10) and rebound onto ``tui_gateway.server``'s
globals at import time — the same install seam the ``methods_*`` handler
modules use (see method_ctx.py). Functions are exercised through
``tui_gateway.server`` because that is the namespace the TUI and the existing
tests call.
"""

import base64

import pytest
from pathlib import Path

from tui_gateway import attachments_mixin, server

ATTACHMENT_FUNCTIONS = (
    "_decode_attach_base64",
    "_sniff_image_ext",
    "_allowed_image_extensions",
    "_session_images_dir",
    "_queue_attached_image",
    "_format_ref_value",
    "_attachment_ref_path",
    "_desktop_attachment_dir",
    "_sanitize_attachment_name",
    "_unique_attachment_path",
    "_resolve_gateway_attachment_path",
    "_decode_attachment_data_url",
    "_stage_session_file_attachment",
)


def test_rebind_contract_all_attachment_helpers_on_server():
    """Every moved helper is rebound onto server.py's namespace, sharing the
    mixin's code object but resolving globals against server (so
    ``_session_cwd`` / ``_hermes_home`` / module constants behave as before)."""
    for name in ATTACHMENT_FUNCTIONS:
        rebound = getattr(server, name)
        standalone = getattr(attachments_mixin, name)
        assert callable(rebound)
        assert rebound.__code__ is standalone.__code__
        assert rebound.__globals__ is vars(server)


def test_decode_attach_base64():
    raw = base64.b64encode(b"hello").decode("ascii")
    assert server._decode_attach_base64(raw, mime_prefix="image/") == b"hello"
    assert (
        server._decode_attach_base64(f"data:image/png;base64,{raw}", mime_prefix="image/")
        == b"hello"
    )
    # whitespace inside payload is tolerated
    assert (
        server._decode_attach_base64(raw[:4] + "\n" + raw[4:], mime_prefix="image/")
        == b"hello"
    )
    assert server._decode_attach_base64("@@@", mime_prefix="image/") is None


def test_sniff_image_ext_magic_and_filename():
    assert server._sniff_image_ext(b"\x89PNG\r\n\x1a\n") == ".png"
    assert server._sniff_image_ext(b"\xff\xd8\xff\xe0") == ".jpg"
    assert server._sniff_image_ext(b"GIF87a....") == ".gif"
    assert server._sniff_image_ext(b"GIF89a....") == ".gif"
    assert server._sniff_image_ext(b"RIFF1234WEBPxxxx") == ".webp"
    assert server._sniff_image_ext(b"BM......") == ".bmp"
    assert server._sniff_image_ext(b"unknown") == ".png"  # fallback
    # filename hint wins over magic bytes
    assert server._sniff_image_ext(b"\x89PNG", "photo.jpeg") == ".jpeg"


def test_allowed_image_extensions():
    exts = server._allowed_image_extensions()
    assert isinstance(exts, frozenset)
    assert ".png" in exts
    assert ".jpg" in exts


def test_session_images_dir_profile_home_anchor(tmp_path):
    profile_home = tmp_path / "profile"
    session = {"profile_home": str(profile_home)}
    assert server._session_images_dir(session) == profile_home / "images"


def test_session_images_dir_falls_back_to_server_hermes_home(monkeypatch, tmp_path):
    launch_home = tmp_path / "launch"
    monkeypatch.setattr(server, "_hermes_home", launch_home)
    assert server._session_images_dir({}) == launch_home / "images"


def test_queue_attached_image_writes_and_queues(tmp_path):
    session = {"profile_home": str(tmp_path / "profile")}
    img_path = server._queue_attached_image(session, b"\x89PNG\r\n", ".png", prefix="upload")
    assert img_path.exists()
    assert img_path.read_bytes() == b"\x89PNG\r\n"
    assert img_path.name.startswith("upload_")
    assert img_path.name.endswith(".png")
    assert session["image_counter"] == 1
    assert session["attached_images"] == [str(img_path)]


def test_queue_attached_image_rolls_back_counter_on_failure(monkeypatch, tmp_path):
    session = {"profile_home": str(tmp_path / "profile")}

    def boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", boom)
    with pytest.raises(OSError):
        server._queue_attached_image(session, b"x", ".png", prefix="upload")
    assert session["image_counter"] == 0
    assert "attached_images" not in session


def test_format_ref_value_quoting():
    assert server._format_ref_value("plain") == "plain"
    assert server._format_ref_value("with space") == "`with space`"
    assert server._format_ref_value("a`b") == '"a`b"'
    assert server._format_ref_value('a`b"c') == "'a`b\"c'"
    assert server._format_ref_value('a`b"c\'d') == 'a`b"c\'d'
    assert server._format_ref_value("") == ""


def test_sanitize_attachment_name():
    assert server._sanitize_attachment_name("") == "attachment"
    assert server._sanitize_attachment_name("  ") == "attachment"
    assert server._sanitize_attachment_name(None) == "attachment"
    assert server._sanitize_attachment_name("..hidden") == "hidden"
    assert server._sanitize_attachment_name("/tmp/evil.txt") == "evil.txt"
    assert server._sanitize_attachment_name("bad\x01\x02name") == "bad_name"


def test_unique_attachment_path_dedupes(tmp_path):
    a = tmp_path / "report.pdf"
    a.write_bytes(b"x")
    assert server._unique_attachment_path(tmp_path, "report.pdf") == tmp_path / "report-2.pdf"
    (tmp_path / "report-2.pdf").write_bytes(b"x")
    assert server._unique_attachment_path(tmp_path, "report.pdf") == tmp_path / "report-3.pdf"
    assert server._unique_attachment_path(tmp_path, "fresh.txt") == tmp_path / "fresh.txt"


def test_attachment_ref_path_workspace_relative(tmp_path):
    session = {"cwd": str(tmp_path)}
    inside = tmp_path / "sub" / "file.txt"
    inside.parent.mkdir()
    inside.write_text("x")
    assert server._attachment_ref_path(session, inside) == "sub/file.txt"


def test_attachment_ref_path_outside_absolute(tmp_path):
    session = {"cwd": str(tmp_path)}
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x")
    assert server._attachment_ref_path(session, outside) == str(outside.resolve())


def test_desktop_attachment_dir_creates(tmp_path):
    session = {"cwd": str(tmp_path)}
    d = server._desktop_attachment_dir(session)
    assert d == (tmp_path / ".hermes" / "desktop-attachments").resolve()
    assert d.is_dir()


def test_resolve_gateway_attachment_path_empty_and_missing(tmp_path):
    assert server._resolve_gateway_attachment_path("") is None
    assert server._resolve_gateway_attachment_path(str(tmp_path / "nope.txt")) is None


def test_decode_attachment_data_url():
    raw = base64.b64encode(b"hello, world").decode("ascii")
    assert server._decode_attachment_data_url(f"data:text/plain;base64,{raw}") == b"hello, world"
    assert server._decode_attachment_data_url(f"data:application/pdf;base64,{raw}") == b"hello, world"
    # bare base64 with no data-URL prefix is tolerated
    assert server._decode_attachment_data_url(raw) == b"hello, world"
    with pytest.raises(ValueError):
        server._decode_attachment_data_url("@@@")


def test_stage_session_file_attachment_inside_workspace(tmp_path):
    session = {"cwd": str(tmp_path)}
    inside = tmp_path / "doc.txt"
    inside.write_text("content")
    stored, uploaded = server._stage_session_file_attachment(
        session, raw_path=str(inside), data_url="", name="doc.txt"
    )
    assert uploaded is False
    assert stored.resolve() == inside.resolve()


def test_stage_session_file_attachment_copies_outside_file(tmp_path):
    session = {"cwd": str(tmp_path)}
    outside = tmp_path.parent / "external.bin"
    outside.write_bytes(b"\x00\x01")
    stored, uploaded = server._stage_session_file_attachment(
        session, raw_path=str(outside), data_url="", name="external.bin"
    )
    assert uploaded is True
    assert stored.resolve().is_file()
    assert stored.read_bytes() == b"\x00\x01"
    assert stored.resolve() != outside.resolve()
    assert "desktop-attachments" in stored.parts


def test_stage_session_file_attachment_writes_data_url(tmp_path):
    session = {"cwd": str(tmp_path)}
    raw = base64.b64encode(b"csv,data").decode("ascii")
    stored, uploaded = server._stage_session_file_attachment(
        session,
        raw_path=str(tmp_path / "does-not-exist.txt"),
        data_url=f"data:text/csv;base64,{raw}",
        name="upload.csv",
    )
    assert uploaded is True
    assert stored.read_bytes() == b"csv,data"
    assert stored.name == "upload.csv"


def test_stage_session_file_attachment_missing_raises(tmp_path):
    session = {"cwd": str(tmp_path)}
    with pytest.raises(ValueError):
        server._stage_session_file_attachment(
            session,
            raw_path=str(tmp_path / "missing.txt"),
            data_url="",
            name="missing.txt",
        )
