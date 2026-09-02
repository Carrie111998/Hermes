from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner, _relocate_inbound_media_for_active_profile
from gateway.session import SessionSource, build_session_key
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _scoped_relocate(event: MessageEvent, profile_home: Path) -> None:
    token = set_hermes_home_override(profile_home)
    try:
        _relocate_inbound_media_for_active_profile(event)
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("path_form", ["host", "container"])
def test_relocates_inbound_document_to_active_profile(
    tmp_path, monkeypatch, path_form
):
    process_home = tmp_path / "root"
    profile_home = process_home / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    source = process_home / "cache" / "documents" / "report.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-1.4 profile scoped")
    monkeypatch.setenv("HERMES_HOME", str(process_home))
    monkeypatch.setenv("TERMINAL_ENV", "docker")

    original = (
        str(source)
        if path_form == "host"
        else "/root/.hermes/cache/documents/report.pdf"
    )
    event = MessageEvent(text="inspect", media_urls=[original])

    _scoped_relocate(event, profile_home)

    target = profile_home / "cache" / "documents" / "report.pdf"
    assert target.read_bytes() == b"%PDF-1.4 profile scoped"
    assert not source.exists()
    assert event.media_urls == ([str(target)] if path_form == "host" else [original])

    _scoped_relocate(event, profile_home)
    assert target.read_bytes() == b"%PDF-1.4 profile scoped"
    assert event.media_urls == ([str(target)] if path_form == "host" else [original])


def test_permission_error_keeps_original_path(tmp_path, monkeypatch):
    process_home = tmp_path / "root"
    profile_home = process_home / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    source = process_home / "cache" / "documents" / "report.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF")
    monkeypatch.setenv("HERMES_HOME", str(process_home))
    monkeypatch.setenv("TERMINAL_ENV", "docker")

    original_is_file = Path.is_file

    def guarded_is_file(path):
        if path == source:
            raise PermissionError("not readable")
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", guarded_is_file)
    container_path = "/root/.hermes/cache/documents/report.pdf"
    event = MessageEvent(text="inspect", media_urls=[container_path])

    _scoped_relocate(event, profile_home)

    assert event.media_urls == [container_path]
    assert source.read_bytes() == b"%PDF"
    assert not (profile_home / "cache" / "documents" / "report.pdf").exists()


@pytest.mark.asyncio
async def test_preprocessing_relocates_before_native_image_buffering(
    tmp_path, monkeypatch
):
    process_home = tmp_path / "root"
    profile_home = process_home / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    source_path = process_home / "cache" / "images" / "photo.png"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setenv("HERMES_HOME", str(process_home))
    monkeypatch.setenv("TERMINAL_ENV", "local")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    runner._decide_image_input_mode = lambda **_: "native"
    runner._session_key_for_source = lambda source: build_session_key(source)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-a",
        chat_type="private",
        profile="coder",
    )
    event = MessageEvent(
        text="inspect",
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=[str(source_path)],
        media_types=["image/png"],
    )

    token = set_hermes_home_override(profile_home)
    try:
        await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )
    finally:
        reset_hermes_home_override(token)

    target = profile_home / "cache" / "images" / "photo.png"
    assert target.read_bytes() == b"\x89PNG\r\n\x1a\n"
    assert event.media_urls == [str(target)]
    assert runner._consume_pending_native_image_paths(build_session_key(source)) == [
        str(target)
    ]
