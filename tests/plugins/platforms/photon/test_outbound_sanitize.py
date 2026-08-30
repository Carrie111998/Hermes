"""Outbound text sanitization on the Photon (iMessage) send path.

Every outbound text send passes through ``_sidecar_send``, which runs
``sanitize_outbound_typography`` (see tests/gateway/platforms/
test_outbound_typography.py) so commands copied from an iMessage bubble are
pure ASCII and paste into a terminal unchanged.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.photon.adapter import PhotonAdapter


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


@pytest.mark.asyncio
async def test_send_sanitizes_outbound_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHOTON_MARKDOWN", raising=False)
    adapter = _make_adapter(monkeypatch)
    calls = _capture_sidecar(adapter)

    await adapter.send("+155****4567", "run \u201cdf -h\u201d \u2014 now")

    path, body = calls[0]
    assert path == "/send"
    assert body["text"] == 'run "df -h" - now'
    assert "\u201c" not in body["text"]
    assert "\u2014" not in body["text"]


@pytest.mark.asyncio
async def test_send_preserves_emoji_zwj_sequences(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHOTON_MARKDOWN", raising=False)
    adapter = _make_adapter(monkeypatch)
    calls = _capture_sidecar(adapter)

    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466"  # 👨👩👧👦
    await adapter.send("+155****4567", f"ok {family}")

    path, body = calls[0]
    assert path == "/send"
    assert body["text"] == f"ok {family}"
