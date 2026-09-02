"""
Tests for Slack Socket Mode dedup TTL (#4777).

Slack replays un-acked Socket Mode events when the websocket reconnects.
The replay can land several minutes after the original; the dedup window
must outlast that gap so the redelivered event is suppressed instead of
producing a second bot reply. Regression for the 300s-default bug where
replays >5 min later slipped through.

Follows the slack-bolt mocking pattern from test_slack_mention.py.
"""

import asyncio
import os
import sys
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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

from gateway.platforms.helpers import MessageDeduplicator  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.slack.adapter import (  # noqa: E402
    SlackAdapter,
    _slack_dedup_ttl_seconds,
)


def test_default_ttl_outlasts_slack_reconnect_redelivery_window():
    # The whole point of the fix: the window must be much longer than the
    # ~6 min reconnect-redelivery gap that caused the duplicate reply.
    with patch.dict(os.environ, {}, clear=True):
        assert _slack_dedup_ttl_seconds() >= 1800.0


def test_env_override_is_respected():
    with patch.dict(os.environ, {"SLACK_DEDUP_TTL_SECONDS": "120"}, clear=True):
        assert _slack_dedup_ttl_seconds() == 120.0


def test_fallback_identity_preserves_distinct_workspaces_and_channels():
    """ID-less fallback must not merge distinct workspace messages."""
    first = {
        "team": "T01KC2VC5U0",
        "channel": "C0A3PUZPGN5",
        "ts": "1787682677.049629",
    }
    same_message = dict(first)
    other_workspace = {**first, "team": "T079QU5LX36"}
    distinct_message = {**first, "channel": "C0DISTINCT01"}
    dedup = MessageDeduplicator()
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    adapter._team_clients = {
        "T01KC2VC5U0": MagicMock(),
        "T079QU5LX36": MagicMock(),
    }

    assert dedup.is_duplicate(adapter._message_dedup_id(first)) is False
    assert dedup.is_duplicate(adapter._message_dedup_id(same_message)) is True
    assert dedup.is_duplicate(adapter._message_dedup_id(other_workspace)) is False
    assert dedup.is_duplicate(adapter._message_dedup_id(distinct_message)) is False


def test_client_message_id_is_not_process_global_across_workspaces():
    event = {"channel": "C0A3PUZPGN5", "ts": "1787682677.049629"}
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    adapter._team_clients = {
        "T01KC2VC5U0": MagicMock(),
        "T079QU5LX36": MagicMock(),
    }

    assert adapter._message_dedup_id(
        {**event, "team": "T01KC2VC5U0", "client_msg_id": "same-client-id"}
    ) != adapter._message_dedup_id(
        {**event, "team": "T079QU5LX36", "client_msg_id": "same-client-id"}
    )


def test_single_authenticated_workspace_normalizes_enterprise_scope():
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    adapter._team_clients = {"T079QU5LX36": MagicMock()}
    event = {
        "team": "T01KC2VC5U0",
        "channel": "C0A3PUZPGN5",
        "ts": "1787682677.049629",
    }
    payload = {
        "team_id": "T01KC2VC5U0",
        "authorizations": [{"team_id": "T079QU5LX36"}],
    }

    assert adapter._canonical_event_team_id(event, payload) == "T079QU5LX36"
    assert adapter._message_dedup_id(event, payload) == (
        "slack:channel-message:T079QU5LX36:C0A3PUZPGN5:1787682677.049629"
    )


def test_known_channel_mapping_normalizes_enterprise_scope_with_multiple_workspaces():
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    adapter._team_clients = {
        "T_WORKSPACE_A": MagicMock(),
        "T079QU5LX36": MagicMock(),
    }
    adapter._channel_team = {"C0A3PUZPGN5": "T079QU5LX36"}
    event = {
        "team": "E01ENTERPRISE",
        "channel": "C0A3PUZPGN5",
        "ts": "1787682677.049629",
    }

    assert adapter._canonical_event_team_id(event, {}) == "T079QU5LX36"


def test_processed_edit_keys_are_workspace_and_channel_scoped():
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    adapter._team_clients = {
        "T_ONE": MagicMock(),
        "T_TWO": MagicMock(),
    }
    first = {"team": "T_ONE", "channel": "C_ONE"}
    second = {"team": "T_TWO", "channel": "C_TWO"}

    assert adapter._processed_message_key(first, {}, "123.456") != (
        adapter._processed_message_key(second, {}, "123.456")
    )


def test_scope_variants_with_different_envelopes_share_message_alias():
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    adapter._team_clients = {"T079QU5LX36": MagicMock()}
    dedup = MessageDeduplicator()
    enterprise_event = {
        "team": "T01KC2VC5U0",
        "channel": "C0A3PUZPGN5",
        "ts": "1787682677.049629",
    }
    workspace_event = {**enterprise_event, "team": "T079QU5LX36"}
    enterprise_payload = {
        "event_id": "EvEnterpriseDelivery",
        "team_id": "T01KC2VC5U0",
        "authorizations": [{"team_id": "T079QU5LX36"}],
    }
    workspace_payload = {
        "event_id": "EvWorkspaceDelivery",
        "team_id": "T079QU5LX36",
    }

    assert dedup.is_duplicate(
        adapter._message_dedup_id(enterprise_event, enterprise_payload)
    ) is False
    assert dedup.is_duplicate(
        adapter._message_dedup_id(workspace_event, workspace_payload)
    ) is True


def test_missing_all_stable_identity_does_not_collapse_messages():
    """Without any stable ID or timestamp, let both messages reach routing."""
    event = {"team": "T_TEAM", "channel": "C_ONE", "text": "same text"}

    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    assert adapter._message_dedup_id(event) == ""


def test_missing_workspace_identity_skips_cross_tenant_dedup():
    """Unknown workspace scope must fail open rather than share a sentinel."""
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    adapter._team_clients = {}
    event = {"channel": "C_ONE", "ts": "1787682677.049629"}

    assert adapter._message_dedup_id(event) == ""
    assert adapter._processed_message_key(event, {}, event["ts"]) == ""


@pytest.mark.asyncio
async def test_file_shared_client_routing_does_not_use_dedup_canonicalizer():
    """A foreign event scope must fall back instead of guessing one workspace."""
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    adapter._app = MagicMock()
    primary_client = MagicMock()
    primary_client.files_info = AsyncMock(
        return_value={"ok": True, "file": {"mimetype": "text/plain"}}
    )
    guessed_client = MagicMock()
    guessed_client.files_info = AsyncMock()
    adapter._app.client = primary_client
    adapter._team_clients = {"T_HOME": guessed_client}

    await adapter._handle_slack_file_shared(
        {
            "team": "E_PARTNER_ORG",
            "channel_id": "C_SHARED",
            "file_id": "F_SHARED",
        }
    )

    primary_client.files_info.assert_awaited_once_with(file="F_SHARED")
    guessed_client.files_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_duplicate_event_runs_one_downstream_turn():
    """Concurrent scope variants must atomically claim one Slack message."""
    adapter = SlackAdapter(
        PlatformConfig(enabled=True, token="xoxb-test", extra={"require_mention": True})
    )
    adapter._app = MagicMock()
    adapter._app.client = AsyncMock()
    adapter._bot_user_id = "U_BOT"
    adapter._team_bot_user_ids = {"T_TEAM": "U_BOT"}
    adapter._team_clients = {"T079QU5LX36": MagicMock()}

    callback_count = 0
    callback_lock = threading.Lock()

    async def downstream_turn(_event):
        nonlocal callback_count
        with callback_lock:
            callback_count += 1

    adapter.handle_message = downstream_turn
    adapter._resolve_user_is_bot = AsyncMock(return_value=False)
    adapter._resolve_user_name = AsyncMock(return_value="Test User")
    adapter._resolve_channel_name = AsyncMock(return_value="test-channel")
    adapter._humanize_user_mentions = AsyncMock(side_effect=lambda text, **_: text)

    enterprise_event = {
        "type": "message",
        "team": "T01KC2VC5U0",
        "channel": "C0A3PUZPGN5",
        "channel_type": "channel",
        "user": "U_USER",
        "text": "<@U_BOT> run this once",
        "ts": "1787682677.049629",
        "client_msg_id": "11111111-2222-4333-8444-555555555555",
    }
    workspace_event = {**enterprise_event, "team": "T079QU5LX36"}
    enterprise_payload = {
        "event_id": "EvEnterpriseDelivery",
        "team_id": "T01KC2VC5U0",
        "event": enterprise_event,
    }
    workspace_payload = {
        "event_id": "EvWorkspaceDelivery",
        "team_id": "T079QU5LX36",
        "event": workspace_event,
    }

    start_barrier = threading.Barrier(2)

    async def start_together(event, payload):
        start_barrier.wait(timeout=2)
        await adapter._handle_slack_message(event, payload)

    await asyncio.gather(
        asyncio.to_thread(
            asyncio.run,
            start_together(enterprise_event, enterprise_payload),
        ),
        asyncio.to_thread(
            asyncio.run,
            start_together(workspace_event, workspace_payload),
        ),
    )

    assert callback_count == 1


