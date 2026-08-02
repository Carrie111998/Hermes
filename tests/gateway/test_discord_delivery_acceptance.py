"""Discord delivery path: id validation, name resolution, acceptance.

Contract: docs/specs/discord-delivery-acceptance.md (matrix 6a-6d) and
docs/specs/discord-delivery-acceptance-contract.md (C-04..C-27).

Every test here drives ``DiscordAdapter.send`` against a mocked client; none
of them requires a live Discord connection.
"""

import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord import adapter as discord_adapter  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402

TOKEN = "MTIzNDU2Nzg5.SUPER-SECRET-BOT-TOKEN"


# ── helpers ──────────────────────────────────────────────────────────────
class _Recorder(logging.Handler):
    """Capture records emitted by the Discord adapter module logger."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def __enter__(self):
        discord_adapter.logger.addHandler(self)
        self._prev_level = discord_adapter.logger.level
        self._prev_prop = discord_adapter.logger.propagate
        discord_adapter.logger.setLevel(logging.DEBUG)
        discord_adapter.logger.propagate = False
        return self

    def __exit__(self, *exc):
        discord_adapter.logger.removeHandler(self)
        discord_adapter.logger.setLevel(self._prev_level)
        discord_adapter.logger.propagate = self._prev_prop
        return False

    @property
    def text(self):
        return "\n".join(r.getMessage() for r in self.records)

    def errors(self):
        return [r for r in self.records if r.levelno >= logging.ERROR]


def _message(msg_id):
    return SimpleNamespace(id=msg_id)


def _channel(channel_id=555, *, posted_ids=(1001, 1002, 1003), readable=True,
             read_result="match", name=None):
    """A channel mock that posts ``posted_ids`` in order and reads them back.

    ``read_result``: "match" | "none" | "mismatch" | "raise".
    """
    posted = list(posted_ids)
    state = {"i": 0}

    async def _send(content=None, reference=None, **kwargs):
        i = state["i"]
        state["i"] += 1
        return _message(posted[i] if i < len(posted) else posted[-1])

    async def _fetch_message(message_id):
        if read_result == "none":
            return None
        if read_result == "mismatch":
            return _message(int(message_id) + 7)
        if read_result == "raise":
            raise RuntimeError("404 Not Found (error code: 10008): Unknown Message")
        return _message(int(message_id))

    chan = SimpleNamespace(
        id=channel_id,
        send=AsyncMock(side_effect=_send),
        fetch_message=AsyncMock(side_effect=_fetch_message),
    )
    if name is not None:
        chan.name = name
    if not readable:
        del chan.fetch_message
    return chan


def _adapter(channel=None, *, guilds=None, strict_lookup=True):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token=TOKEN))

    def get_channel(channel_id):
        if strict_lookup and not isinstance(channel_id, int):
            raise AssertionError(f"get_channel called with non-int {channel_id!r}")
        if channel is None:
            return None
        return channel if channel_id == getattr(channel, "id", None) else None

    async def fetch_channel(channel_id):
        if strict_lookup and not isinstance(channel_id, int):
            raise AssertionError(f"fetch_channel called with non-int {channel_id!r}")
        if channel is None or channel_id != getattr(channel, "id", None):
            raise RuntimeError("404 Not Found (error code: 10003): Unknown Channel")
        return channel

    client = SimpleNamespace(
        get_channel=MagicMock(side_effect=get_channel),
        fetch_channel=AsyncMock(side_effect=fetch_channel),
    )
    if guilds is not None:
        client.guilds = guilds
    adapter._client = client
    return adapter


def _guild(text_channels=(), threads=(), forums=None):
    g = SimpleNamespace(text_channels=list(text_channels), threads=list(threads))
    if forums is not None:
        g.forums = list(forums)
    return g


# ── 6a. strict snowflake validation (C-04..C-07) ─────────────────────────
INVALID_IDS = [
    "",             # A1
    "   ",          # A1
    "0",            # A2
    "-123",         # A3
    "+123",         # A4
    "1_2",          # A5
    "١٢٣",  # A6 non-ASCII decimal digits
    "12.0",         # A7
    "0123",         # A8
    "1 2",          # A9
    None,           # A10
    True,           # A11
    "1" * 21,       # A12
]


@pytest.mark.parametrize("bad_id", INVALID_IDS)
@pytest.mark.asyncio
async def test_invalid_channel_id_fails_loud_without_posting(bad_id):
    channel = _channel()
    adapter = _adapter(channel)
    with _Recorder() as log:
        result = await adapter.send(bad_id, "hello")
    assert result.success is False, f"{bad_id!r} must not be accepted as a channel id"
    assert result.error
    assert channel.send.await_count == 0
    # The value must be rejected BEFORE any lookup — int() must never see it.
    assert adapter._client.get_channel.call_count == 0, f"{bad_id!r} reached get_channel"
    assert adapter._client.fetch_channel.await_count == 0
    assert log.errors(), f"{bad_id!r} must be reported at ERROR level"


@pytest.mark.parametrize("bad_id", INVALID_IDS)
@pytest.mark.asyncio
async def test_invalid_thread_id_fails_loud_without_posting(bad_id):
    """C-07 — thread_id goes through the same predicate as chat_id."""
    channel = _channel()
    adapter = _adapter(channel)
    with _Recorder() as log:
        result = await adapter.send("555", "hello", metadata={"thread_id": bad_id})
    assert result.success is False
    assert channel.send.await_count == 0
    assert adapter._client.get_channel.call_count == 0, f"{bad_id!r} reached get_channel"
    assert log.errors()


@pytest.mark.parametrize("good_id", [" 555 ", 555])
@pytest.mark.asyncio
async def test_valid_ids_are_accepted(good_id):
    """A13/A14 — one strip, and a positive int."""
    channel = _channel()
    adapter = _adapter(channel)
    result = await adapter.send(good_id, "hello")
    assert result.success is True
    assert result.message_id == "1001"


# ── 6b. name resolution (C-08..C-13) ─────────────────────────────────────
@pytest.mark.parametrize("target", ["#general", "general"])
@pytest.mark.asyncio
async def test_exact_channel_name_resolves(target):
    channel = _channel(channel_id=555, name="general")
    adapter = _adapter(None, guilds=[_guild(text_channels=[channel])])
    result = await adapter.send(target, "hello")
    # C-13: the object found by the guild scan is the object posted to.
    assert result.success is True
    assert channel.send.await_count == 1


@pytest.mark.asyncio
async def test_channel_name_match_is_case_sensitive():
    channel = _channel(channel_id=555, name="general")
    adapter = _adapter(None, guilds=[_guild(text_channels=[channel])])
    with _Recorder() as log:
        result = await adapter.send("#General", "hello")
    assert result.success is False
    assert channel.send.await_count == 0
    assert log.errors()


@pytest.mark.asyncio
async def test_unknown_channel_name_fails_loud():
    channel = _channel(channel_id=555, name="general")
    adapter = _adapter(None, guilds=[_guild(text_channels=[channel])])
    with _Recorder() as log:
        result = await adapter.send("nope", "hello")
    assert result.success is False
    assert channel.send.await_count == 0
    assert log.errors()


@pytest.mark.asyncio
async def test_ambiguous_channel_name_across_guilds_fails_loud_with_ids():
    a = _channel(channel_id=111, name="general")
    b = _channel(channel_id=222, name="general")
    adapter = _adapter(None, guilds=[_guild(text_channels=[a]), _guild(text_channels=[b])])
    with _Recorder() as log:
        result = await adapter.send("#general", "hello")
    assert result.success is False
    assert a.send.await_count == 0 and b.send.await_count == 0
    blob = log.text + (result.error or "")
    assert "111" in blob and "222" in blob, "ambiguity diagnostic must name the competing ids"
    assert log.errors()


@pytest.mark.asyncio
async def test_ambiguous_channel_and_thread_name_fails_loud():
    chan = _channel(channel_id=111, name="general")
    thread = _channel(channel_id=333, name="general")
    adapter = _adapter(None, guilds=[_guild(text_channels=[chan], threads=[thread])])
    result = await adapter.send("general", "hello")
    assert result.success is False
    assert chan.send.await_count == 0 and thread.send.await_count == 0


@pytest.mark.asyncio
async def test_bare_hash_target_fails_loud():
    adapter = _adapter(None, guilds=[_guild()])
    with _Recorder() as log:
        result = await adapter.send("#", "hello")
    assert result.success is False
    assert log.errors()


@pytest.mark.asyncio
async def test_client_without_guilds_fails_loud_on_name():
    adapter = _adapter(None)  # no guilds attribute at all
    with _Recorder() as log:
        result = await adapter.send("#general", "hello")
    assert result.success is False
    assert log.errors()


@pytest.mark.asyncio
async def test_name_is_never_passed_to_int_or_channel_lookup():
    """C-10 — the lookup mocks raise if handed anything that is not an int."""
    channel = _channel(channel_id=555, name="general")
    adapter = _adapter(None, guilds=[_guild(text_channels=[channel])])
    result = await adapter.send("#general", "hello")
    assert result.success is True
    assert adapter._client.fetch_channel.await_count == 0
    assert adapter._client.get_channel.call_count == 0


@pytest.mark.asyncio
async def test_nameless_candidates_are_skipped():
    """C-11 — a candidate without .name must not raise or match."""
    nameless = SimpleNamespace(id=999)
    channel = _channel(channel_id=555, name="general")
    forum = _channel(channel_id=777, name="forum-a")
    adapter = _adapter(
        None,
        guilds=[_guild(text_channels=[nameless, channel], forums=[forum])],
    )
    result = await adapter.send("general", "hello")
    assert result.success is True
    assert channel.send.await_count == 1


@pytest.mark.asyncio
async def test_resolved_name_target_is_the_scanned_object():
    """C-13 — identity, not a second lookup."""
    channel = _channel(channel_id=555, name="general")
    adapter = _adapter(None, guilds=[_guild(text_channels=[channel])])
    result = await adapter.send("general", "hi")
    assert result.success is True
    assert channel.fetch_message.await_count == 1


# ── 6c. loud API failures (C-14..C-18) ───────────────────────────────────
@pytest.mark.asyncio
async def test_fetch_channel_404_fails_loud():
    adapter = _adapter(None)  # every fetch_channel raises 404
    with _Recorder() as log:
        result = await adapter.send("555", "hello")
    assert result.success is False
    assert log.errors(), "a 404 must be reported at ERROR level, not debug/trace"


@pytest.mark.asyncio
async def test_channel_lookup_returning_none_fails_loud():
    async def fetch_channel(channel_id):
        return None

    adapter = _adapter(None)
    adapter._client.fetch_channel = AsyncMock(side_effect=fetch_channel)
    with _Recorder() as log:
        result = await adapter.send("555", "hello")
    assert result.success is False
    assert log.errors()


@pytest.mark.asyncio
async def test_forbidden_send_fails_loud():
    channel = _channel()
    channel.send = AsyncMock(
        side_effect=RuntimeError("403 Forbidden (error code: 50013): Missing Permissions")
    )
    adapter = _adapter(channel)
    with _Recorder() as log:
        result = await adapter.send("555", "hello")
    assert result.success is False
    assert log.errors()


@pytest.mark.asyncio
async def test_mid_chunk_failure_preserves_landed_ids():
    """C-18 / row C4 — chunk 1 landed; its id is repair evidence."""
    channel = _channel()
    calls = {"n": 0}

    async def _send(content=None, reference=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("500 Internal Server Error")
        return _message(1000 + calls["n"])

    channel.send = AsyncMock(side_effect=_send)
    adapter = _adapter(channel)
    adapter.truncate_message = lambda content, max_len, **kw: ["a", "b", "c"]
    with _Recorder() as log:
        result = await adapter.send("555", "hello")
    assert result.success is False
    assert (result.raw_response or {}).get("message_ids") == ["1001"]
    assert log.errors()


@pytest.mark.asyncio
async def test_failure_diagnostics_are_secret_free():
    """C-16 — no URL, no token, no message body in logs or in error."""
    body = "TOP-SECRET-MESSAGE-BODY-2f4a"
    channel = _channel()
    channel.send = AsyncMock(
        side_effect=RuntimeError(
            f"400 Bad Request https://discord.com/api/v10/channels/555/messages"
            f"?token={TOKEN} body={body}"
        )
    )
    adapter = _adapter(channel)
    with _Recorder() as log:
        result = await adapter.send("555", body)
    assert result.success is False
    blob = log.text + "\n" + (result.error or "")
    assert "https://" not in blob and "http://" not in blob
    assert TOKEN not in blob
    assert body not in blob


# ── 6d. received-message acceptance (C-19..C-27) ─────────────────────────
@pytest.mark.asyncio
async def test_successful_send_reads_the_posted_message_back():
    channel = _channel()
    adapter = _adapter(channel)
    result = await adapter.send("555", "hello")
    assert result.success is True
    assert result.message_id == "1001"
    channel.fetch_message.assert_awaited_once_with(1001)


@pytest.mark.parametrize("mode", ["none", "mismatch", "raise"])
@pytest.mark.asyncio
async def test_unverified_post_is_a_failure_with_evidence(mode):
    channel = _channel(read_result=mode)
    adapter = _adapter(channel)
    with _Recorder() as log:
        result = await adapter.send("555", "hello")
    assert result.success is False
    assert result.message_id == "1001", "posted id must survive as repair evidence"
    assert (result.raw_response or {}).get("message_ids") == ["1001"]
    acceptance = (result.raw_response or {}).get("delivery_acceptance")
    assert isinstance(acceptance, dict)
    assert acceptance.get("unverified_message_id") == "1001"
    assert acceptance.get("reason")
    assert "verified_message_ids" in acceptance
    assert result.retryable is False
    assert channel.send.await_count == 1, "the side effect must never be retried"
    assert log.errors()


@pytest.mark.asyncio
async def test_target_without_read_back_capability_fails():
    channel = _channel(readable=False)
    adapter = _adapter(channel)
    with _Recorder() as log:
        result = await adapter.send("555", "hello")
    assert result.success is False
    assert result.message_id == "1001"
    assert channel.send.await_count == 1
    assert log.errors()


@pytest.mark.asyncio
async def test_multi_chunk_stops_at_first_unverified_chunk():
    """Row D6 — chunk 2 fails read-back: no chunk 3, evidence kept."""
    channel = _channel()
    posted = {"n": 0}
    fetched = []

    async def _send(content=None, reference=None, **kwargs):
        posted["n"] += 1
        return _message(1000 + posted["n"])

    async def _fetch_message(message_id):
        fetched.append(message_id)
        if message_id == 1002:
            return None
        return _message(message_id)

    channel.send = AsyncMock(side_effect=_send)
    channel.fetch_message = AsyncMock(side_effect=_fetch_message)
    adapter = _adapter(channel)
    adapter.truncate_message = lambda content, max_len, **kw: ["a", "b", "c"]

    result = await adapter.send("555", "hello")
    assert result.success is False
    assert result.message_id == "1001"
    assert (result.raw_response or {}).get("message_ids") == ["1001", "1002"]
    assert fetched == [1001, 1002]
    assert channel.send.await_count == 2, "chunk 3 must not be posted"


@pytest.mark.asyncio
async def test_thread_target_is_read_back_on_the_thread():
    """Row D8 — the read-back must hit the thread, not the parent."""
    parent = _channel(channel_id=555)
    thread = _channel(channel_id=999, posted_ids=(2001,))

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token=TOKEN))
    lookup = {555: parent, 999: thread}
    adapter._client = SimpleNamespace(
        get_channel=lambda cid: lookup.get(cid),
        fetch_channel=AsyncMock(side_effect=AssertionError("unexpected fetch")),
    )

    result = await adapter.send("555", "hello", metadata={"thread_id": "999"})
    assert result.success is True
    assert thread.fetch_message.await_count == 1
    assert parent.fetch_message.await_count == 0
    assert parent.send.await_count == 0


@pytest.mark.asyncio
async def test_send_with_retry_never_reposts_after_acceptance_failure():
    """C-26 / row D7 — base-class recovery must not duplicate a landed post."""
    channel = _channel(read_result="mismatch")
    adapter = _adapter(channel)

    result = await adapter._send_with_retry("555", "hello", max_retries=2, base_delay=0)
    assert result.success is False
    assert result.message_id == "1001"
    assert channel.send.await_count == 1, (
        "the message already landed; retry/plain-text fallback must not post again"
    )


@pytest.mark.asyncio
async def test_edit_message_uses_the_shared_exact_name_resolver():
    """C-04 — edits use the same strict id/name target contract as sends."""
    message = SimpleNamespace(id=42, edit=AsyncMock())
    channel = _channel(channel_id=555, name="general")
    channel.fetch_message = AsyncMock(return_value=message)
    adapter = _adapter(None, guilds=[_guild(text_channels=[channel])])

    result = await adapter.edit_message("general", "42", "updated")

    assert result.success is True
    message.edit.assert_awaited_once()
    assert adapter._client.get_channel.call_count == 0
    assert adapter._client.fetch_channel.await_count == 0


@pytest.mark.asyncio
async def test_lookup_failure_scrubs_the_configured_bot_token():
    """C-16 — provider lookup diagnostics cannot echo the configured token."""
    adapter = _adapter(None)
    adapter._client.fetch_channel = AsyncMock(
        side_effect=RuntimeError(f"lookup rejected Authorization: Bot {TOKEN}")
    )
    with _Recorder() as log:
        result = await adapter.send("555", "harmless body")

    assert result.success is False
    assert TOKEN not in (result.error or "")
    assert TOKEN not in log.text
