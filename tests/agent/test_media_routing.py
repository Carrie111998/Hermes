import base64

from agent.gemini_native_adapter import _extract_multimodal_parts
from agent.media_routing import (
    build_native_media_content_parts,
    supported_input_modalities,
)


def test_active_gemini_flash_lite_declares_all_multimodal_inputs():
    from types import SimpleNamespace
    from unittest.mock import patch

    fake_info = SimpleNamespace(
        input_modalities=["text", "image", "pdf", "video", "audio"]
    )
    with patch("agent.models_dev.get_model_info", return_value=fake_info):
        modalities = supported_input_modalities(
            "openrouter", "google/gemini-3.5-flash-lite"
        )
        assert {"text", "image", "pdf", "video", "audio"} <= modalities


def test_builds_openrouter_native_parts_for_all_media(tmp_path):
    files = {
        "image": (tmp_path / "photo.png", "image/png"),
        "pdf": (tmp_path / "notes.pdf", "application/pdf"),
        "audio": (tmp_path / "sample.mp3", "audio/mpeg"),
        "video": (tmp_path / "clip.mp4", "video/mp4"),
    }
    for path, _mime in files.values():
        path.write_bytes(b"test-bytes")

    parts, skipped = build_native_media_content_parts(
        "analyze everything",
        [
            {"path": str(path), "mime_type": mime, "modality": modality}
            for modality, (path, mime) in files.items()
        ],
    )

    assert skipped == []
    assert [part["type"] for part in parts] == [
        "text",
        "image_url",
        "file",
        "input_audio",
        "video_url",
    ]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[2]["file"]["file_data"].startswith("data:application/pdf;base64,")
    assert parts[3]["input_audio"]["format"] == "mp3"
    assert parts[3]["input_audio"]["data"] == base64.b64encode(b"test-bytes").decode()
    assert parts[4]["video_url"]["url"].startswith("data:video/mp4;base64,")


def test_gemini_native_adapter_converts_non_image_media_to_inline_data(tmp_path):
    pdf = tmp_path / "notes.pdf"
    audio = tmp_path / "sample.mp3"
    video = tmp_path / "clip.mp4"
    for path in (pdf, audio, video):
        path.write_bytes(b"native-data")

    parts, skipped = build_native_media_content_parts(
        "analyze",
        [
            {"path": str(pdf), "mime_type": "application/pdf", "modality": "pdf"},
            {"path": str(audio), "mime_type": "audio/mpeg", "modality": "audio"},
            {"path": str(video), "mime_type": "video/mp4", "modality": "video"},
        ],
    )
    assert skipped == []

    native = _extract_multimodal_parts(parts)
    assert native[0] == {"text": parts[0]["text"]}
    assert [part["inlineData"]["mimeType"] for part in native[1:]] == [
        "application/pdf",
        "audio/mpeg",
        "video/mp4",
    ]


def test_unreadable_attachment_falls_back_without_fake_media(tmp_path):
    missing = tmp_path / "missing.pdf"
    parts, skipped = build_native_media_content_parts(
        "read this",
        [{"path": str(missing), "mime_type": "application/pdf", "modality": "pdf"}],
    )
    assert parts == [{"type": "text", "text": "read this"}]
    assert skipped == [str(missing)]


def test_attachment_exceeding_size_ceiling_is_skipped(tmp_path):
    oversized = tmp_path / "large_voice.ogg"
    oversized.write_bytes(b"x" * 200)

    # Test with custom max_size_bytes
    parts, skipped = build_native_media_content_parts(
        "listen",
        [{"path": str(oversized), "mime_type": "audio/ogg", "modality": "audio"}],
        max_size_bytes=100,
    )
    assert parts == [{"type": "text", "text": "listen"}]
    assert skipped == [str(oversized)]


def test_attachment_size_ceiling_includes_base64_expansion(tmp_path):
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"x" * 76)  # 104 bytes after Base64 encoding.

    parts, skipped = build_native_media_content_parts(
        "listen",
        [{"path": str(audio), "mime_type": "audio/mpeg", "modality": "audio"}],
        max_size_bytes=100,
    )

    assert parts == [{"type": "text", "text": "listen"}]
    assert skipped == [str(audio)]


def test_audio_format_and_mime_normalization():
    from agent.media_routing import normalize_audio_format, normalize_audio_mime
    from pathlib import Path

    assert normalize_audio_format(Path("audio.mp3")) == "mp3"
    assert normalize_audio_format(Path("audio.ogg")) == "ogg"
    assert normalize_audio_format(Path("audio.opus")) == "ogg"
    assert normalize_audio_format(Path("audio.wav")) == "wav"
    assert normalize_audio_format(Path("audio.flac")) == "flac"
    assert normalize_audio_format(mime="audio/mpeg") == "mp3"
    assert normalize_audio_format(mime="audio/ogg") == "ogg"

    assert normalize_audio_mime("mp3") == "audio/mpeg"
    assert normalize_audio_mime("mpeg") == "audio/mpeg"
    assert normalize_audio_mime("ogg") == "audio/ogg"
    assert normalize_audio_mime("opus") == "audio/ogg"
    assert normalize_audio_mime("wav") == "audio/wav"
    assert normalize_audio_mime("flac") == "audio/flac"
    assert normalize_audio_mime("audio/custom") == "audio/custom"


def test_modality_auto_derived_when_omitted(tmp_path):
    img = tmp_path / "screenshot.png"
    img.write_bytes(b"png-data")
    audio = tmp_path / "memo.ogg"
    audio.write_bytes(b"ogg-data")

    parts, skipped = build_native_media_content_parts(
        "check",
        [
            {"path": str(img), "mime_type": "image/png"},
            {"path": str(audio), "mime_type": "audio/ogg"},
        ],
    )
    assert skipped == []
    assert [part["type"] for part in parts] == ["text", "image_url", "input_audio"]


def test_magic_bytes_audio_format_and_mime_detection(tmp_path):
    from agent.media_routing import _mime_type, normalize_audio_format

    # An audio file with no extension or misleading extension
    audio_unknown = tmp_path / "voice_cache_123"
    audio_unknown.write_bytes(b"OggS" + b"\x00" * 28)

    assert normalize_audio_format(audio_unknown) == "ogg"
    assert _mime_type(audio_unknown, "") == "audio/ogg"


def test_rich_hint_contains_size_and_mime(tmp_path):
    photo = tmp_path / "photo.png"
    photo.write_bytes(b"x" * 2048)  # 2.0 KB

    parts, _ = build_native_media_content_parts(
        "describe",
        [{"path": str(photo), "mime_type": "image/png", "modality": "image"}],
    )
    assert len(parts) == 2
    hint_text = parts[0]["text"]
    assert "2.0 KB" in hint_text
    assert "image/png" in hint_text


def test_openai_target_provider_triggers_audio_transcoding(tmp_path, monkeypatch):
    import agent.media_routing as mr

    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggS" + b"\x00" * 28)

    monkeypatch.setattr(
        mr,
        "transcode_audio_to_supported_format",
        lambda *args, **kwargs: (b"fake-mp3-bytes", "mp3"),
    )

    parts, skipped = build_native_media_content_parts(
        "listen to voice",
        [{"path": str(audio), "mime_type": "audio/ogg", "modality": "audio"}],
        target_provider="openai",
    )
    assert skipped == []
    audio_part = next(p for p in parts if p.get("type") == "input_audio")
    assert audio_part["input_audio"]["format"] == "mp3"
    assert audio_part["input_audio"]["data"] == base64.b64encode(
        b"fake-mp3-bytes"
    ).decode("ascii")


def test_openai_transcode_failure_skips_unsupported_audio(tmp_path, monkeypatch):
    import agent.media_routing as mr

    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggS" + b"\x00" * 28)
    monkeypatch.setattr(
        mr, "transcode_audio_to_supported_format", lambda *_a, **_k: None
    )

    for target_provider in ("openai", "azure"):
        parts, skipped = build_native_media_content_parts(
            "listen to voice",
            [{"path": str(audio), "mime_type": "audio/ogg", "modality": "audio"}],
            target_provider=target_provider,
        )

        assert parts == [{"type": "text", "text": "listen to voice"}]
        assert skipped == [str(audio)]


def test_failed_audio_keeps_images_on_existing_native_pipeline(tmp_path):
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    missing_audio = tmp_path / "missing.ogg"

    parts, skipped = build_native_media_content_parts(
        "look and listen",
        [
            {"path": str(image), "mime_type": "image/png", "modality": "image"},
            {
                "path": str(missing_audio),
                "mime_type": "audio/ogg",
                "modality": "audio",
            },
        ],
        # The established image pipeline handles provider limits reactively;
        # this ceiling applies to newly inlined non-image payloads only.
        max_size_bytes=16,
    )

    assert any(part.get("type") == "image_url" for part in parts)
    assert not any(part.get("type") == "input_audio" for part in parts)
    assert skipped == [str(missing_audio)]


def test_gemini_adapter_does_not_fetch_arbitrary_remote_media_urls():
    parts = _extract_multimodal_parts([
        {"type": "text", "text": "inspect these references"},
        {
            "type": "video_url",
            "video_url": {"url": "https://example.com/video.mp4"},
        },
        {
            "type": "file",
            "file": {"file_data": "https://example.com/document.pdf"},
        },
    ])

    assert parts == [{"text": "inspect these references"}]


def test_transcode_audio_when_ffmpeg_missing(tmp_path, monkeypatch):
    import shutil
    from agent.media_routing import transcode_audio_to_supported_format

    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggS" + b"\x00" * 28)

    monkeypatch.setattr(shutil, "which", lambda *_: None)
    assert transcode_audio_to_supported_format(audio) is None


def test_transcode_audio_successful_subprocess(tmp_path, monkeypatch):
    from pathlib import Path
    import shutil
    import subprocess
    from agent.media_routing import transcode_audio_to_supported_format

    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggS" + b"\x00" * 28)

    monkeypatch.setattr(shutil, "which", lambda *_: "/usr/bin/ffmpeg")

    def fake_run(cmd, **kwargs):
        out_file = Path(cmd[-1])
        out_file.write_bytes(b"fake-mp3-output")
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = transcode_audio_to_supported_format(audio, target_format="mp3")
    assert result is not None
    data, fmt = result
    assert data == b"fake-mp3-output"
    assert fmt == "mp3"


def test_transcode_audio_subprocess_failure(tmp_path, monkeypatch):
    import shutil
    import subprocess
    from agent.media_routing import transcode_audio_to_supported_format

    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggS" + b"\x00" * 28)

    monkeypatch.setattr(shutil, "which", lambda *_: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, returncode=1),
    )
    assert transcode_audio_to_supported_format(audio, target_format="mp3") is None


def test_rich_hint_formats_megabytes_for_large_files(tmp_path):
    big_file = tmp_path / "recording.wav"
    big_file.write_bytes(b"x" * int(2.5 * 1024 * 1024))

    parts, skipped = build_native_media_content_parts(
        "listen",
        [{"path": str(big_file), "mime_type": "audio/wav", "modality": "audio"}],
    )
    assert skipped == []
    assert len(parts) == 2
    hint_text = parts[0]["text"]
    assert "2.5 MB" in hint_text
    assert "audio/wav" in hint_text


def test_supported_input_modalities_openrouter_vendor_fallback():
    from types import SimpleNamespace
    from unittest.mock import patch

    fake_info = SimpleNamespace(input_modalities=["text", "audio"])

    def mock_get_info(provider, model):
        if provider == "openrouter":
            return None
        if provider == "openai" and model == "gpt-4o":
            return fake_info
        return None

    with patch("agent.models_dev.get_model_info", side_effect=mock_get_info):
        modalities = supported_input_modalities("openrouter", "openai/gpt-4o")
        assert "audio" in modalities
        assert "text" in modalities

    with patch("agent.models_dev.get_model_info", return_value=None):
        modalities = supported_input_modalities("openrouter", "unknown/model")
        assert modalities == set()


def test_unsupported_modality_is_skipped(tmp_path):
    f = tmp_path / "unknown.bin"
    f.write_bytes(b"binary-data")
    parts, skipped = build_native_media_content_parts(
        "check",
        [{"path": str(f), "mime_type": "application/octet-stream", "modality": "custom_binary"}],
    )
    assert skipped == [str(f)]
    assert parts == [{"type": "text", "text": "check"}]


def test_bare_string_paths_accepted_in_attachments(tmp_path):
    img = tmp_path / "simple.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    parts, skipped = build_native_media_content_parts(
        "look",
        [str(img)],
    )
    assert skipped == []
    assert len(parts) == 2
    assert parts[1]["type"] == "image_url"
