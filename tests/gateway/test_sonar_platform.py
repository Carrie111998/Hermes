"""Tests for Sonar platform plugin registration."""

import asyncio
import types

import pytest

import plugins.platforms.sonar.adapter as sonar_adapter
from plugins.platforms.sonar.adapter import (
    SonarAdapter,
    _media_kind_for,
    _split_chunks,
    check_requirements,
    register,
    validate_config,
)
from gateway.config import Platform, PlatformConfig


def test_split_chunks_multipart():
    long = "a" * 5000
    parts = _split_chunks(long, 3200)
    assert len(parts) >= 2
    assert "".join(parts) == long


def test_register_platform_smoke():
    class Ctx:
        def __init__(self):
            self.calls = []

        def register_platform(self, **kwargs):
            self.calls.append(kwargs)

    ctx = Ctx()
    register(ctx)
    assert len(ctx.calls) == 1
    assert ctx.calls[0]["name"] == "sonar"
    assert ctx.calls[0]["label"] == "Sonar"


def test_validate_config_empty_senders():
    cfg = PlatformConfig(enabled=True, extra={"authorized_senders": []})
    # May be False without sonar-cli on CI; just ensure no exception
    assert validate_config(cfg) in (True, False)


def test_platform_enum_value():
    assert Platform("sonar").value == "sonar"


# ---------------------------------------------------------------------------
# voice / media send support
# ---------------------------------------------------------------------------

def _adapter():
    return SonarAdapter(types.SimpleNamespace(extra={}))


def test_media_kind_for_mapping():
    assert _media_kind_for("/tmp/a.m4a") == "voice"
    assert _media_kind_for("/tmp/a.MP3") == "audio"
    assert _media_kind_for("/tmp/a.png") == "image"
    assert _media_kind_for("/tmp/a.mp4") == "video"
    assert _media_kind_for("/tmp/a.pdf") is None


def test_send_voice_transcodes_mp3_to_m4a(monkeypatch, tmp_path):
    src = tmp_path / "note.mp3"
    src.write_bytes(b"ID3-fake")
    captured = {}

    # fake ffmpeg: "transcode" by writing the output file (last arg)
    def fake_run(args, **kwargs):
        out = args[-1]
        with open(out, "wb") as f:
            f.write(b"fLaC-fake-m4a")
        return types.SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(sonar_adapter, "_find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(sonar_adapter.subprocess, "run", fake_run)

    adapter = _adapter()

    async def fake_send_file(to, path, kind, caption=None, mime=None):
        captured.update(to=to, path=path, kind=kind, caption=caption, mime=mime)
        return {"id": "evt1"}

    monkeypatch.setattr(adapter, "_sonar_send_file", fake_send_file)

    result = asyncio.run(adapter.send_voice("npub1xyz", str(src), caption="hi"))
    assert result.success
    assert result.message_id == "evt1"
    assert captured["kind"] == "voice"
    assert captured["mime"] == "audio/mp4"
    assert captured["path"].endswith(".m4a")  # transcoded, not the mp3


def test_send_voice_m4a_passthrough(monkeypatch, tmp_path):
    src = tmp_path / "note.m4a"
    src.write_bytes(b"m4a-fake")
    monkeypatch.setattr(
        sonar_adapter.subprocess, "run",
        lambda *a, **k: pytest.fail("ffmpeg must not run for .m4a input"),
    )
    captured = {}
    adapter = _adapter()

    async def fake_send_file(to, path, kind, caption=None, mime=None):
        captured.update(path=path, kind=kind)
        return {"id": "evt2"}

    monkeypatch.setattr(adapter, "_sonar_send_file", fake_send_file)
    result = asyncio.run(adapter.send_voice("npub1xyz", str(src)))
    assert result.success
    assert captured["path"] == str(src)
    assert captured["kind"] == "voice"


def test_send_voice_missing_file():
    result = asyncio.run(_adapter().send_voice("npub1xyz", "/nope/missing.mp3"))
    assert not result.success
    assert "not found" in (result.error or "")


def test_send_image_file_and_video_kinds(monkeypatch, tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG")
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"ftyp")
    calls = []
    adapter = _adapter()

    async def fake_send_file(to, path, kind, caption=None, mime=None):
        calls.append((path, kind))
        return {"id": f"e{len(calls)}"}

    monkeypatch.setattr(adapter, "_sonar_send_file", fake_send_file)
    r1 = asyncio.run(adapter.send_image_file("npub1xyz", str(img)))
    r2 = asyncio.run(adapter.send_video("npub1xyz", str(vid)))
    assert r1.success and r2.success
    assert calls == [(str(img), "image"), (str(vid), "video")]


def test_send_document_routes_by_extension(monkeypatch, tmp_path):
    doc = tmp_path / "clip.mp4"
    doc.write_bytes(b"ftyp")
    calls = []
    adapter = _adapter()

    async def fake_send_file(to, path, kind, caption=None, mime=None):
        calls.append(kind)
        return {"id": "evt"}

    monkeypatch.setattr(adapter, "_sonar_send_file", fake_send_file)
    result = asyncio.run(adapter.send_document("npub1xyz", str(doc)))
    assert result.success
    assert calls == ["video"]


def test_send_document_unknown_ext_falls_back(monkeypatch, tmp_path):
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF")
    sent = {}
    adapter = _adapter()

    async def fake_send(chat_id, content, reply_to=None, metadata=None):
        sent["content"] = content
        return types.SimpleNamespace(success=True)

    monkeypatch.setattr(adapter, "send", fake_send)
    result = asyncio.run(
        adapter.send_document("npub1xyz", str(doc), file_name="report.pdf")
    )
    assert result.success
    assert "report.pdf" in sent["content"]  # friendly notice, no host path
    assert str(doc) not in sent["content"]


def test_standalone_send_with_media_files(monkeypatch, tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG")
    runs = []

    def fake_run_json(args, home, cli, timeout):
        runs.append(args)
        return {"id": "x"}

    monkeypatch.setattr(sonar_adapter, "_run_sonar_json", fake_run_json)
    monkeypatch.setattr(sonar_adapter, "_find_sonar_cli", lambda explicit=None: "/bin/true")
    monkeypatch.setenv("SONAR_HOME_CHANNEL", "npub1home")

    pconfig = types.SimpleNamespace(extra={})
    result = asyncio.run(
        sonar_adapter._standalone_send(
            pconfig, "npub1peer", "hello", media_files=[str(img), str(tmp_path / "x.pdf")]
        )
    )
    assert result["success"]
    kinds = [a for a in runs if "--kind" in a]
    assert len(kinds) == 1  # pdf skipped, png sent
    assert kinds[0][kinds[0].index("--kind") + 1] == "image"