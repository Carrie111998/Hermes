"""QQ Bot group @-message policy tests.

Covers the explicit-allowlist group policy model:

* default ``group_policy=pairing`` denies every group @ message (QQ has no
  group pairing flow) and leaves a masked WARNING log;
* ``group_policy=allowlist`` + ``group_allow_from`` allows only explicitly
  configured groups;
* an optional member allowlist (``group_member_allow_from``) gates which
  members inside an allowed group may interact — and is never mixed with the
  private-chat C2C allowlist (``QQ_ALLOWED_USERS`` / ``allow_from``);
* denied messages never reach ``handle_message`` (no MessageEvent);
* logs never contain full group/member OpenIDs;
* non-@ group events (``GROUP_MESSAGE_CREATE``) are not newly subscribed or
  routed.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig

GROUP_ID = "47A1B2C3D4E5F60718293A4B5C6D7E8F"
OTHER_GROUP_ID = "58B2C3D4E5F60718293A4B5C6D7E8F0A"
MEMBER_ID = "6C93D4E5F60718293A4B5C6D7E8F0A1B"
OTHER_MEMBER_ID = "7DA4E5F60718293A4B5C6D7E8F0A1B2C"
C2C_OPENID = "C2C_OPENID_0000000000000000000000000000000C"
MSG_ID = "ROBOT1.0_MSG_0000000000000000000000000000000000000000"


def _make_config(**extra):
    """Build a PlatformConfig(enabled=True, extra=extra) for testing."""
    return PlatformConfig(enabled=True, extra=extra)


def _make_adapter(**extra):
    from gateway.platforms.qqbot import QQAdapter
    return QQAdapter(_make_config(**extra))


def _group_payload(group_id=GROUP_ID, member_id=MEMBER_ID, content="@bot hello"):
    return {
        "id": MSG_ID,
        "content": content,
        "group_openid": group_id,
        "author": {"member_openid": member_id},
        "timestamp": "2026-08-01T10:00:00+08:00",
    }


# ---------------------------------------------------------------------------
# 1. Default pairing, no group config → deny + masked log
# ---------------------------------------------------------------------------

class TestDefaultPairingDenies:
    def test_default_pairing_denies_group(self):
        adapter = _make_adapter(app_id="a", client_secret="b")
        assert adapter._group_policy == "pairing"
        assert adapter._is_group_allowed(GROUP_ID) is False

    @pytest.mark.asyncio
    async def test_group_message_denied_and_logged(self, caplog):
        adapter = _make_adapter(app_id="a", client_secret="b")
        adapter.handle_message = AsyncMock()

        await adapter._handle_group_message(
            _group_payload(), MSG_ID, "@bot hello", {"member_openid": MEMBER_ID}, ""
        )

        adapter.handle_message.assert_not_awaited()
        assert any(
            "QQ group message denied" in rec.message
            and "policy=pairing" in rec.message
            and "reason=pairing_unavailable" in rec.message
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# 2. Allowed group, no member allowlist → any member @ passes
# ---------------------------------------------------------------------------

class TestAllowedGroupNoMemberAllowlist:
    def test_policy_decision_allows(self):
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist", group_allow_from=GROUP_ID,
        )
        allowed, reason = adapter._evaluate_group_allowed(GROUP_ID)
        assert allowed is True
        assert reason is None

    @pytest.mark.asyncio
    async def test_group_message_reaches_handle_message(self):
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist", group_allow_from=GROUP_ID,
        )
        adapter.handle_message = AsyncMock()

        await adapter._handle_group_message(
            _group_payload(), MSG_ID, "@bot hello", {"member_openid": MEMBER_ID}, ""
        )

        adapter.handle_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. Non-allowed group → deny
# ---------------------------------------------------------------------------

class TestNonAllowedGroupDenied:
    def test_policy_decision_denies_group(self):
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist", group_allow_from=GROUP_ID,
        )
        allowed, reason = adapter._evaluate_group_allowed(OTHER_GROUP_ID)
        assert allowed is False
        assert reason == "group_not_allowed"

    @pytest.mark.asyncio
    async def test_group_message_denied_with_reason(self, caplog):
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist", group_allow_from=GROUP_ID,
        )
        adapter.handle_message = AsyncMock()

        await adapter._handle_group_message(
            _group_payload(group_id=OTHER_GROUP_ID),
            MSG_ID, "@bot hello", {"member_openid": MEMBER_ID}, "",
        )

        adapter.handle_message.assert_not_awaited()
        assert any(
            "QQ group message denied" in rec.message
            and "reason=group_not_allowed" in rec.message
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# 4. Allowed group + allowed member → pass
# ---------------------------------------------------------------------------

class TestAllowedGroupAndMember:
    @pytest.mark.asyncio
    async def test_member_message_reaches_handle_message(self):
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist", group_allow_from=GROUP_ID,
            group_member_allow_from=MEMBER_ID,
        )
        adapter.handle_message = AsyncMock()

        await adapter._handle_group_message(
            _group_payload(), MSG_ID, "@bot hello", {"member_openid": MEMBER_ID}, ""
        )

        adapter.handle_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. Allowed group + non-allowed member → deny
# ---------------------------------------------------------------------------

class TestAllowedGroupNonAllowedMemberDenied:
    @pytest.mark.asyncio
    async def test_member_message_denied_with_reason(self, caplog):
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist", group_allow_from=GROUP_ID,
            group_member_allow_from=MEMBER_ID,
        )
        adapter.handle_message = AsyncMock()

        await adapter._handle_group_message(
            _group_payload(member_id=OTHER_MEMBER_ID),
            MSG_ID, "@bot hello", {"member_openid": OTHER_MEMBER_ID}, "",
        )

        adapter.handle_message.assert_not_awaited()
        assert any(
            "QQ group message denied" in rec.message
            and "reason=member_not_allowed" in rec.message
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# 6. Group members never need to hit the C2C allow_from / QQ_ALLOWED_USERS
# ---------------------------------------------------------------------------

class TestGroupMemberIndependentOfC2CAllowlist:
    def test_member_allowed_without_c2c_allow_from(self):
        # allow_from holds a C2C openid (private chat); the group member is a
        # different value and is NOT listed — the group gate must still pass.
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist", group_allow_from=GROUP_ID,
            allow_from=C2C_OPENID,
        )
        allowed, reason = adapter._evaluate_group_allowed(GROUP_ID)
        assert allowed is True
        assert reason is None

    @pytest.mark.asyncio
    async def test_member_allowlist_does_not_read_c2c_allow_from(self):
        # With a member allowlist configured, only it decides membership —
        # a member listed in the C2C allow_from but absent from the member
        # allowlist stays denied at the group-message path.
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist", group_allow_from=GROUP_ID,
            allow_from=MEMBER_ID,  # member id sits in the C2C allowlist
            group_member_allow_from=OTHER_MEMBER_ID,
        )
        adapter.handle_message = AsyncMock()

        await adapter._handle_group_message(
            _group_payload(), MSG_ID, "@bot hello", {"member_openid": MEMBER_ID}, ""
        )

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_group_message_passes_without_c2c_allow_from(self):
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist", group_allow_from=GROUP_ID,
            allow_from=C2C_OPENID,
        )
        adapter.handle_message = AsyncMock()

        await adapter._handle_group_message(
            _group_payload(), MSG_ID, "@bot hello", {"member_openid": MEMBER_ID}, ""
        )

        adapter.handle_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# 7. Private-chat C2C allowlist behavior is unchanged
# ---------------------------------------------------------------------------

class TestC2CBehaviorUnchanged:
    def test_dm_allowlist_still_matches(self):
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            dm_policy="allowlist", allow_from=f"{C2C_OPENID},user2",
        )
        assert adapter._is_dm_allowed(C2C_OPENID) is True
        assert adapter._is_dm_allowed("user2") is True
        assert adapter._is_dm_allowed("stranger") is False

    def test_dm_intake_pairing_default_unchanged(self):
        adapter = _make_adapter(app_id="a", client_secret="b")
        assert adapter._is_dm_intake_allowed("any_user") is True
        assert adapter._is_dm_allowed("any_user") is False

    def test_group_member_allowlist_does_not_leak_into_dm(self):
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist", group_allow_from=GROUP_ID,
            group_member_allow_from=MEMBER_ID,
        )
        # The member allowlist must not authorize private-chat senders.
        assert adapter._is_dm_allowed(MEMBER_ID) is False


# ---------------------------------------------------------------------------
# 8. Logs never contain full group/member OpenIDs
# ---------------------------------------------------------------------------

class TestLogMasking:
    @pytest.mark.asyncio
    async def test_denial_log_masks_openids(self, caplog):
        adapter = _make_adapter(app_id="a", client_secret="b")
        adapter.handle_message = AsyncMock()

        await adapter._handle_group_message(
            _group_payload(), MSG_ID, "@bot hello", {"member_openid": MEMBER_ID}, ""
        )

        log_text = "\n".join(rec.message for rec in caplog.records)
        assert "QQ group message denied" in log_text
        assert GROUP_ID not in log_text
        assert MEMBER_ID not in log_text
        assert "47A1B2...7E8F" in log_text  # masked: 6-prefix + 4-suffix
        assert "6C93D4...0A1B" in log_text

    @pytest.mark.asyncio
    async def test_accept_log_masks_openids(self, caplog):
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist", group_allow_from=GROUP_ID,
        )
        adapter.handle_message = AsyncMock()
        caplog.set_level("DEBUG")

        await adapter._handle_group_message(
            _group_payload(), MSG_ID, "@bot hello", {"member_openid": MEMBER_ID}, ""
        )

        log_text = "\n".join(rec.message for rec in caplog.records)
        assert "QQ group message accepted" in log_text
        assert GROUP_ID not in log_text
        assert MEMBER_ID not in log_text

    def test_mask_helper(self):
        from gateway.platforms.qqbot.adapter import _mask_openid
        assert _mask_openid(GROUP_ID) == "47A1B2...7E8F"
        assert _mask_openid("") == "<empty>"
        assert _mask_openid(None) == "<empty>"


# ---------------------------------------------------------------------------
# 9. Passed messages build a correct MessageEvent
# ---------------------------------------------------------------------------

class TestMessageEventConstruction:
    @pytest.mark.asyncio
    async def test_event_fields_are_correct(self):
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist", group_allow_from=GROUP_ID,
        )
        captured = {}
        async def _fake_handle(event):
            captured["event"] = event
        adapter.handle_message = _fake_handle

        await adapter._handle_group_message(
            _group_payload(content="@bot 查天气"),
            MSG_ID, "@bot 查天气", {"member_openid": MEMBER_ID}, "2026-08-01T10:00:00+08:00",
        )

        event = captured["event"]
        assert event.source.chat_type == "group"
        assert event.source.chat_id == GROUP_ID
        assert event.source.user_id == MEMBER_ID
        assert event.message_id == MSG_ID
        # The @-mention prefix is stripped by the adapter before the event.
        assert event.text == "查天气"
        # Group sessions key on group_openid, isolating them from C2C DMs.
        assert adapter._chat_type_map.get(GROUP_ID) == "group"


# ---------------------------------------------------------------------------
# 10. Non-@ group events are not newly subscribed or routed
# ---------------------------------------------------------------------------

class TestNonAtGroupEventsNotHandled:
    def test_group_message_create_not_routed_to_on_message(self, monkeypatch):
        adapter = _make_adapter(app_id="a", client_secret="b")
        scheduled = []
        monkeypatch.setattr(asyncio, "create_task", lambda coro: scheduled.append(coro))

        # GROUP_MESSAGE_CREATE (non-@ group traffic) must NOT be dispatched
        # to _on_message — QQ only pushes GROUP_AT_MESSAGE_CREATE under the
        # subscribed intent, and we must not start handling full-group traffic.
        adapter._dispatch_payload({"op": 0, "t": "GROUP_MESSAGE_CREATE", "d": {}})
        assert scheduled == []

    def test_group_at_message_create_is_routed(self, monkeypatch):
        adapter = _make_adapter(app_id="a", client_secret="b")
        scheduled = []
        monkeypatch.setattr(asyncio, "create_task", lambda coro: scheduled.append(coro))

        adapter._dispatch_payload(
            {"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "d": {"id": MSG_ID}}
        )
        assert len(scheduled) == 1
        # Close the never-run coroutine so pytest sees no pending-task warning.
        scheduled[0].close()


# ---------------------------------------------------------------------------
# 11. Guild ACL stays group-level: member allowlist must not restrict guilds
# ---------------------------------------------------------------------------

GUILD_ID = "9E4C5D6E7F8091A2B3C4D5E6F708192A3"
CHANNEL_ID = "AF5D6E7F8091A2B3C4D5E6F708192A3B4"
GUILD_AUTHOR_ID = "B06E7F8091A2B3C4D5E6F708192A3B4C5"
OTHER_GUILD_ID = "C17F8091A2B3C4D5E6F708192A3B4C5D6"


def _guild_payload(guild_id=GUILD_ID, channel_id=CHANNEL_ID, author_id=GUILD_AUTHOR_ID):
    return {
        "id": MSG_ID,
        "content": "@bot hello",
        "guild_id": guild_id,
        "channel_id": channel_id,
        "author": {"id": author_id, "username": "guild_user"},
        "member": {"nick": "guild_user"},
        "timestamp": "2026-08-01T10:00:00+08:00",
    }


class TestGuildACLUnaffectedByMemberAllowlist:
    @pytest.mark.asyncio
    async def test_member_allowlist_does_not_restrict_allowlisted_guild(self):
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist", group_allow_from=GUILD_ID,
            group_member_allow_from=MEMBER_ID,  # guild author is NOT listed
        )
        captured = {}

        async def _fake_handle(event):
            captured["event"] = event

        adapter.handle_message = _fake_handle

        await adapter._handle_guild_message(
            _guild_payload(), MSG_ID, "@bot hello",
            {"id": GUILD_AUTHOR_ID, "username": "guild_user"}, "",
        )

        # Guild traffic is group-level only: author["id"] is never matched
        # against group_member_allow_from.
        assert "event" in captured
        event = captured["event"]
        assert event.source.chat_type == "group"
        assert event.source.chat_id == CHANNEL_ID
        assert event.source.user_id == GUILD_AUTHOR_ID

    @pytest.mark.asyncio
    async def test_unallowlisted_guild_remains_denied(self):
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist", group_allow_from=GUILD_ID,
            group_member_allow_from=MEMBER_ID,
        )
        adapter.handle_message = AsyncMock()

        await adapter._handle_guild_message(
            _guild_payload(guild_id=OTHER_GUILD_ID), MSG_ID, "@bot hello",
            {"id": GUILD_AUTHOR_ID}, "",
        )

        adapter.handle_message.assert_not_awaited()
