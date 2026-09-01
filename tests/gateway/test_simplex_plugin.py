"""Tests for the SimpleX Chat platform-plugin adapter.

Loaded via the ``_plugin_adapter_loader`` helper so this lives under
``plugin_adapter_simplex`` in ``sys.modules`` and cannot collide with
sibling platform-plugin tests on the same xdist worker.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

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


def _send_ack(item_id: int = 101) -> dict:
    return {
        "type": "newChatItems",
        "chatItems": [
            {
                "chatInfo": {"type": "direct"},
                "chatItem": {
                    "meta": {"itemId": item_id},
                    "chatDir": {"type": "directSnd"},
                },
            }
        ],
    }


async def _drain_dispatches(adapter) -> None:
    await asyncio.sleep(0)
    tasks = list(adapter._dispatch_tasks)
    if tasks:
        await asyncio.gather(*tasks)


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
async def test_send_dm():
    """DMs use the structured ``/_send @<id> json [...]`` form.

    The bare ``@<id> text`` chat-command form is unreliable — the
    daemon silently drops messages when it cannot resolve the display
    name.  The structured ``/_send`` form addresses by ID and
    survives newlines/quoting through ``json.dumps``, matching what
    ``send_image`` and ``send_document`` already do.
    """
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)

    adapter._send_command = AsyncMock(return_value=_send_ack())

    result = await adapter.send("contact-42", "Hello, SimpleX!")
    adapter._send_command.assert_awaited_once()
    command = adapter._send_command.await_args.args[0]
    assert command.startswith("/_send @contact-42 json ")
    msg_content = json.loads(command.split(" json ", 1)[1])[0][
        "msgContent"
    ]
    assert msg_content == {"type": "text", "text": "Hello, SimpleX!"}
    assert result.success is True
    assert result.message_id == "101"



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

    adapter._send_command = AsyncMock(return_value=_send_ack())

    result = await adapter.send("group:grp-99", "Hello, group!")
    command = adapter._send_command.await_args.args[0]
    assert command.startswith("/_send #grp-99 json ")
    msg_content = json.loads(command.split(" json ", 1)[1])[0][
        "msgContent"
    ]
    assert msg_content == {"type": "text", "text": "Hello, group!"}
    assert result.success is True


# ---------------------------------------------------------------------------
# 7b. Channel directory enumeration (list_channels)
# ---------------------------------------------------------------------------


def _adapter_with_ws():
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)
    adapter._ws = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_list_channels_contacts_and_groups():
    adapter = _adapter_with_ws()

    async def fake_send_command(command, timeout=30.0):
        if command == "/contacts":
            return {
                "contacts": [
                    {"contactId": 1, "localDisplayName": "alice"},
                    {"contactId": 2, "profile": {"displayName": "bob"}},
                    "garbage",
                ]
            }
        if command == "/groups":
            return {
                "groups": [
                    {"groupId": 7, "localDisplayName": "friends"},
                    # [groupInfo, groupSummary] pair form
                    [{"groupId": 9, "groupProfile": {"displayName": "work"}}, {}],
                ]
            }
        return None

    adapter._send_command = fake_send_command
    channels = await adapter.list_channels()

    assert {"id": "1", "name": "alice", "type": "dm"} in channels
    assert {"id": "2", "name": "bob", "type": "dm"} in channels
    assert {"id": "group:7", "name": "friends", "type": "group"} in channels
    assert {"id": "group:9", "name": "work", "type": "group"} in channels


@pytest.mark.asyncio
async def test_list_channels_returns_none_when_disconnected():
    """None (not []) so the directory falls back to session discovery."""
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)
    assert adapter._ws is None
    assert await adapter.list_channels() is None


@pytest.mark.asyncio
async def test_list_channels_returns_none_on_contacts_timeout():
    adapter = _adapter_with_ws()

    async def fake_send_command(command, timeout=30.0):
        return None  # daemon unresponsive

    adapter._send_command = fake_send_command
    assert await adapter.list_channels() is None


@pytest.mark.asyncio
async def test_send_short_content_single_send():
    """Content within the limit is delivered as exactly one WS send."""
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)

    adapter._send_command = AsyncMock(return_value=_send_ack())

    result = await adapter.send("contact-42", "short message")
    adapter._send_command.assert_awaited_once()
    assert result.success is True


@pytest.mark.asyncio
async def test_send_long_content_chunks_into_ordered_sends():
    """Content longer than the max is split into multiple ordered sends,
    each chunk staying within the advertised limit."""
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)

    counter = 0

    async def send_command(_command, timeout=30.0):
        nonlocal counter
        counter += 1
        return _send_ack(counter)

    adapter._send_command = AsyncMock(side_effect=send_command)

    max_len = _simplex.MAX_MESSAGE_LENGTH
    # Plain text (no code fences) longer than the limit -> several chunks.
    long_text = "word " * (max_len // 2)
    assert len(long_text) > max_len

    result = await adapter.send("contact-42", long_text)
    assert result.success is True
    assert adapter._send_command.await_count > 1

    total = adapter._send_command.await_count
    bodies = []
    for i, call in enumerate(adapter._send_command.await_args_list):
        command = call.args[0]
        # Every chunk is addressed to the same contact, in order.
        assert command.startswith("/_send @contact-42 json ")
        chunk = json.loads(command.split(" json ", 1)[1])[0][
            "msgContent"
        ]["text"]
        # Each chunk body must respect the per-message limit.
        assert len(chunk) <= max_len
        # Multi-part indicator appended by the base helper (whatever its exact
        # spacing): a trailing "(n/N)" marker must be present, in send order.
        assert re.search(r"\(\d+/\d+\)\s*$", chunk)
        assert re.search(rf"\({i + 1}/{total}\)\s*$", chunk)
        # Drop the trailing part-indicator to recover the original body text.
        bodies.append(re.sub(r"\s*\(\d+/\d+\)\s*$", "", chunk))

    # Content coverage: reassembling the indicator-stripped chunk bodies must
    # account for every word of the original message. truncate_message splits
    # on word boundaries and lstrips inter-chunk whitespace, so word-for-word
    # reassembly is reliable even if exact byte concatenation is not.
    reassembled = " ".join(bodies).split()
    assert reassembled == long_text.split()


@pytest.mark.asyncio
async def test_send_when_ws_not_connected_reports_failure():
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)
    # No _ws assigned — _send_ws should drop quietly
    result = await adapter.send("contact-42", "hi")
    assert result.success is False
    assert result.error is not None


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
async def test_standalone_send_defaults_to_local_daemon(monkeypatch):
    monkeypatch.delenv("SIMPLEX_WS_URL", raising=False)
    pconfig = MagicMock()
    pconfig.extra = {}

    sent_payloads = []

    class DummyWs:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def send(self, payload):
            sent_payloads.append(json.loads(payload))

        async def recv(self):
            corr_id = sent_payloads[-1]["corrId"]
            return json.dumps({"corrId": corr_id, "resp": _send_ack()})

    def fake_connect(url, **kwargs):
        assert url == "ws://127.0.0.1:5225"
        assert kwargs["open_timeout"] == 10
        assert kwargs["close_timeout"] == 5
        return DummyWs()

    import websockets
    monkeypatch.setattr(websockets, "connect", fake_connect)

    result = await _standalone_send(pconfig, "contact-42", "hi")
    assert result == {
        "success": True,
        "platform": "simplex",
        "chat_id": "contact-42",
        "message_id": "101",
    }
    assert sent_payloads[0]["cmd"].startswith("/_send @contact-42 json ")
    msg_content = json.loads(
        sent_payloads[0]["cmd"].split(" json ", 1)[1]
    )[0]["msgContent"]
    assert msg_content == {"type": "text", "text": "hi"}


@pytest.mark.asyncio
async def test_standalone_send_removes_owned_image_conversion(monkeypatch, tmp_path):
    source = tmp_path / "source.webp"
    source.write_bytes(b"RIFFxxxxWEBP")
    converted = tmp_path / "converted.png"
    converted.write_bytes(b"\x89PNG")
    monkeypatch.setattr(
        SimplexAdapter,
        "_prepare_image",
        staticmethod(lambda _path: (str(converted), "data:image/jpg;base64,")),
    )

    sent_payloads = []

    class DummyWs:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def send(self, payload):
            sent_payloads.append(json.loads(payload))

        async def recv(self):
            return json.dumps(
                {"corrId": sent_payloads[-1]["corrId"], "resp": _send_ack(91)}
            )

    import websockets

    monkeypatch.setattr(websockets, "connect", lambda *_args, **_kwargs: DummyWs())
    pconfig = MagicMock()
    pconfig.extra = {"ws_url": "ws://localhost:5225"}

    result = await _standalone_send(
        pconfig,
        "42",
        "",
        media_files=[str(source)],
    )

    assert result["success"] is True
    assert not converted.exists()


@pytest.mark.asyncio
async def test_health_monitor_does_not_reconnect_quiet_healthy_ws(monkeypatch):
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)
    adapter._running = True
    adapter._last_ws_activity = 0
    adapter._ws = AsyncMock()

    monkeypatch.setattr(_simplex, "HEALTH_CHECK_INTERVAL", 0.01)
    monkeypatch.setattr(_simplex, "HEALTH_CHECK_STALE_THRESHOLD", 0.01)

    task = asyncio.create_task(adapter._health_monitor())
    await asyncio.sleep(0.03)
    adapter._running = False
    await asyncio.wait_for(task, timeout=1)

    adapter._ws.close.assert_not_called()




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


@pytest.mark.asyncio
async def test_document_file_sets_document_type():
    """A non-image/non-audio file must classify as DOCUMENT, not TEXT,
    so run.py's document-context injection surfaces the path to the agent."""
    from gateway.config import PlatformConfig
    from gateway.platforms.base import MessageType

    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)
    dispatched = []

    async def _capture(event):
        dispatched.append(event)

    adapter.handle_message = _capture
    await adapter._handle_chat_item(_make_file_chat_item("/tmp/report.pdf", "report.pdf"))
    await _drain_dispatches(adapter)

    assert dispatched, "_handle_chat_item did not dispatch any event"
    assert dispatched[0].message_type == MessageType.DOCUMENT
    assert dispatched[0].media_urls == ["/tmp/report.pdf"]
    assert dispatched[0].media_types == ["application/octet-stream"]


@pytest.mark.asyncio
async def test_image_file_still_sets_photo_type():
    """Regression guard: image files keep classifying as PHOTO after the
    document catch-all was added."""
    from gateway.config import PlatformConfig
    from gateway.platforms.base import MessageType

    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)
    dispatched = []

    async def _capture(event):
        dispatched.append(event)

    adapter.handle_message = _capture
    await adapter._handle_chat_item(_make_file_chat_item("/tmp/pic.jpg", "pic.jpg"))
    await _drain_dispatches(adapter)

    assert dispatched, "_handle_chat_item did not dispatch any event"
    assert dispatched[0].message_type == MessageType.PHOTO


# ---------------------------------------------------------------------------
# Nested AChatItem normalization (singular newChatItem events)
# ---------------------------------------------------------------------------

def _make_direct_text_wrapper(text: str = "hello") -> dict:
    """Minimal normalized AChatItem wrapper for a received direct text."""
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
                "msgContent": {"type": "text", "text": text},
            },
        },
    }


def test_normalize_unwraps_nested_achatitem():
    """{type: newChatItem, chatItem: {chatInfo, chatItem}} → inner wrapper."""
    inner = _make_direct_text_wrapper()
    payload = {"type": "newChatItem", "chatItem": inner}
    assert SimplexAdapter._normalize_chat_item_wrapper(payload) is inner


def test_normalize_passes_through_normalized_wrapper():
    """An already-normalized {chatInfo, chatItem} wrapper is returned as-is."""
    wrapper = _make_direct_text_wrapper()
    assert SimplexAdapter._normalize_chat_item_wrapper(wrapper) is wrapper


def test_normalize_maps_item_key_variant():
    """{chatInfo, item} responses normalize to {chatInfo, chatItem}."""
    wrapper = _make_direct_text_wrapper()
    payload = {"chatInfo": wrapper["chatInfo"], "item": wrapper["chatItem"]}
    normalized = SimplexAdapter._normalize_chat_item_wrapper(payload)
    assert normalized["chatInfo"] is wrapper["chatInfo"]
    assert normalized["chatItem"] is wrapper["chatItem"]


def test_normalize_tolerates_non_dict():
    assert SimplexAdapter._normalize_chat_item_wrapper(None) == {}
    assert SimplexAdapter._normalize_chat_item_wrapper("junk") == {}


@pytest.mark.asyncio
async def test_handle_event_singular_new_chat_item_nested():
    """A wrapped singular newChatItem event with the AChatItem nested one
    level down must reach message handling instead of being silently
    dropped because chatInfo isn't at the top level."""
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)

    event = {
        "corrId": "",
        "resp": {"type": "newChatItem", "chatItem": _make_direct_text_wrapper("hi there")},
    }
    await adapter._handle_event(event)

    # Text messages are buffered by the batching layer before dispatch —
    # the pending batch proves the item survived normalization.
    batches = list(adapter._pending_text_batches.values())
    assert batches, "nested newChatItem was dropped before dispatch"
    assert batches[0].text == "hi there"
    assert batches[0].source.chat_id == "42"
    # Cancel the flush timer so no task leaks out of the test.
    for task in adapter._pending_text_batch_tasks.values():
        task.cancel()


@pytest.mark.asyncio
async def test_handle_event_new_chat_items_nested_elements():
    """newChatItems arrays whose elements nest the AChatItem one level down
    are normalized element-by-element."""
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)

    event = {
        "corrId": "",
        "resp": {
            "type": "newChatItems",
            "chatItems": [
                {"chatItem": _make_direct_text_wrapper("first")},
                _make_direct_text_wrapper("second"),  # already-normalized mix
            ],
        },
    }
    await adapter._handle_event(event)

    batches = list(adapter._pending_text_batches.values())
    assert batches, "nested newChatItems elements were dropped"
    # Same chat → batched into one pending event, in arrival order.
    assert batches[0].text == "first\nsecond"
    for task in adapter._pending_text_batch_tasks.values():
        task.cancel()


# ---------------------------------------------------------------------------
# Correlated commands, lifecycle, transfers, edits, and reactions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_command_correlates_response_and_cleans_state():
    from gateway.config import PlatformConfig

    class RecordingWs:
        def __init__(self):
            self.payloads = []

        async def send(self, payload):
            self.payloads.append(json.loads(payload))

    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    ws = RecordingWs()
    adapter._ws = ws
    adapter._ws_ready.set()

    pending = asyncio.create_task(adapter._send_command("/contacts", timeout=1))
    await asyncio.sleep(0)
    corr_id = ws.payloads[0]["corrId"]
    response = {"type": "contacts", "contacts": []}
    await adapter._handle_event({"corrId": corr_id, "resp": response})

    assert await pending == response
    assert corr_id not in adapter._pending_responses
    assert corr_id not in adapter._pending_corr_ids


@pytest.mark.asyncio
async def test_large_message_retry_preserves_reply_on_first_retry_only():
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    calls = []

    async def reply(command, timeout=30.0):
        calls.append(command)
        if len(calls) == 1:
            return {
                "type": "chatCmdError",
                "chatError": {"type": "error", "errorType": "largeMsg"},
            }
        return _send_ack(100 + len(calls))

    adapter._send_command = AsyncMock(side_effect=reply)
    text = "x" * 1200
    result = await adapter.send("42", text, reply_to="77")

    assert result.success is True
    assert len(calls) >= 3
    retry_payloads = [json.loads(command.split(" json ", 1)[1])[0] for command in calls]
    assert retry_payloads[0]["quotedItemId"] == 77
    assert retry_payloads[1]["quotedItemId"] == 77
    assert all("quotedItemId" not in item for item in retry_payloads[2:])
    retried_text = "".join(
        re.sub(r" \(\d+/\d+\)$", "", item["msgContent"]["text"])
        for item in retry_payloads[1:]
    )
    assert retried_text == text


@pytest.mark.asyncio
async def test_contact_request_acceptance_uses_protocol_command():
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    adapter._send_command = AsyncMock(return_value={"type": "contactRequestAccepted"})

    await adapter._handle_event(
        {
            "resp": {
                "type": "receivedContactRequest",
                "contactRequest": {"contactRequestId": 19},
            }
        }
    )
    await asyncio.gather(*adapter._command_tasks)

    adapter._send_command.assert_awaited_once_with("/_accept 19", timeout=30.0)


@pytest.mark.asyncio
async def test_inbound_edit_keeps_item_id_and_bypasses_text_batch():
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture
    wrapper = _make_direct_text_wrapper("corrected")
    wrapper["chatItem"]["meta"]["itemId"] = 505
    await adapter._handle_event(
        {"resp": {"type": "chatItemUpdated", "chatItem": wrapper}}
    )
    await _drain_dispatches(adapter)

    assert len(captured) == 1
    assert captured[0].message_id == "505"
    assert captured[0].source.message_id == "505"
    assert captured[0].metadata == {"is_edit": True}
    assert adapter._pending_text_batches == {}


def test_group_text_batches_are_scoped_by_sender():
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    first = MagicMock()
    first.source.platform.value = "simplex"
    first.source.chat_id = "group:7"
    first.source.user_id = "member-1"
    second = MagicMock()
    second.source.platform.value = "simplex"
    second.source.chat_id = "group:7"
    second.source.user_id = "member-2"

    assert adapter._text_batch_key(first) != adapter._text_batch_key(second)


@pytest.mark.asyncio
async def test_file_descriptor_acceptance_and_completion_dispatch(tmp_path):
    from gateway.config import PlatformConfig
    from gateway.platforms.base import MessageType

    receive_dir = tmp_path / "incoming"
    receive_dir.mkdir()
    adapter = SimplexAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "ws_url": "ws://localhost:5225",
                "files_folder": str(receive_dir),
            },
        )
    )
    adapter._receive_file = AsyncMock()
    adapter.set_authorization_check(lambda user_id, chat_type, chat_id: True)
    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture
    wrapper = _make_file_chat_item("", "../../report.pdf")
    wrapper["chatItem"]["file"].pop("fileSource")
    wrapper["chatItem"]["file"]["fileStatus"] = {"type": "rcvInvitation"}

    await adapter._handle_event(
        {
            "resp": {
                "type": "rcvFileDescrReady",
                "rcvFileTransfer": {"fileId": 7, "fileName": "../../report.pdf"},
                "chatItem": wrapper,
            }
        }
    )
    await _drain_dispatches(adapter)
    await asyncio.gather(*adapter._command_tasks)
    target = Path(adapter._receive_file.await_args.args[1])
    assert adapter._receive_file.await_args.args[0] == 7
    assert target.parent == receive_dir
    assert ".." not in target.name

    completed = receive_dir / "report.pdf"
    completed.write_bytes(b"%PDF-1.7\n")
    await adapter._handle_event(
        {
            "resp": {
                "type": "rcvFileComplete",
                "chatItem": {
                    "chatItem": {
                        "file": {
                            "fileId": 7,
                            "fileSource": {"filePath": "report.pdf"},
                        }
                    }
                },
            }
        }
    )
    await _drain_dispatches(adapter)

    assert len(captured) == 1
    assert captured[0].message_type == MessageType.DOCUMENT
    assert captured[0].media_urls == [str(completed)]
    assert adapter._pending_file_transfers == {}


@pytest.mark.asyncio
async def test_receive_file_enables_approved_relays():
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    adapter._send_command = AsyncMock(return_value={"type": "rcvFileAccepted"})

    await adapter._receive_file(9, "/tmp/safe-name.pdf")

    adapter._send_command.assert_awaited_once_with(
        "/freceive 9 approved_relays=on /tmp/safe-name.pdf",
        timeout=30.0,
    )


@pytest.mark.asyncio
async def test_media_send_uses_structured_file_source_and_reply(tmp_path):
    from gateway.config import PlatformConfig

    document = tmp_path / "evidence.txt"
    document.write_text("proof")
    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    adapter._send_command = AsyncMock(return_value=_send_ack(601))

    result = await adapter.send_document(
        "42", str(document), caption="attached", reply_to="55"
    )
    command = adapter._send_command.await_args.args[0]
    composed = json.loads(command.split(" json ", 1)[1])[0]

    assert result.success is True
    assert result.message_id == "601"
    assert composed["fileSource"] == {"filePath": str(document)}
    assert composed["quotedItemId"] == 55
    assert composed["msgContent"] == {"type": "file", "text": "attached"}


@pytest.mark.asyncio
async def test_streaming_edit_and_final_overflow_lifecycle():
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    adapter._send_command = AsyncMock(return_value={"type": "chatItemUpdated"})

    interim = await adapter.edit_message("42", "700", "working", finalize=False)
    assert interim.success is True
    assert " 700 live=on json " in adapter._send_command.await_args.args[0]

    adapter._send_composed = AsyncMock(
        side_effect=[
            _simplex.SendResult(success=True, message_id="701"),
            _simplex.SendResult(success=True, message_id="702"),
        ]
    )
    final_text = "a" * (_simplex.MAX_MESSAGE_LENGTH * 2 + 100)
    final = await adapter.edit_message("42", "700", final_text, finalize=True)

    final_command = adapter._send_command.await_args.args[0]
    assert " live=on" not in final_command
    assert adapter._send_composed.await_count == 2
    assert final.message_id == "702"
    assert final.continuation_message_ids == ("701", "702")


@pytest.mark.asyncio
async def test_partial_stream_overflow_reports_exact_delivered_source_prefix():
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    adapter._send_command = AsyncMock(return_value={"type": "chatItemUpdated"})
    adapter._send_composed = AsyncMock(
        side_effect=[
            _simplex.SendResult(success=True, message_id="701"),
            _simplex.SendResult(
                success=False,
                error="not submitted",
                error_kind="transient",
                retryable=True,
            ),
        ]
    )
    content = "word " * (_simplex.MAX_MESSAGE_LENGTH // 2)

    result = await adapter.edit_message("42", "700", content, finalize=True)

    assert result.success is False
    assert result.raw_response["partial_overflow"] is True
    delivered_prefix = result.raw_response["delivered_prefix"]
    assert delivered_prefix
    assert content.startswith(delivered_prefix)
    assert len(delivered_prefix) < len(content)


@pytest.mark.asyncio
async def test_delete_and_error_diagnostics_are_truthful():
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    adapter._send_command = AsyncMock(
        return_value={
            "type": "chatCmdError",
            "chatError": {"type": "error", "errorType": "notFound"},
        }
    )

    assert await adapter.delete_message("42", "99") is False
    diagnostics = adapter.get_runtime_diagnostics()
    assert diagnostics["command_errors"] == 1
    assert diagnostics["ready"] is False


@pytest.mark.asyncio
async def test_reaction_hook_and_direct_approval_resolution(monkeypatch):
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    adapter.send = AsyncMock(
        return_value=_simplex.SendResult(success=True, message_id="800")
    )
    spawned = []

    def close_background(coroutine):
        spawned.append(coroutine)
        coroutine.close()
        return MagicMock()

    adapter._spawn_command_task = close_background
    hook_events = []

    async def reaction_hook(event):
        hook_events.append(event)

    adapter._reaction_handler = reaction_hook
    import tools.approval as approval

    resolved = []
    monkeypatch.setattr(
        approval,
        "resolve_gateway_approval",
        lambda session_key, choice: resolved.append((session_key, choice)) or 1,
    )

    await adapter.send_exec_approval("42", "rm harmless", "session-1")
    reaction = {
        "type": "chatItemReaction",
        "added": True,
        "reaction": {
            "chatInfo": {
                "type": "direct",
                "contact": {"contactId": 42, "localDisplayName": "operator"},
            },
            "chatReaction": {
                "chatDir": {"type": "directRcv"},
                "chatItem": {"meta": {"itemId": 800}},
                "reaction": {"type": "emoji", "emoji": "✅"},
            },
        },
    }
    await adapter._handle_event({"resp": reaction})
    if adapter._dispatch_tasks:
        await asyncio.gather(*list(adapter._dispatch_tasks))

    assert resolved == [("session-1", "once")]
    assert hook_events[0]["event_name"] == "reaction:added"
    assert hook_events[0]["item_id"] == "800"
    assert adapter._approval_prompts_by_item == {}


@pytest.mark.asyncio
async def test_connect_waits_for_real_socket_and_disconnect_clears_state(monkeypatch):
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(
            enabled=True,
            extra={"ws_url": "ws://localhost:5225", "connect_timeout": 0.5},
        )
    )
    listener_stop = asyncio.Event()

    async def fake_listener():
        adapter._ws = AsyncMock()
        adapter._ws_ready.set()
        await listener_stop.wait()

    monkeypatch.setattr(adapter, "_ws_listener", fake_listener)
    assert await adapter.connect() is True
    assert adapter.get_runtime_diagnostics()["ready"] is True

    pending = asyncio.get_running_loop().create_future()
    adapter._pending_responses["corr"] = pending
    adapter._approval_prompts_by_item["1"] = {"session_key": "s"}
    await adapter.disconnect()

    assert isinstance(pending.exception(), ConnectionError)
    assert adapter.get_runtime_diagnostics()["ready"] is False
    assert adapter._approval_prompts_by_item == {}


def test_prepare_image_uses_temp_output_without_overwriting_peer_file(tmp_path):
    from PIL import Image

    source = tmp_path / "picture.webp"
    existing = tmp_path / "picture.png"
    Image.new("RGB", (4, 4), color="red").save(source, "WEBP")
    existing.write_bytes(b"preserve-me")

    converted, thumbnail = SimplexAdapter._prepare_image(str(source))
    try:
        assert Path(converted) != existing
        assert Path(converted).is_file()
        assert existing.read_bytes() == b"preserve-me"
        assert thumbnail.startswith("data:image/jpg;base64,")
    finally:
        Path(converted).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_reader_remains_available_when_media_handler_sends_reply():
    """A media callback may send a busy ack; its corrId must reach the reader."""
    from gateway.config import PlatformConfig

    class RecordingWs:
        def __init__(self):
            self.payloads = []

        async def send(self, payload):
            self.payloads.append(json.loads(payload))

    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    ws = RecordingWs()
    adapter._ws = ws
    adapter._ws_ready.set()
    replies = []

    async def callback(_event):
        replies.append(await adapter.send("42", "busy acknowledgement"))

    adapter.handle_message = callback
    await adapter._handle_event(
        {"resp": {"type": "newChatItems", "chatItems": [_make_file_chat_item("/tmp/pic.jpg", "pic.jpg")]}}
    )
    for _ in range(20):
        if ws.payloads:
            break
        await asyncio.sleep(0)
    assert ws.payloads, "message callback never submitted its reply command"

    corr_id = ws.payloads[0]["corrId"]
    await adapter._handle_event({"corrId": corr_id, "resp": _send_ack(901)})
    await _drain_dispatches(adapter)

    assert replies[0].success is True
    assert replies[0].message_id == "901"


@pytest.mark.asyncio
async def test_file_download_requires_explicit_authorization(tmp_path):
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(
            enabled=True,
            extra={"ws_url": "ws://localhost:5225", "files_folder": str(tmp_path)},
        )
    )
    adapter._receive_file = AsyncMock()
    wrapper = _make_file_chat_item("", "untrusted.bin")
    wrapper["chatItem"]["file"].pop("fileSource")

    await adapter._handle_event(
        {
            "resp": {
                "type": "rcvFileDescrReady",
                "rcvFileTransfer": {"fileId": 7, "fileName": "untrusted.bin"},
                "chatItem": wrapper,
            }
        }
    )

    adapter._receive_file.assert_not_awaited()
    assert adapter.get_runtime_diagnostics()["file_rejections"] == 1
    assert adapter._pending_file_transfers == {}


@pytest.mark.asyncio
async def test_stalled_file_expires_and_preserves_caption():
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "ws_url": "ws://localhost:5225",
                "file_transfer_timeout": 0.01,
            },
        )
    )
    adapter.set_authorization_check(lambda user_id, chat_type, chat_id: True)
    adapter._file_transfer_timeout = 0.01
    adapter._text_batch_delay = 0
    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture
    wrapper = _make_file_chat_item("", "report.pdf")
    wrapper["chatItem"]["file"].pop("fileSource")
    wrapper["chatItem"]["file"]["fileStatus"] = {"type": "rcvInvitation"}
    await adapter._handle_chat_item(wrapper)
    await asyncio.sleep(0.03)
    if adapter._pending_text_batch_tasks:
        await asyncio.gather(*list(adapter._pending_text_batch_tasks.values()))

    assert [event.text for event in captured] == ["here you go"]
    assert captured[0].media_urls == []
    assert adapter.get_runtime_diagnostics()["file_timeouts"] == 1
    assert adapter._pending_file_transfers == {}


@pytest.mark.asyncio
async def test_cancelled_file_releases_state_and_preserves_caption():
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "ws_url": "ws://localhost:5225",
                "file_transfer_timeout": 10,
            },
        )
    )
    adapter.set_authorization_check(lambda user_id, chat_type, chat_id: True)
    adapter._text_batch_delay = 0
    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture
    wrapper = _make_file_chat_item("", "report.pdf")
    wrapper["chatItem"]["file"].pop("fileSource")
    wrapper["chatItem"]["file"]["fileStatus"] = {"type": "rcvInvitation"}
    await adapter._handle_chat_item(wrapper)
    await adapter._handle_event(
        {
            "resp": {
                "type": "rcvFileSndCancelled",
                "chatItem": wrapper,
                "rcvFileTransfer": {"fileId": 7},
            }
        }
    )
    if adapter._pending_text_batch_tasks:
        await asyncio.gather(*list(adapter._pending_text_batch_tasks.values()))

    assert [event.text for event in captured] == ["here you go"]
    assert adapter.get_runtime_diagnostics()["file_failures"] == 1
    assert adapter._file_transfer_tasks == {}


def test_non_numeric_allowlist_entry_emits_migration_warning(monkeypatch, caplog):
    from gateway.config import PlatformConfig

    monkeypatch.setenv("SIMPLEX_ALLOWED_USERS", "4,mutable-name")
    with caplog.at_level("WARNING"):
        SimplexAdapter(
            PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
        )

    assert "non-numeric SIMPLEX_ALLOWED_USERS" in caplog.text
    assert "mutable-name" not in caplog.text


def test_outgoing_reaction_event_is_not_an_approval_input():
    response = {
        "type": "chatItemReaction",
        "added": True,
        "reaction": {
            "chatInfo": {"type": "direct", "contact": {"contactId": 42}},
            "chatReaction": {
                "chatDir": {"type": "directSnd"},
                "chatItem": {"meta": {"itemId": 800}},
                "reaction": {"type": "emoji", "emoji": "✅"},
            },
        },
    }
    assert SimplexAdapter._reaction_context(response) is None


@pytest.mark.asyncio
async def test_partial_delivery_is_never_retried_or_fallback_duplicated():
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    responses = [
        _send_ack(1),
        {
            "type": "localCommandOutcomeUnknown",
            "error": "confirmation timed out; delivery may have occurred",
        },
    ]
    adapter._send_command = AsyncMock(side_effect=responses)
    content = "z" * (_simplex.MAX_MESSAGE_LENGTH + 100)

    result = await adapter._send_with_retry("42", content, max_retries=3)

    assert result.success is False
    assert result.error_kind == "partial_delivery"
    assert result.retryable is False
    assert adapter._send_command.await_count == 2


@pytest.mark.asyncio
async def test_listener_reconnects_after_clean_socket_end(monkeypatch):
    """A dropped socket must not clear the adapter's reconnect run flag."""
    from gateway.config import PlatformConfig
    import websockets

    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    adapter._running = True
    adapter._write_runtime_status_safe = MagicMock()
    second_connected = asyncio.Event()
    attempts = 0

    class FakeSocket:
        def __init__(self, *, hold: bool):
            self.hold = hold

        async def __aenter__(self):
            if self.hold:
                second_connected.set()
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def __aiter__(self):
            async def messages():
                if self.hold:
                    while adapter._running:
                        await asyncio.sleep(0.01)
                if False:
                    yield ""

            return messages()

    def fake_connect(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return FakeSocket(hold=attempts > 1)

    monkeypatch.setattr(websockets, "connect", fake_connect)
    monkeypatch.setattr(_simplex, "WS_RETRY_DELAY_INITIAL", 0.0)
    task = asyncio.create_task(adapter._ws_listener())
    await asyncio.wait_for(second_connected.wait(), timeout=1)

    assert attempts >= 2
    assert adapter._running is True
    assert adapter.get_runtime_diagnostics()["reconnects"] >= 1

    adapter._running = False
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_pre_submit_failure_is_retryable_but_unknown_outcome_is_not():
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    adapter._ws_ready.set()

    class FlakySocket:
        def __init__(self):
            self.calls = 0

        async def send(self, payload):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("not submitted")
            corr_id = json.loads(payload)["corrId"]
            asyncio.create_task(
                adapter._handle_event({"corrId": corr_id, "resp": _send_ack(44)})
            )

    socket = FlakySocket()
    adapter._ws = socket
    delivered = await adapter._send_with_retry(
        "42", "retry once", max_retries=1, base_delay=0
    )
    assert delivered.success is True
    assert socket.calls == 2
    assert adapter._pending_responses == {}

    adapter._send_command = AsyncMock(
        return_value={
            "type": "localCommandOutcomeUnknown",
            "error": "submitted but confirmation was lost",
        }
    )
    unknown = await adapter._send_with_retry("42", "do not duplicate", max_retries=3)
    assert unknown.error_kind == "delivery_unknown"
    assert adapter._send_command.await_count == 1


def test_utf8_payload_split_respects_serialized_byte_budget():
    from gateway.config import PlatformConfig

    text = "🙂" * 4000
    chunks = SimplexAdapter._split_utf8_payload(text)
    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )

    assert len(chunks) > 1
    assert "".join(chunks) == text
    assert all(
        len(json.dumps(chunk, ensure_ascii=False).encode("utf-8")) - 2 <= 12000
        for chunk in chunks
    )
    assert adapter.message_len_fn(text) > adapter.MAX_MESSAGE_LENGTH
    assert len(
        adapter.truncate_message(
            text,
            adapter.MAX_MESSAGE_LENGTH,
            len_fn=adapter.message_len_fn,
        )
    ) > 1


@pytest.mark.asyncio
async def test_initial_stream_preview_uses_simplex_live_item():
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    adapter._send_command = AsyncMock(return_value=_send_ack(77))

    await adapter.send("42", "draft", metadata={"expect_edits": True})
    assert " live=on json " in adapter._send_command.await_args.args[0]

    await adapter.send("42", "final", metadata={"notify": True})
    assert " live=on json " not in adapter._send_command.await_args.args[0]


@pytest.mark.asyncio
async def test_group_sender_prefers_member_contact_id():
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(
            enabled=True,
            extra={"ws_url": "ws://localhost:5225", "group_allowed": "*"},
        )
    )
    adapter.group_allow_from = {"*"}
    adapter.set_authorization_check(lambda *_args: True)
    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture
    wrapper = _make_file_chat_item("/tmp/report.pdf", "report.pdf")
    wrapper["chatInfo"] = {
        "type": "group",
        "groupInfo": {"groupId": 9, "localDisplayName": "review"},
    }
    wrapper["chatItem"]["chatDir"] = {
        "type": "groupRcv",
        "groupMember": {
            "memberId": "opaque-member",
            "memberContactId": 42,
            "localDisplayName": "operator",
        },
    }

    await adapter._handle_chat_item(wrapper)
    await _drain_dispatches(adapter)

    assert captured[0].source.user_id == "42"
    assert adapter._file_sender_context(wrapper)[0] == "42"


@pytest.mark.asyncio
async def test_late_file_completion_is_ignored_and_owned_target_removed(tmp_path):
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "ws_url": "ws://localhost:5225",
                "files_folder": str(tmp_path),
                "file_transfer_timeout": 0.01,
            },
        )
    )
    adapter.set_authorization_check(lambda *_args: True)
    adapter._file_transfer_timeout = 0.01
    adapter._text_batch_delay = 0
    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture
    wrapper = _make_file_chat_item("", "late.pdf")
    wrapper["chatItem"]["file"].pop("fileSource")
    wrapper["chatItem"]["file"]["fileStatus"] = {"type": "rcvInvitation"}
    target = tmp_path / "simplex-rcv-owned-late.pdf"
    target.write_bytes(b"%PDF")
    adapter._file_receive_targets[7] = str(target)

    await adapter._handle_chat_item(wrapper)
    await asyncio.sleep(0.03)
    if adapter._pending_text_batch_tasks:
        await asyncio.gather(*list(adapter._pending_text_batch_tasks.values()))
    before = len(captured)

    complete = _make_file_chat_item(str(target), "late.pdf")
    await adapter._handle_event(
        {"resp": {"type": "rcvFileComplete", "chatItem": complete}}
    )
    await _drain_dispatches(adapter)

    assert len(captured) == before
    assert not target.exists()
    assert adapter.get_runtime_diagnostics()["late_file_completions"] == 1


@pytest.mark.asyncio
async def test_outbound_converted_image_removed_after_send_completion(tmp_path):
    from gateway.config import PlatformConfig

    source = tmp_path / "source.webp"
    source.write_bytes(b"RIFFxxxxWEBP")
    converted = tmp_path / "converted.png"
    converted.write_bytes(b"\x89PNG")
    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    adapter._prepare_image = MagicMock(return_value=(str(converted), "data:image/jpg;base64,"))
    adapter._send_composed = AsyncMock(
        return_value=_simplex.SendResult(success=True, message_id="88")
    )

    result = await adapter.send_image("42", f"file://{source}")
    assert result.success is True
    assert converted.exists()

    await adapter._handle_event(
        {
            "resp": {
                "type": "sndFileCompleteXFTP",
                "chatItem": {
                    "chatInfo": {"type": "direct"},
                    "chatItem": {"meta": {"itemId": 88}},
                },
            }
        }
    )
    assert not converted.exists()


@pytest.mark.asyncio
async def test_successful_owned_receive_attaches_post_turn_cleanup(tmp_path):
    from gateway.config import PlatformConfig

    target = tmp_path / "simplex-rcv-owned.pdf"
    target.write_bytes(b"%PDF")
    adapter = SimplexAdapter(
        PlatformConfig(
            enabled=True,
            extra={"ws_url": "ws://localhost:5225", "files_folder": str(tmp_path)},
        )
    )
    adapter.set_authorization_check(lambda *_args: True)
    adapter._file_receive_targets[7] = str(target)
    pending = _make_file_chat_item("", "owned.pdf")
    pending["chatItem"]["file"].pop("fileSource")
    adapter._pending_file_transfers[7] = pending
    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture
    complete = _make_file_chat_item(str(target), "owned.pdf")
    await adapter._handle_event(
        {"resp": {"type": "rcvFileComplete", "chatItem": complete}}
    )
    await _drain_dispatches(adapter)

    assert target.exists()
    callbacks = captured[0]._post_turn_cleanup_callbacks
    assert len(callbacks) == 1
    callbacks[0]()
    assert not target.exists()


@pytest.mark.asyncio
async def test_retained_receive_survives_ttl_and_disconnect(tmp_path):
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "ws_url": "ws://localhost:5225",
                "files_folder": str(tmp_path),
                "retain_received_files": True,
            },
        )
    )
    adapter._media_cleanup_timeout = 0.01
    adapter.set_authorization_check(lambda *_args: True)
    adapter._receive_file = AsyncMock()
    pending = _make_file_chat_item("", "retained.pdf")
    pending["chatItem"]["file"].pop("fileSource")
    pending["chatItem"]["file"]["fileStatus"] = {"type": "rcvInvitation"}
    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture
    await adapter._handle_event(
        {
            "resp": {
                "type": "rcvFileDescrReady",
                "rcvFileTransfer": {"fileId": 7, "fileName": "retained.pdf"},
                "chatItem": pending,
            }
        }
    )
    await asyncio.gather(*list(adapter._command_tasks))
    target = Path(adapter._receive_file.await_args.args[1])
    target.write_bytes(b"%PDF")
    complete = _make_file_chat_item(str(target), "retained.pdf")
    await adapter._handle_event(
        {"resp": {"type": "rcvFileComplete", "chatItem": complete}}
    )
    await _drain_dispatches(adapter)
    await asyncio.sleep(0.03)

    assert not hasattr(captured[0], "_post_turn_cleanup_callbacks")
    assert adapter._owned_media_cleanup_tasks == {}
    assert target.exists()

    await adapter.disconnect()
    assert target.exists()


@pytest.mark.asyncio
async def test_duplicate_success_completion_does_not_delete_active_media(tmp_path):
    from gateway.config import PlatformConfig

    target = tmp_path / "simplex-rcv-active.pdf"
    target.write_bytes(b"%PDF")
    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    adapter.set_authorization_check(lambda *_args: True)
    adapter._file_receive_targets[7] = str(target)
    pending = _make_file_chat_item("", "active.pdf")
    pending["chatItem"]["file"].pop("fileSource")
    adapter._pending_file_transfers[7] = pending
    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture
    complete = _make_file_chat_item(str(target), "active.pdf")
    event = {"resp": {"type": "rcvFileComplete", "chatItem": complete}}
    await adapter._handle_event(event)
    await _drain_dispatches(adapter)
    await adapter._handle_event(event)
    await _drain_dispatches(adapter)

    assert len(captured) == 1
    assert target.exists()
    assert adapter.get_runtime_diagnostics()["late_file_completions"] == 1

    captured[0]._post_turn_cleanup_callbacks[0]()


@pytest.mark.asyncio
async def test_captionless_failed_attachment_is_not_silent():
    from gateway.config import PlatformConfig

    adapter = SimplexAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    )
    adapter.set_authorization_check(lambda *_args: True)
    adapter._text_batch_delay = 0
    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture
    wrapper = _make_file_chat_item("", "missing.pdf")
    wrapper["chatItem"]["file"].pop("fileSource")
    wrapper["chatItem"]["content"]["msgContent"]["text"] = ""
    adapter._pending_file_transfers[7] = wrapper

    await adapter._fail_file_transfer(7, "sender cancelled")
    if adapter._pending_text_batch_tasks:
        await asyncio.gather(*list(adapter._pending_text_batch_tasks.values()))

    assert captured[0].text == "[Attachment unavailable: sender cancelled]"
