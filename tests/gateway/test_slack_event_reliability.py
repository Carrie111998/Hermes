"""Slack inbound event reliability — no event may vanish without a trace.

``SlackAdapter._handle_slack_message`` is the single ingress for every inbound
Slack event (``message``, ``app_mention``, file shares, reaction routes). Two
invariants are asserted here:

1.  **Delivery is accounted for.** If anything between socket receipt and
    ``handle_message()`` raises, the event must produce an ERROR that names it
    (channel / ts / thread_ts) and must RELEASE its deduplication claim, so the
    sibling ``app_mention`` Slack fires for the same @mention — or a Socket
    Mode redelivery — can still be processed. The Discord adapter already
    releases the claim on a failed handoff
    (``_dedup.discard`` in ``_scan_recent_discord_messages``); Slack did not,
    so a mid-flight failure swallowed the message permanently and silently.

2.  **Every intentional discard is logged.** The bare ``return`` filters in the
    ingress (dedup hit, ``allow_bots``, ``message_deleted``, strict mention,
    unmentioned non-wake) emitted nothing at INFO, so a filtered event left no
    trace at all and was indistinguishable from an event Slack never sent.

Regression for the incident where an @mention posted in a thread while another
gateway turn was active never reached the gateway and never appeared in logs.
"""

import asyncio
import logging
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig


def _ensure_slack_mock():
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return

    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock

    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock

    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        ("slack_bolt.adapter.socket_mode.async_handler", slack_bolt.adapter.socket_mode.async_handler),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)


_ensure_slack_mock()

import plugins.platforms.slack.adapter as _slack_mod  # noqa: E402

_slack_mod.SLACK_AVAILABLE = True

from plugins.platforms.slack.adapter import SlackAdapter  # noqa: E402

ADAPTER_LOGGER = "plugins.platforms.slack.adapter"

BOT_USER_ID = "U0BFVBD9H19"
CHANNEL_ID = "C0BK9LZFYFM"
THREAD_TS = "1785180592.650829"
MESSAGE_TS = "1785180644.192059"
TEAM_ID = "T0BF6M0M2AZ"
SENDER_ID = "U_BROCK"


def _make_adapter(**extra):
    adapter = SlackAdapter(
        PlatformConfig(enabled=True, token="xoxb-test", extra=dict(extra))
    )
    adapter.platform = Platform.SLACK
    adapter._bot_user_id = BOT_USER_ID
    adapter._resolve_user_is_bot = AsyncMock(return_value=False)
    adapter._resolve_user_name = AsyncMock(return_value="brock")
    adapter._resolve_channel_name = AsyncMock(return_value="robert-ops-shadow")
    adapter._fetch_thread_context = AsyncMock(return_value="")
    adapter.handle_message = AsyncMock()
    return adapter


def _mention_in_thread(**overrides):
    """The incident event: an @mention posted as a reply in an existing thread."""
    event = {
        "type": "message",
        "channel": CHANNEL_ID,
        "channel_type": "channel",
        "ts": MESSAGE_TS,
        "thread_ts": THREAD_TS,
        "team": TEAM_ID,
        "user": SENDER_ID,
        "client_msg_id": "cmid-incident",
        "text": f"<@{BOT_USER_ID}> do you see any duplicate subscriptions?",
    }
    event.update(overrides)
    return event


def _discard_records(caplog):
    """Log records that name the discarded event's Slack coordinates."""
    return [
        record
        for record in caplog.records
        if MESSAGE_TS in record.getMessage() and CHANNEL_ID in record.getMessage()
    ]


# ---------------------------------------------------------------------------
# 1. Delivery accounting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_mention_in_thread_is_delivered():
    """Control: the incident event reaches the gateway on the happy path."""
    adapter = _make_adapter()

    await adapter._handle_slack_message(_mention_in_thread())

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_ingress_failure_is_logged_with_event_coordinates(caplog):
    """A mid-flight failure must name the lost event, not vanish."""
    adapter = _make_adapter()
    adapter._resolve_channel_name = AsyncMock(side_effect=RuntimeError("ratelimited"))

    with caplog.at_level(logging.INFO, logger=ADAPTER_LOGGER):
        await adapter._handle_slack_message(_mention_in_thread())

    assert adapter.handle_message.await_count == 0
    failures = [r for r in _discard_records(caplog) if r.levelno >= logging.ERROR]
    assert failures, (
        "a Slack event lost before delivery produced no ERROR naming it — "
        f"records: {[r.getMessage() for r in caplog.records]}"
    )
    assert THREAD_TS in failures[0].getMessage()


@pytest.mark.asyncio
async def test_ingress_failure_releases_dedup_claim_so_sibling_event_delivers():
    """Slack fires ``message`` AND ``app_mention`` for one @mention.

    The two share an event ts, so whichever reaches the deduplicator first
    claims it. If that one dies mid-flight the claim must be released, or the
    sibling — the user's only remaining chance to be heard — is swallowed.
    """
    adapter = _make_adapter()
    adapter._resolve_channel_name = AsyncMock(side_effect=RuntimeError("ratelimited"))

    await adapter._handle_slack_message(_mention_in_thread())
    assert adapter.handle_message.await_count == 0

    # The sibling app_mention for the very same user action.
    adapter._resolve_channel_name = AsyncMock(return_value="robert-ops-shadow")
    await adapter._handle_slack_message(_mention_in_thread(type="app_mention"))

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_successful_delivery_keeps_dedup_claim():
    """The release must not weaken redelivery suppression on the happy path."""
    adapter = _make_adapter()

    await adapter._handle_slack_message(_mention_in_thread())
    await adapter._handle_slack_message(_mention_in_thread(type="app_mention"))

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_ingress_failure_does_not_break_the_socket_listener():
    """Bolt's listener must not see the exception — the ingress owns it."""
    adapter = _make_adapter()
    adapter._resolve_channel_name = AsyncMock(side_effect=RuntimeError("ratelimited"))

    await adapter._handle_slack_message(_mention_in_thread())


@pytest.mark.asyncio
async def test_cancellation_releases_the_claim_and_still_propagates():
    """Shutdown must stay cancellable, but must not eat the event either."""
    adapter = _make_adapter()
    adapter._resolve_channel_name = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await adapter._handle_slack_message(_mention_in_thread())

    adapter._resolve_channel_name = AsyncMock(return_value="robert-ops-shadow")
    await adapter._handle_slack_message(_mention_in_thread())

    adapter.handle_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. Discard observability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_event_discard_is_logged(caplog):
    adapter = _make_adapter()
    await adapter._handle_slack_message(_mention_in_thread())
    caplog.clear()

    with caplog.at_level(logging.INFO, logger=ADAPTER_LOGGER):
        await adapter._handle_slack_message(_mention_in_thread(type="app_mention"))

    records = _discard_records(caplog)
    assert records, "a deduplicated Slack event produced no log line"
    assert any("duplicate" in r.getMessage().lower() for r in records)


@pytest.mark.asyncio
async def test_deleted_message_discard_is_logged(caplog):
    adapter = _make_adapter()

    with caplog.at_level(logging.INFO, logger=ADAPTER_LOGGER):
        await adapter._handle_slack_message(
            _mention_in_thread(subtype="message_deleted")
        )

    assert adapter.handle_message.await_count == 0
    assert _discard_records(caplog), "a deleted-message event produced no log line"


@pytest.mark.asyncio
async def test_bot_sender_discard_is_logged(caplog):
    """``allow_bots=none`` drops bot-authored events — say so."""
    adapter = _make_adapter(allow_bots="none")
    adapter._resolve_user_is_bot = AsyncMock(return_value=True)

    with caplog.at_level(logging.INFO, logger=ADAPTER_LOGGER):
        await adapter._handle_slack_message(
            _mention_in_thread(bot_id="B123", client_msg_id=None)
        )

    assert adapter.handle_message.await_count == 0
    assert _discard_records(caplog), "a bot-filtered event produced no log line"


@pytest.mark.asyncio
async def test_unmentioned_thread_reply_discard_is_logged(caplog):
    """The default require_mention path drops ambient chatter — say so."""
    adapter = _make_adapter(require_mention=True)

    with caplog.at_level(logging.INFO, logger=ADAPTER_LOGGER):
        await adapter._handle_slack_message(
            _mention_in_thread(text="ambient chatter with no mention")
        )

    assert adapter.handle_message.await_count == 0
    assert _discard_records(caplog), "an unmentioned event produced no log line"


@pytest.mark.asyncio
async def test_discard_log_never_contains_message_text(caplog):
    """Discard logs are metadata only — they must not leak message content."""
    secret = "customer-id-12345-private"
    adapter = _make_adapter(require_mention=True)

    with caplog.at_level(logging.DEBUG, logger=ADAPTER_LOGGER):
        await adapter._handle_slack_message(_mention_in_thread(text=secret))

    assert adapter.handle_message.await_count == 0
    assert secret not in caplog.text
