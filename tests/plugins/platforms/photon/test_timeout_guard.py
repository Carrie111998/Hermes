"""Photon send-path timeouts must not trigger the plain-text resend.

httpx transport exceptions can stringify to an empty string — with the pinned
``httpx==0.28.1``, ``str(httpx.ReadTimeout(""))`` is ``""``. ``_send_with_retry``
classifies failures by substring on ``SendResult.error``, and its timeout guard
returns False for empty input, so a ``/send`` whose HTTP reply was lost past
the sidecar timeout fell through to the unconditional plain-text fallback and
delivered the same message twice (issue #100034).

These tests pin the guard end-to-end: an exception whose ``str()`` is empty
must surface a classifiable error string, and the resend must never happen.
"""

from __future__ import annotations

from typing import Any, Dict, List

import httpx
import pytest

from gateway.config import PlatformConfig
from plugins.platforms.photon import adapter as photon_adapter
from plugins.platforms.photon.adapter import PhotonAdapter


def _make_adapter(monkeypatch: pytest.MonkeyPatch) -> PhotonAdapter:
    monkeypatch.setenv("PHOTON_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("PHOTON_PROJECT_SECRET", "test-project-secret")
    cfg = PlatformConfig(enabled=True, token="", extra={})
    return PhotonAdapter(cfg)


@pytest.mark.asyncio
async def test_empty_message_timeout_not_resent_as_plain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter(monkeypatch)
    paths: List[str] = []

    async def _fake_sidecar_call(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        paths.append(path)
        # Reply lost past the 30s sidecar timeout; str(exc) == "" on httpx 0.28.1.
        raise httpx.ReadTimeout("")

    async def _fake_sleep(
        delay: float,
    ) -> None:  # pragma: no cover - guard must return before any sleep
        raise AssertionError("timeout guard must return before any retry sleep")

    monkeypatch.setattr(photon_adapter.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(adapter, "_sidecar_call", _fake_sidecar_call)

    result = await adapter._send_with_retry(
        "space-1", "hello", max_retries=1, base_delay=0.25
    )

    # The original failure is returned as-is — no second /send for the
    # plain-text fallback, because the request may already be delivered.
    assert result.success is False
    assert paths == ["/send"]
    assert "ReadTimeout" in (result.error or "")
    assert PhotonAdapter._is_timeout_error(result.error) is True


@pytest.mark.asyncio
async def test_sidecar_send_surfaces_exception_type_on_empty_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter(monkeypatch)

    async def _fake_sidecar_call(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        raise httpx.ReadTimeout("")

    monkeypatch.setattr(adapter, "_sidecar_call", _fake_sidecar_call)

    result = await adapter._sidecar_send(
        "space-1", "hello", richlink=False, markdown=False
    )

    assert result.success is False
    # The exception type keeps the error classifiable even when str(e) is empty.
    assert (result.error or "").startswith("ReadTimeout")
    assert PhotonAdapter._is_timeout_error(result.error) is True
