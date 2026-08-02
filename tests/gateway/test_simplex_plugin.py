"""Tests for the SimpleX Chat platform-plugin adapter.

Loaded via the ``_plugin_adapter_loader`` helper so this lives under
``plugin_adapter_simplex`` in ``sys.modules`` and cannot collide with
sibling platform-plugin tests on the same xdist worker.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import websockets

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_simplex = load_plugin_adapter("simplex")

SimplexAdapter = _simplex.SimplexAdapter
check_requirements = _simplex.check_requirements
validate_config = _simplex.validate_config
is_connected = _simplex.is_connected
register = _simplex.register
_env_enablement = _simplex._env_enablement
_standalone_send = _simplex._standalone_send
_guess_extension = _simplex._guess_extension
_is_image_ext = _simplex._is_image_ext
_is_audio_ext = _simplex._is_audio_ext
_CORR_PREFIX = _simplex._CORR_PREFIX


# ---------------------------------------------------------------------------
# 1. Platform enum (plugin-discovered, not bundled)
# ---------------------------------------------------------------------------

def test_platform_enum_resolves_via_plugin_scan():
    """The plugin filesystem scan should expose Platform("simplex")."""
    from gateway.config import Platform
    p = Platform("simplex")
    assert p.value == "simplex"
    # Identity stability — repeated lookups return the same pseudo-member
    assert Platform("simplex") is p


# ---------------------------------------------------------------------------
# 2. check_requirements / validate_config / is_connected
# ---------------------------------------------------------------------------


def test_check_requirements_true_when_configured(monkeypatch):
    monkeypatch.setenv("SIMPLEX_WS_URL", "ws://127.0.0.1:5225")
    # websockets is a dev dep in this repo via the test plugins; the
    # check_requirements() gate also asserts the package imports.
    websockets_present = True
    try:
        import websockets  # noqa: F401
    except ImportError:
        websockets_present = False
    assert check_requirements() is websockets_present


def test_validate_config_uses_env_or_extra():
    from gateway.config import PlatformConfig
    # Empty extra + no env → invalid
    cfg = PlatformConfig(enabled=True)
    assert validate_config(cfg) is False
    # extra-only path → valid
    cfg2 = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    assert validate_config(cfg2) is True


def test_is_connected_mirrors_validate(monkeypatch):
    from gateway.config import PlatformConfig
    monkeypatch.delenv("SIMPLEX_WS_URL", raising=False)
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://x"})
    assert is_connected(cfg) is True
    assert is_connected(PlatformConfig(enabled=True)) is False


# ---------------------------------------------------------------------------
# 3. _env_enablement seeds PlatformConfig.extra
# ---------------------------------------------------------------------------


def test_env_enablement_seeds_home_channel(monkeypatch):
    monkeypatch.setenv("SIMPLEX_WS_URL", "ws://127.0.0.1:5225")
    monkeypatch.setenv("SIMPLEX_HOME_CHANNEL", "42")
    monkeypatch.setenv("SIMPLEX_HOME_CHANNEL_NAME", "Personal")
    seed = _env_enablement()
    assert seed["home_channel"] == {"chat_id": "42", "name": "Personal"}


# ---------------------------------------------------------------------------
# 4. Adapter init
# ---------------------------------------------------------------------------

def test_adapter_init_custom_url():
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)
    assert adapter.ws_url == "ws://localhost:5225"
    assert adapter._running is False
    assert adapter._ws is None


# ---------------------------------------------------------------------------
# 5. Helper functions (magic-byte detection)
# ---------------------------------------------------------------------------

def test_guess_extension_png():
    assert _guess_extension(b"\x89PNG\r\n\x1a\n") == ".png"


# ---------------------------------------------------------------------------
# 6. Correlation IDs
# ---------------------------------------------------------------------------


def test_corr_id_pending_set_self_trims():
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)
    adapter._max_pending_corr = 4
    for _ in range(10):
        adapter._make_corr_id()
    # After many additions, the pending set should be bounded by the trim
    # logic — at most one trim window above the cap.
    assert len(adapter._pending_corr_ids) <= adapter._max_pending_corr + 1


# ---------------------------------------------------------------------------
# 7. Outbound send (mocked WS)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_group():
    """Groups use the structured ``/_send #<id> json [...]`` form.

    The bracket chat-command form ``#[<id>] text`` *looks* like an exact
    ID match in the daemon docs but is parsed as a display-name lookup
    — so messages to groups whose display name isn't literally the ID
    silently drop. The structured ``/_send`` form addresses by numeric
    ID and survives newlines/quoting through ``json.dumps``.
    """
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)

    mock_ws = AsyncMock()
    adapter._ws = mock_ws

    result = await adapter.send("group:grp-99", "Hello, group!")
    payload = json.loads(mock_ws.send.call_args[0][0])
    assert payload["cmd"].startswith("/_send #grp-99 json ")
    msg_content = json.loads(payload["cmd"].split(" json ", 1)[1])[0][
        "msgContent"
    ]
    assert msg_content == {"type": "text", "text": "Hello, group!"}
    assert result.success is True


# ---------------------------------------------------------------------------
# 8. Inbound: filter own-echo by corrId prefix
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 9. Standalone (out-of-process) send for cron
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_standalone_send_missing_websockets(monkeypatch):
    """When websockets is unimportable, return a clean error dict.

    Implementation detail: the standalone path does ``import websockets``
    inside the function body. We simulate the package being absent by
    pulling it out of ``sys.modules`` and pointing the finder at None.
    """
    import sys
    saved_websockets = sys.modules.pop("websockets", None)
    saved_meta = list(sys.meta_path)

    class _Blocker:
        @staticmethod
        def find_spec(name, path=None, target=None):
            if name == "websockets" or name.startswith("websockets."):
                raise ImportError("websockets blocked for test")
            return None

    sys.meta_path.insert(0, _Blocker())
    try:
        pconfig = MagicMock()
        pconfig.extra = {"ws_url": "ws://localhost:5225"}
        result = await _standalone_send(pconfig, "contact-42", "hi")
        assert isinstance(result, dict)
        assert "error" in result
        assert "websockets" in result["error"]
    finally:
        sys.meta_path[:] = saved_meta
        if saved_websockets is not None:
            sys.modules["websockets"] = saved_websockets


@pytest.mark.asyncio
async def test_standalone_send_dm_text_uses_numeric_id(monkeypatch):
    """DM text must go through ``/_send @<numeric-id> json``.

    Bare ``@<numeric-id>`` is rejected by simplex-chat v6.5.6.1 with
    contactNotFound (the @ command only accepts display names). The
    structured form addresses by numeric ID, which also covers static
    numeric targets (``SIMPLEX_HOME_CHANNEL=<contact-id>``).
    """
    sent = {}

    class FakeWS:
        def __init__(self):
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            self.closed = True
            return False

        async def send(self, payload):
            sent["payload"] = payload

        async def recv(self):
            return '{"resp": {"type": "newChatItems"}}'

    class FakeWSClient:
        @staticmethod
        def connect(uri, **kw):
            return FakeWS()

    monkeypatch.setattr(websockets, "connect", FakeWSClient.connect)
    pconfig = MagicMock()
    pconfig.extra = {"ws_url": "ws://localhost:5225"}
    result = await _standalone_send(pconfig, "5", "hello")
    assert result == {"success": True, "platform": "simplex", "chat_id": "5"}
    cmd = json.loads(sent["payload"])["cmd"]
    assert cmd.startswith("/_send @5 json ")
    msg_content = json.loads(cmd.split(" json ", 1)[1])[0]["msgContent"]
    assert msg_content == {"type": "text", "text": "hello"}


@pytest.mark.asyncio
async def test_standalone_send_dm_text_composite_chat_id(monkeypatch):
    """Composite ``id|displayName`` chat_id must resolve to the numeric ID."""
    sent = {}

    class FakeWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, payload):
            sent["payload"] = payload

        async def recv(self):
            return '{"resp": {"type": "newChatItems"}}'

    class FakeWSClient:
        @staticmethod
        def connect(uri, **kw):
            return FakeWS()

    monkeypatch.setattr(websockets, "connect", FakeWSClient.connect)
    pconfig = MagicMock()
    pconfig.extra = {"ws_url": "ws://localhost:5225"}
    result = await _standalone_send(pconfig, "5|人可通_1", "hello")
    assert result["success"] is True
    cmd = json.loads(sent["payload"])["cmd"]
    assert cmd.startswith("/_send @5 json ")


@pytest.mark.asyncio
async def test_standalone_send_media_unpacks_tuples(monkeypatch, tmp_path):
    """media_files entries are (path, is_voice) tuples — the path must be
    unpacked, not passed as a tuple (empty tuple-as-path would yield an
    empty thumbnail and a false-success send). Also waits for the daemon's
    newChatItems ack before closing (XFTP upload is async)."""
    img = tmp_path / "test.png"
    from PIL import Image

    Image.new("RGB", (64, 64), (200, 30, 30)).save(img)

    events = []

    class FakeWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, payload):
            events.append(("send", payload))

        async def recv(self):
            return '{"resp": {"type": "newChatItems"}}'

    class FakeWSClient:
        @staticmethod
        def connect(uri, **kw):
            return FakeWS()

    monkeypatch.setattr(websockets, "connect", FakeWSClient.connect)
    pconfig = MagicMock()
    pconfig.extra = {"ws_url": "ws://localhost:5225"}
    result = await _standalone_send(
        pconfig, "5", "", media_files=[(str(img), False)]
    )
    assert result["success"] is True
    send_events = [e for e in events if e[0] == "send"]
    assert len(send_events) == 1
    cmd = json.loads(send_events[0][1])["cmd"]
    assert cmd.startswith("/_send @5 json ")
    items = json.loads(cmd.split(" json ", 1)[1])
    assert items[0]["filePath"] == str(img)
    # Thumbnail must be a non-empty JPEG data URI (empty = false success).
    assert items[0]["msgContent"]["image"].startswith("data:image/jpg;base64,")
    assert len(items[0]["msgContent"]["image"]) > 100


@pytest.mark.asyncio
async def test_standalone_send_media_voice_branch(monkeypatch, tmp_path):
    """is_voice=True must build the voice-note payload (fileSource + type
    "voice"), not an image — mirrors live send_voice."""
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"OggS fake audio")

    events = []

    class FakeWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, payload):
            events.append(("send", payload))

        async def recv(self):
            return '{"resp": {"type": "newChatItems"}}'

    class FakeWSClient:
        @staticmethod
        def connect(uri, **kw):
            return FakeWS()

    monkeypatch.setattr(websockets, "connect", FakeWSClient.connect)
    pconfig = MagicMock()
    pconfig.extra = {"ws_url": "ws://localhost:5225"}
    result = await _standalone_send(
        pconfig, "5", "caption", media_files=[(str(audio), True)]
    )
    assert result["success"] is True
    cmd = json.loads(events[0][1])["cmd"]
    items = json.loads(cmd.split(" json ", 1)[1])
    assert items[0]["fileSource"]["filePath"] == str(audio)
    assert items[0]["msgContent"]["type"] == "voice"
    assert items[0]["msgContent"]["text"] == "caption"


@pytest.mark.asyncio
async def test_standalone_send_media_document_branch(monkeypatch, tmp_path):
    """Non-image, non-voice attachments must go as generic file (type
    "file") — mirrors live send_document, not an image payload."""
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF fake")

    events = []

    class FakeWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, payload):
            events.append(("send", payload))

        async def recv(self):
            return '{"resp": {"type": "newChatItems"}}'

    class FakeWSClient:
        @staticmethod
        def connect(uri, **kw):
            return FakeWS()

    monkeypatch.setattr(websockets, "connect", FakeWSClient.connect)
    pconfig = MagicMock()
    pconfig.extra = {"ws_url": "ws://localhost:5225"}
    result = await _standalone_send(
        pconfig, "5", "", media_files=[(str(doc), False)]
    )
    assert result["success"] is True
    cmd = json.loads(events[0][1])["cmd"]
    items = json.loads(cmd.split(" json ", 1)[1])
    assert items[0]["filePath"] == str(doc)
    assert items[0]["msgContent"]["type"] == "file"
    # No image/thumbnail key on the generic file payload.
    assert "image" not in items[0]["msgContent"]


@pytest.mark.asyncio
async def test_standalone_send_media_no_ack_returns_error(monkeypatch, tmp_path):
    """A media send whose newChatItems ack never arrives must NOT report
    success — the daemon never confirmed the transfer, so a false-success
    would mask a lost file (the exact bug class this PR fixes)."""
    import asyncio as _asyncio

    img = tmp_path / "test.png"
    from PIL import Image

    Image.new("RGB", (64, 64), (200, 30, 30)).save(img)

    class FakeWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, payload):
            pass

        async def recv(self):
            raise _asyncio.TimeoutError("no ack from daemon")

    class FakeWSClient:
        @staticmethod
        def connect(uri, **kw):
            return FakeWS()

    monkeypatch.setattr(websockets, "connect", FakeWSClient.connect)
    pconfig = MagicMock()
    pconfig.extra = {"ws_url": "ws://localhost:5225"}
    result = await _standalone_send(
        pconfig, "5", "", media_files=[(str(img), False)]
    )
    assert "success" not in result
    assert "error" in result
    assert "did not acknowledge" in result["error"]


# ---------------------------------------------------------------------------
# 10. register() — plugin-side metadata
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Inbound attachment message type classification
# ---------------------------------------------------------------------------

def _make_file_chat_item(file_path: str, file_name: str) -> dict:
    """Minimal direct-chat rcvMsgContent item carrying a completed file."""
    return {
        "chatInfo": {
            "type": "direct",
            "contact": {"contactId": 42, "localDisplayName": "tester"},
        },
        "chatItem": {
            "chatDir": {"type": "directRcv"},
            "meta": {"itemTs": "2026-01-01T00:00:00Z"},
            "content": {
                "type": "rcvMsgContent",
                "msgContent": {"type": "file", "text": "here you go"},
            },
            "file": {
                "fileId": 7,
                "fileName": file_name,
                "fileSource": {"filePath": file_path},
            },
        },
    }


