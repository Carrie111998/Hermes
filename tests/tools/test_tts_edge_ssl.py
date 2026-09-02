"""Regression tests for custom CA handling in Edge TTS."""

import asyncio
import ssl
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_edge_connector_overrides_dependency_request_context(monkeypatch):
    """The Hermes CA must win over edge-tts' explicit certifi context."""
    from tools import tts_tool

    hermes_context = ssl.create_default_context()
    dependency_context = ssl.create_default_context()
    monkeypatch.setattr(
        "agent.ssl_verify.resolve_httpx_verify",
        lambda: hermes_context,
    )

    async def check_connector():
        connector = tts_tool._edge_tts_connector()
        request = MagicMock()
        request.is_ssl.return_value = True
        request.ssl = dependency_context

        try:
            assert connector is not None
            assert connector._get_ssl_context(request) is hermes_context
        finally:
            await connector.close_from_hermes()

    asyncio.run(check_connector())


def test_edge_connector_preserves_dependency_default_without_custom_ca(monkeypatch):
    """No custom trust setting should leave edge-tts' defaults untouched."""
    from tools import tts_tool

    monkeypatch.setattr("agent.ssl_verify.resolve_httpx_verify", lambda: True)

    assert tts_tool._edge_tts_connector() is None


def test_generate_edge_tts_passes_custom_ca_connector(tmp_path):
    """The synthesis path must give the enforcing connector to edge-tts."""
    from tools.tts_tool import _generate_edge_tts

    connector = MagicMock()
    connector.close_from_hermes = AsyncMock()
    communicate = MagicMock()
    communicate.save = AsyncMock()
    edge_tts = MagicMock()
    edge_tts.Communicate.return_value = communicate

    with patch("tools.tts_tool._import_edge_tts", return_value=edge_tts), patch(
        "tools.tts_tool._edge_tts_connector", return_value=connector
    ):
        asyncio.run(_generate_edge_tts("Hello", str(tmp_path / "out.mp3"), {}))

    assert edge_tts.Communicate.call_args.kwargs["connector"] is connector
    connector.close_from_hermes.assert_awaited_once_with()


def test_generate_edge_tts_keeps_connector_across_owned_chunk_sessions(
    tmp_path, monkeypatch
):
    """Per-chunk sessions must not close the shared custom-CA connector."""
    import aiohttp

    from tools import tts_tool

    hermes_context = ssl.create_default_context()
    connectors = []
    chunk_connector_states = []

    monkeypatch.setattr(
        "agent.ssl_verify.resolve_httpx_verify",
        lambda: hermes_context,
    )
    original_connector_factory = tts_tool._edge_tts_connector

    def capture_connector():
        connector = original_connector_factory()
        connectors.append(connector)
        return connector

    class ChunkingCommunicate:
        def __init__(self, text, **kwargs):
            self.connector = kwargs["connector"]

        async def save(self, output_path):
            for _ in range(2):
                chunk_connector_states.append(self.connector.closed)
                async with aiohttp.ClientSession(connector=self.connector):
                    pass
            Path(output_path).write_bytes(b"audio")

    edge_tts = MagicMock(Communicate=ChunkingCommunicate)
    monkeypatch.setattr(tts_tool, "_edge_tts_connector", capture_connector)
    monkeypatch.setattr(tts_tool, "_import_edge_tts", lambda: edge_tts)

    output_path = tmp_path / "out.mp3"
    asyncio.run(tts_tool._generate_edge_tts("long text", str(output_path), {}))

    assert chunk_connector_states == [False, False]
    assert connectors[0].closed


def test_generate_edge_tts_closes_connector_when_save_fails(tmp_path, monkeypatch):
    """Hermes must release the custom connector on synthesis failures."""
    from tools import tts_tool

    connector = MagicMock()
    connector.close_from_hermes = AsyncMock()
    communicate = MagicMock()
    communicate.save = AsyncMock(side_effect=RuntimeError("synthesis failed"))
    edge_tts = MagicMock()
    edge_tts.Communicate.return_value = communicate

    monkeypatch.setattr(tts_tool, "_edge_tts_connector", lambda: connector)
    monkeypatch.setattr(tts_tool, "_import_edge_tts", lambda: edge_tts)

    with pytest.raises(RuntimeError, match="synthesis failed"):
        asyncio.run(
            tts_tool._generate_edge_tts("Hello", str(tmp_path / "out.mp3"), {})
        )

    connector.close_from_hermes.assert_awaited_once_with()
