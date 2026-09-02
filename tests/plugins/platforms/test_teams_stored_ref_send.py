"""Stored conversation-ref send for Teams (governance #1218).

Throwaway HTTP only — never a live Bot Framework host.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from plugins.platforms.teams.stored_ref import (
    StoredRefError,
    activity_post_url,
    classify_stored_ref,
    group_inbound_addresses_bot,
    load_stored_refs,
    persist_inbound_ref,
    send_from_stored_ref,
)


BOT = "00000000-0000-4000-8000-0000000000aa"


def _throwaway(**overrides):
    ref = {
        "kind": "personal",
        "person": "Throwaway",
        "aad_object_id": "00000000-0000-0000-0000-000000000001",
        "tenant_id": "00000000-0000-0000-0000-000000000002",
        "bot_app_id": BOT,
        "service_url": "http://127.0.0.1:9/",
        "conversation_id": "throwaway-1218",
        "user_id": "29:throwaway-roster",
    }
    ref.update(overrides)
    return ref


def test_classify_accepts_personal_matching_bot():
    classify_stored_ref(_throwaway(), expected_bot_app_id=BOT)


def test_classify_rejects_group_without_inbound_addresser():
    with pytest.raises(StoredRefError, match="mention this bot or reply"):
        classify_stored_ref(_throwaway(kind="groupChat"), expected_bot_app_id=BOT)


def test_classify_rejects_group_with_sender_but_no_bot_address():
    with pytest.raises(StoredRefError, match="mention this bot or reply"):
        classify_stored_ref(
            _throwaway(kind="groupChat", addressed_by="29:customer-roster"),
            expected_bot_app_id=BOT,
        )


def test_classify_accepts_group_after_mention():
    classify_stored_ref(
        _throwaway(
            kind="groupChat",
            addressed_by="29:customer-roster",
            addressed_via="mention",
        ),
        expected_bot_app_id=BOT,
    )


def test_classify_accepts_group_after_reply_to_own():
    classify_stored_ref(
        _throwaway(
            kind="groupChat",
            addressed_by="29:customer-roster",
            addressed_via="reply_to_own",
        ),
        expected_bot_app_id=BOT,
    )


def test_classify_rejects_wrong_bot():
    with pytest.raises(StoredRefError, match="bot"):
        classify_stored_ref(_throwaway(), expected_bot_app_id="00000000-0000-0000-0000-000000000099")


def test_classify_rejects_reply_only_policy():
    with pytest.raises(StoredRefError, match="reply_only"):
        classify_stored_ref(
            _throwaway(outbound_policy="reply_only_until_customer_writes"),
            expected_bot_app_id=BOT,
        )


def test_activity_url_uses_service_url_and_conversation_id():
    url = activity_post_url(_throwaway())
    assert url == "http://127.0.0.1:9/v3/conversations/throwaway-1218/activities"


def test_load_stored_refs_indexes_by_conversation_id(tmp_path: Path):
    path = tmp_path / "throwaway.json"
    path.write_text(json.dumps(_throwaway()), encoding="utf-8")
    loaded = load_stored_refs(tmp_path)
    assert "throwaway-1218" in loaded
    assert loaded["throwaway-1218"]["user_id"] == "29:throwaway-roster"


def test_persist_inbound_ref_writes_personal_json(tmp_path: Path):
    dest = persist_inbound_ref(
        tmp_path,
        conversation_id="a:owner-chat",
        conversation_type="personal",
        service_url="https://smba.trafficmanager.net/teams/",
        tenant_id="00000000-0000-0000-0000-000000000002",
        bot_app_id=BOT,
        aad_object_id="00000000-0000-4000-8000-0000000000bb",
        user_id="29:owner-roster",
        person="Owner",
        filename_stem="owner",
    )
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["kind"] == "personal"
    assert data["conversation_id"] == "a:owner-chat"
    assert data["user_id"] == "29:owner-roster"
    assert data["bot_app_id"] == BOT
    assert "outbound_policy" not in data


def test_persist_inbound_ref_does_not_unlock_reply_only(tmp_path: Path):
    locked = _throwaway(
        conversation_id="a:locked",
        outbound_policy="reply_only_until_customer_writes",
    )
    (tmp_path / "customer.json").write_text(json.dumps(locked), encoding="utf-8")
    with pytest.raises(StoredRefError, match="reply_only"):
        persist_inbound_ref(
            tmp_path,
            conversation_id="a:locked",
            conversation_type="personal",
            service_url="https://smba.trafficmanager.net/teams/",
            tenant_id=locked["tenant_id"],
            bot_app_id=BOT,
            filename_stem="unlocked",
        )
    assert not (tmp_path / "unlocked.json").exists()


def test_send_from_stored_ref_returns_activity_id_on_201():
    posted = {}

    async def poster(url, headers, body):
        posted["url"] = url
        posted["body"] = body
        posted["auth_prefix"] = str(headers.get("Authorization", ""))[:7]
        return 201, {"id": "activity-throwaway-1218"}

    async def run():
        return await send_from_stored_ref(
            _throwaway(),
            "STORED-REF-OWN-SEND",
            poster=poster,
            expected_bot_app_id=BOT,
            token="not-a-secret-for-test",
        )

    result = asyncio.run(run())
    assert result["success"] is True
    assert result["message_id"] == "activity-throwaway-1218"
    assert posted["url"].endswith("/v3/conversations/throwaway-1218/activities")
    assert posted["body"]["text"] == "STORED-REF-OWN-SEND"
    assert posted["body"]["from"]["id"] == f"28:{BOT}"
    assert posted["auth_prefix"] == "Bearer "


def test_send_from_stored_ref_http_400_is_not_success():
    async def poster(url, headers, body):
        return 400, {"error": {"message": "Invalid or unencrypted user ID"}}

    async def run():
        return await send_from_stored_ref(
            _throwaway(),
            "STORED-REF-OWN-SEND",
            poster=poster,
            expected_bot_app_id=BOT,
            token="not-a-secret-for-test",
        )

    result = asyncio.run(run())
    assert result.get("success") is not True
    assert "error" in result
    assert "400" in result["error"]


def test_send_from_stored_ref_missing_activity_id_is_not_success():
    async def poster(url, headers, body):
        return 201, {}

    async def run():
        return await send_from_stored_ref(
            _throwaway(),
            "STORED-REF-OWN-SEND",
            poster=poster,
            expected_bot_app_id=BOT,
            token="not-a-secret-for-test",
        )

    result = asyncio.run(run())
    assert result.get("success") is not True
    assert "activity id" in result["error"]


def test_send_from_stored_ref_poster_exception_is_not_success():
    async def poster(url, headers, body):
        raise TimeoutError("connector timeout")

    async def run():
        return await send_from_stored_ref(
            _throwaway(),
            "STORED-REF-OWN-SEND",
            poster=poster,
            expected_bot_app_id=BOT,
            token="not-a-secret-for-test",
        )

    result = asyncio.run(run())
    assert result.get("success") is not True
    assert "error" in result
    assert "TimeoutError" in result["error"] or "timeout" in result["error"].lower()


def test_send_from_stored_ref_status_zero_is_not_missing_activity_id():
    async def poster(url, headers, body):
        return 0, {"error": "stored-ref send: service host is not allowlisted"}

    async def run():
        return await send_from_stored_ref(
            _throwaway(),
            "STORED-REF-OWN-SEND",
            poster=poster,
            expected_bot_app_id=BOT,
            token="not-a-secret-for-test",
        )

    result = asyncio.run(run())
    assert result.get("success") is not True
    assert "activity id" not in result["error"]
    assert "failed (0)" in result["error"]


def _group_ref(**overrides):
    ref = _throwaway(
        kind="groupChat",
        conversation_id="19:group-throwaway",
        addressed_by="29:customer-roster",
        addressed_via="mention",
        last_inbound_activity_id="activity-inbound-1",
    )
    ref.update(overrides)
    return ref


def test_group_send_without_reply_is_not_a_first_post():
    async def poster(url, headers, body):
        raise AssertionError("must not POST a group first post")

    async def run():
        return await send_from_stored_ref(
            _group_ref(last_inbound_activity_id=""),
            "STORED-REF-OWN-SEND",
            poster=poster,
            expected_bot_app_id=BOT,
            token="not-a-secret-for-test",
        )

    result = asyncio.run(run())
    assert result.get("success") is not True
    assert "first post" in result["error"]


def test_group_send_replies_in_addressed_thread():
    posted = {}

    async def poster(url, headers, body):
        posted["body"] = body
        return 201, {"id": "activity-group-reply"}

    async def run():
        return await send_from_stored_ref(
            _group_ref(),
            "STORED-REF-OWN-SEND",
            poster=poster,
            expected_bot_app_id=BOT,
            token="not-a-secret-for-test",
            reply_to="activity-inbound-1",
        )

    result = asyncio.run(run())
    assert result["success"] is True
    assert result["message_id"] == "activity-group-reply"
    assert posted["body"]["replyToId"] == "activity-inbound-1"


def test_group_inbound_mention_addresses_bot():
    assert (
        group_inbound_addresses_bot(
            bot_app_id=BOT,
            entities=[{"type": "mention", "mentioned": {"id": f"28:{BOT}"}}],
        )
        == "mention"
    )


def test_group_inbound_reply_to_own_addresses_bot():
    assert (
        group_inbound_addresses_bot(
            bot_app_id=BOT,
            reply_to_id="activity-own-1",
            own_activity_ids=["activity-own-1"],
        )
        == "reply_to_own"
    )


def test_group_inbound_ambient_does_not_address_bot():
    assert (
        group_inbound_addresses_bot(
            bot_app_id=BOT,
            entities=[],
            reply_to_id="activity-someone-else",
            own_activity_ids=["activity-own-1"],
        )
        is None
    )


def test_persist_rejects_unmentioned_group(tmp_path: Path):
    with pytest.raises(StoredRefError, match="mention this bot or reply"):
        persist_inbound_ref(
            tmp_path,
            conversation_id="19:group-throwaway",
            conversation_type="groupChat",
            service_url="https://smba.trafficmanager.net/teams/",
            tenant_id="00000000-0000-0000-0000-000000000002",
            bot_app_id=BOT,
            user_id="29:customer-roster",
            inbound_activity_id="activity-ambient",
        )
    assert list(tmp_path.glob("*.json")) == []


def test_adapter_does_not_persist_unmentioned_group(tmp_path: Path, monkeypatch):
    from plugins.platforms.teams.adapter import TeamsAdapter

    adapter = object.__new__(TeamsAdapter)
    adapter._client_id = BOT
    adapter._tenant_id = "00000000-0000-0000-0000-000000000002"
    adapter._stored_refs = {}
    adapter._own_activity_ids = {}
    monkeypatch.setattr(adapter, "_stored_ref_dir", lambda: tmp_path)

    class _Conv:
        conversation_type = "groupChat"
        id = "19:group-throwaway"
        tenant_id = "00000000-0000-0000-0000-000000000002"

    class _Activity:
        service_url = "https://smba.trafficmanager.net/teams/"
        id = "activity-ambient"
        entities = []
        reply_to_id = None

    class _From:
        aad_object_id = "00000000-0000-0000-0000-000000000001"
        id = "29:customer-roster"
        name = "Customer"

    adapter._persist_inbound_stored_ref(_Activity(), _Conv(), _From())
    assert list(tmp_path.glob("*.json")) == []
    assert adapter._stored_refs == {}
