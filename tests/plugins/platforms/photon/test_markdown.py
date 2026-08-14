"""Markdown handling tests for PhotonAdapter.

Plain text is the safe default. ``PHOTON_MARKDOWN=true`` explicitly opts into
the spectrum-ts ``markdown()`` builder.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.photon import adapter as photon_adapter
from plugins.platforms.photon.adapter import PhotonAdapter

_MD = "**bold** and `code`"


def _make_adapter(monkeypatch: pytest.MonkeyPatch) -> PhotonAdapter:
    monkeypatch.setenv("PHOTON_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("PHOTON_PROJECT_SECRET", "test-project-secret")
    cfg = PlatformConfig(enabled=True, token="", extra={})
    return PhotonAdapter(cfg)


def _capture_sidecar(adapter: PhotonAdapter) -> List[Tuple[str, Dict[str, Any]]]:
    calls: List[Tuple[str, Dict[str, Any]]] = []

    async def _fake_call(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        calls.append((path, body))
        return {"ok": True, "messageId": "msg-123"}

    adapter._sidecar_call = _fake_call  # type: ignore[assignment]
    return calls


def test_format_message_strips_markdown_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PHOTON_MARKDOWN", raising=False)
    adapter = _make_adapter(monkeypatch)
    assert adapter.format_message(_MD) == "bold and code"


def test_supports_code_blocks_mirrors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHOTON_MARKDOWN", raising=False)
    assert _make_adapter(monkeypatch).supports_code_blocks is False
    monkeypatch.setenv("PHOTON_MARKDOWN", "true")
    assert _make_adapter(monkeypatch).supports_code_blocks is True


@pytest.mark.asyncio
async def test_sidecar_send_defaults_to_stripped_plain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PHOTON_MARKDOWN", raising=False)
    adapter = _make_adapter(monkeypatch)
    calls = _capture_sidecar(adapter)

    await adapter.send("+15551234567", _MD)

    path, body = calls[0]
    assert path == "/send"
    assert "format" not in body
    assert body["text"] == "bold and code"


@pytest.mark.asyncio
async def test_sidecar_send_can_explicitly_opt_in_to_markdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOTON_MARKDOWN", "true")
    adapter = _make_adapter(monkeypatch)
    calls = _capture_sidecar(adapter)

    await adapter.send("+15551234567", _MD)

    path, body = calls[0]
    assert path == "/send"
    assert body["format"] == "markdown"
    assert body["text"] == _MD


@pytest.mark.asyncio
async def test_standalone_send_defaults_to_stripped_plain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PHOTON_MARKDOWN", raising=False)
    monkeypatch.setenv("PHOTON_SIDECAR_TOKEN", "tok")

    posted: List[Tuple[str, Dict[str, Any]]] = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> Dict[str, Any]:
            return {"ok": True, "messageId": "m-9"}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url: str, json: Dict[str, Any], headers=None):
            posted.append((url, json))
            return _Resp()

    monkeypatch.setattr(photon_adapter.httpx, "AsyncClient", _FakeClient)

    cfg = PlatformConfig(enabled=True, token="", extra={})
    result = await photon_adapter._standalone_send(cfg, "+15551234567", _MD)

    assert result.get("success") is True
    assert "format" not in posted[0][1]
    assert posted[0][1]["text"] == "bold and code"


@pytest.mark.asyncio
async def test_markdown_plus_raw_url_delivers_plain_builder_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PHOTON_MARKDOWN", raising=False)
    adapter = _make_adapter(monkeypatch)
    calls = _capture_sidecar(adapter)

    await adapter.send(
        "+15551234567",
        "## Update\n\n- **Read the docs:** https://example.com/docs",
    )

    path, body = calls[0]
    assert path == "/send"
    assert body == {
        "spaceId": "+15551234567",
        "text": "Update\nRead the docs: https://example.com/docs",
    }
    for marker in ("##", "\n- ", "- **", "**", "[the docs]("):
        assert marker not in body["text"]
