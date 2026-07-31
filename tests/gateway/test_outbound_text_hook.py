from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.outbound_text_hook import prepare_outbound_text_file
from plugins.platforms.telegram.adapter import TelegramAdapter


def test_cp1251_txt_becomes_utf8_bom_without_touching_source(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = tmp_path / "obzor.txt"
    original = "Привет, Дима и Макс!".encode("cp1251")
    source.write_bytes(original)

    prepared = prepare_outbound_text_file(str(source), "Обзор — финал.txt")

    payload = Path(prepared.path).read_bytes()
    assert prepared.changed is True
    assert prepared.file_name == "Обзор_финал.txt"
    assert prepared.source_encoding == "cp1251"
    assert prepared.output_encoding == "utf-8-sig"
    assert payload.startswith(b"\xef\xbb\xbf")
    assert payload.decode("utf-8-sig") == "Привет, Дима и Макс!"
    assert source.read_bytes() == original


def test_utf16_markdown_becomes_utf8_bom(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    source = tmp_path / "report.md"
    source.write_bytes("# Русский отчёт\nТезисы".encode("utf-16"))

    prepared = prepare_outbound_text_file(str(source))

    payload = Path(prepared.path).read_bytes()
    assert payload.startswith(b"\xef\xbb\xbf")
    assert payload.decode("utf-8-sig") == "# Русский отчёт\nТезисы"


def test_json_is_utf8_without_bom(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    source = tmp_path / "data.json"
    source.write_bytes('{"тест": true}'.encode("cp1251"))

    prepared = prepare_outbound_text_file(str(source))

    payload = Path(prepared.path).read_bytes()
    assert not payload.startswith(b"\xef\xbb\xbf")
    assert payload.decode("utf-8") == '{"тест": true}'
    assert prepared.output_encoding == "utf-8"


def test_binary_payload_with_text_extension_is_not_rewritten(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    source = tmp_path / "fake.txt"
    source.write_bytes(b"\x00\x01\xffbinary")

    prepared = prepare_outbound_text_file(str(source))

    assert prepared.changed is False
    assert prepared.path == str(source)
    assert source.read_bytes() == b"\x00\x01\xffbinary"


@pytest.mark.asyncio
async def test_telegram_send_document_applies_hook_before_upload(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    source = tmp_path / "report.txt"
    source.write_bytes("Тезисы для Димы".encode("cp1251"))

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="123456789:TEST"))
    captured = {}

    async def fake_send_document(**kwargs):
        captured["filename"] = kwargs["filename"]
        captured["payload"] = kwargs["document"].read()
        return SimpleNamespace(message_id=77)

    async def relay(func, kwargs, *_args, **_kw):
        return await func(**kwargs)

    adapter._bot = SimpleNamespace(send_document=fake_send_document)
    adapter._send_with_dm_topic_reply_anchor_retry = AsyncMock(side_effect=relay)

    result = await adapter.send_document(
        chat_id="123",
        file_path=str(source),
        file_name="Русский — обзор.txt",
    )

    assert result.success is True
    assert result.message_id == "77"
    assert captured["filename"] == "Русский_обзор.txt"
    assert captured["payload"].startswith(b"\xef\xbb\xbf")
    assert captured["payload"].decode("utf-8-sig") == "Тезисы для Димы"
    assert source.read_bytes() == "Тезисы для Димы".encode("cp1251")
