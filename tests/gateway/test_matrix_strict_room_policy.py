"""Strict Matrix room-allowlist policy regressions.

The default remains backward-compatible: DMs bypass ``allowed_rooms``. Operators
that enable ``allowed_rooms_apply_to_dms`` with a non-empty room list require an
explicit room ID for every message and invite path, including rooms Matrix
classifies as DMs.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig, load_gateway_config
from plugins.platforms.matrix.adapter import (
    MatrixAdapter,
    _apply_yaml_config,
    _standalone_send,
)


ALLOWED_ROOM = "!allowed:example.org"
OTHER_ROOM = "!other:example.org"


def _make_adapter(*, strict=True, allowed_rooms=None) -> MatrixAdapter:
    if allowed_rooms is None:
        allowed_rooms = [ALLOWED_ROOM]
    adapter = MatrixAdapter(
        PlatformConfig(
            enabled=True,
            token="syt_test_token",
            extra={
                "homeserver": "https://matrix.example.org",
                "user_id": "@hermes:example.org",
                "allowed_rooms": allowed_rooms,
                "allowed_rooms_apply_to_dms": strict,
            },
        )
    )
    adapter._text_batch_delay_seconds = 0
    adapter._startup_ts = time.time() - 10
    adapter.handle_message = AsyncMock()
    adapter._client = MagicMock()
    adapter._join_room_by_id = AsyncMock(return_value=True)
    adapter._record_dm_room = AsyncMock()
    return adapter


async def _drain_invite_tasks(adapter: MatrixAdapter) -> None:
    tasks = list(adapter._invite_join_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), ("true", True), ("yes", True), (False, False), ("false", False)],
)
def test_allowed_rooms_apply_to_dms_parses_yaml_booleans_and_strings(raw, expected):
    adapter = _make_adapter(strict=raw)
    assert adapter._allowed_rooms_apply_to_dms is expected


def test_top_level_matrix_yaml_seeds_dm_inclusive_policy_without_new_env_var():
    seeded = _apply_yaml_config(
        {},
        {
            "allowed_rooms": [ALLOWED_ROOM],
            "allowed_rooms_apply_to_dms": True,
        },
    )
    assert seeded == {
        "allowed_rooms": [ALLOWED_ROOM],
        "allowed_rooms_apply_to_dms": True,
    }


def test_room_policy_yaml_seed_omits_absent_keys():
    assert _apply_yaml_config({}, {"enabled": True}) is None


def test_real_gateway_load_preserves_legacy_env_only_room_allowlist(
    tmp_path, monkeypatch
):
    (tmp_path / "config.yaml").write_text(
        "matrix:\n  enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MATRIX_ALLOWED_ROOMS", "!env-only:example.org")

    config = load_gateway_config()
    adapter = MatrixAdapter(config.platforms[Platform.MATRIX])

    assert adapter._allowed_room_ids == {"!env-only:example.org"}
    assert adapter._allowed_rooms_apply_to_dms is False


def test_real_gateway_load_keeps_matrix_room_policy_profile_scoped(
    tmp_path, monkeypatch
):
    """A foreign process-global room list cannot replace this profile's YAML."""
    (tmp_path / "config.yaml").write_text(
        "matrix:\n"
        "  enabled: true\n"
        "  allowed_rooms: []\n"
        "  allowed_rooms_apply_to_dms: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MATRIX_ALLOWED_ROOMS", "!foreign:example.org")

    config = load_gateway_config()
    extra = config.platforms[Platform.MATRIX].extra

    assert extra["allowed_rooms"] == []
    assert extra["allowed_rooms_apply_to_dms"] is True
    adapter = MatrixAdapter(config.platforms[Platform.MATRIX])
    assert adapter._allowed_room_ids == set()
    assert adapter._strict_room_policy_allows("!any:example.org") is True


@pytest.mark.asyncio
async def test_strict_policy_rejects_unlisted_dm_but_allows_listed_dm():
    adapter = _make_adapter()
    adapter._resolve_room_identity = AsyncMock(
        return_value=SimpleNamespace(chat_type="dm")
    )

    assert await adapter._is_allowed_matrix_room_event(OTHER_ROOM) is False
    assert await adapter._is_allowed_matrix_room_event(ALLOWED_ROOM) is True


@pytest.mark.asyncio
async def test_strict_policy_blocks_unlisted_dm_on_real_message_intake():
    adapter = _make_adapter()
    adapter._resolve_room_identity = AsyncMock(
        return_value=SimpleNamespace(chat_type="dm")
    )
    event = SimpleNamespace(
        room_id=OTHER_ROOM,
        sender="@alice:example.org",
        event_id="$unlisted",
    )

    await adapter._on_room_message(event)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_policy_with_empty_room_list_remains_unrestricted():
    adapter = _make_adapter(allowed_rooms=[])
    adapter._resolve_room_identity = AsyncMock(
        return_value=SimpleNamespace(chat_type="dm")
    )

    assert await adapter._is_allowed_matrix_room_event(OTHER_ROOM) is True


@pytest.mark.asyncio
async def test_default_policy_preserves_dm_exemption():
    adapter = _make_adapter(strict=False)
    adapter._resolve_room_identity = AsyncMock(
        return_value=SimpleNamespace(chat_type="dm")
    )

    assert await adapter._is_allowed_matrix_room_event(OTHER_ROOM) is True


@pytest.mark.asyncio
async def test_strict_policy_blocks_unlisted_invite_at_join_sink():
    adapter = _make_adapter()

    adapter._schedule_invite_join(OTHER_ROOM, is_direct=True, inviter="@alice:example.org")
    await _drain_invite_tasks(adapter)

    adapter._join_room_by_id.assert_not_awaited()
    adapter._record_dm_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_policy_allows_listed_invite_at_join_sink():
    adapter = _make_adapter()

    adapter._schedule_invite_join(ALLOWED_ROOM, is_direct=True, inviter="@alice:example.org")
    await _drain_invite_tasks(adapter)

    adapter._join_room_by_id.assert_awaited_once_with(ALLOWED_ROOM)
    adapter._record_dm_room.assert_awaited_once_with(
        ALLOWED_ROOM, "@alice:example.org"
    )


@pytest.mark.asyncio
async def test_strict_policy_blocks_unlisted_pending_invite_after_restart():
    adapter = _make_adapter()
    sync_data = {"rooms": {"invite": {OTHER_ROOM: {}}}}

    adapter._schedule_pending_invite_joins(sync_data)
    await _drain_invite_tasks(adapter)

    adapter._join_room_by_id.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allowed_room", "authorized_inviter", "allow_all", "should_join"),
    [
        (True, True, False, True),
        (True, False, False, False),
        (False, True, False, False),
        (False, False, True, False),
    ],
)
async def test_live_invite_room_and_inviter_policies_compose(
    monkeypatch, allowed_room, authorized_inviter, allow_all, should_join
):
    """Room IDs remain independent alongside inviter authorization (#87258)."""
    adapter = _make_adapter()
    adapter._allowed_user_ids = {"@authorized:example.org"}
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true" if allow_all else "false")
    room_id = ALLOWED_ROOM if allowed_room else OTHER_ROOM
    inviter = (
        "@authorized:example.org" if authorized_inviter else "@intruder:example.org"
    )
    event = SimpleNamespace(
        room_id=room_id,
        sender=inviter,
        content=SimpleNamespace(is_direct=True),
    )

    await adapter._on_invite(event)
    await _drain_invite_tasks(adapter)

    if should_join:
        adapter._join_room_by_id.assert_awaited_once_with(room_id)
    else:
        adapter._join_room_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_common_join_sink_rejects_unlisted_room_even_if_already_joined():
    adapter = _make_adapter()
    adapter._join_room_by_id = MatrixAdapter._join_room_by_id.__get__(adapter)
    adapter._joined_rooms.add(OTHER_ROOM)

    assert await adapter._join_room_by_id(OTHER_ROOM) is False
    adapter._client.join_room.assert_not_called()


@pytest.mark.asyncio
async def test_strict_outbound_room_guard_blocks_every_direct_room_sink():
    adapter = _make_adapter()
    adapter._client.send_message_event = AsyncMock(return_value="$sent")
    adapter._client.upload_media = AsyncMock(return_value="mxc://example/media")
    adapter._client.redact = AsyncMock()
    adapter._client.invite_user = AsyncMock()
    adapter._client.messages = AsyncMock(return_value={"chunk": []})
    adapter._client.set_fully_read_marker = AsyncMock()
    adapter._client.set_typing = AsyncMock()

    send_result = await adapter.send(OTHER_ROOM, "secret")
    edit_result = await adapter.edit_message(OTHER_ROOM, "$event", "edited")
    media_result = await adapter._upload_and_send(
        OTHER_ROOM, b"secret", "secret.txt", "text/plain", "m.file"
    )
    reaction_result = await adapter._send_reaction(OTHER_ROOM, "$event", "✅")
    redact_result = await adapter.redact_message(OTHER_ROOM, "$event")
    history_result = await adapter.fetch_history(OTHER_ROOM)
    invite_result = await adapter.invite_user(OTHER_ROOM, "@bob:example.org")
    receipt_result = await adapter.send_read_receipt(OTHER_ROOM, "$event")
    await adapter.send_typing(OTHER_ROOM)
    await adapter.stop_typing(OTHER_ROOM)

    assert send_result.success is False
    assert edit_result.success is False
    assert media_result.success is False
    assert reaction_result is None
    assert redact_result is False
    assert history_result == []
    assert invite_result is False
    assert receipt_result is False
    adapter._client.send_message_event.assert_not_awaited()
    adapter._client.upload_media.assert_not_awaited()
    adapter._client.redact.assert_not_awaited()
    adapter._client.invite_user.assert_not_awaited()
    adapter._client.messages.assert_not_awaited()
    adapter._client.set_fully_read_marker.assert_not_awaited()
    adapter._client.set_typing.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_policy_disables_room_creation_before_client_call():
    adapter = _make_adapter()
    adapter._client.create_room = AsyncMock(return_value="!new:example.org")

    result = await adapter.create_room(name="escape")

    assert result is None
    adapter._client.create_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_policy_blocks_unlisted_chat_info_before_state_lookup():
    adapter = _make_adapter()
    adapter._resolve_room_identity = AsyncMock(
        side_effect=AssertionError("unlisted room metadata was queried")
    )

    result = await adapter.get_chat_info(OTHER_ROOM)

    assert result == {
        "name": OTHER_ROOM,
        "type": "group",
        "chat_id": OTHER_ROOM,
        "allowed": False,
    }
    adapter._resolve_room_identity.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_policy_resolves_allowlisted_chat_info():
    adapter = _make_adapter()
    adapter._resolve_room_identity = AsyncMock(
        return_value=SimpleNamespace(chat_type="group", display_name="Allowed room")
    )

    result = await adapter.get_chat_info(ALLOWED_ROOM)

    assert result == {"name": "Allowed room", "type": "group"}
    adapter._resolve_room_identity.assert_awaited_once_with(ALLOWED_ROOM)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strict", "allowed_rooms"),
    [(False, [ALLOWED_ROOM]), (True, [])],
)
async def test_room_management_preserves_default_and_empty_list_behavior(
    strict, allowed_rooms
):
    adapter = _make_adapter(strict=strict, allowed_rooms=allowed_rooms)
    adapter._client.create_room = AsyncMock(return_value="!new:example.org")
    adapter._resolve_room_identity = AsyncMock(
        return_value=SimpleNamespace(chat_type="dm", display_name="Other room")
    )

    created = await adapter.create_room(name="still allowed")
    info = await adapter.get_chat_info(OTHER_ROOM)

    assert created == "!new:example.org"
    assert info == {"name": "Other room", "type": "dm"}
    adapter._client.create_room.assert_awaited_once()
    adapter._resolve_room_identity.assert_awaited_once_with(OTHER_ROOM)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strict", "allowed_rooms"),
    [(False, [ALLOWED_ROOM]), (True, [])],
)
async def test_outbound_guard_preserves_default_and_empty_list_behavior(
    strict, allowed_rooms
):
    adapter = _make_adapter(strict=strict, allowed_rooms=allowed_rooms)
    adapter._client.send_message_event = AsyncMock(return_value="$sent")

    result = await adapter.send(OTHER_ROOM, "still allowed")

    assert result.success is True
    adapter._client.send_message_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_strict_policy_blocks_standalone_send_before_network():
    config = PlatformConfig(
        enabled=True,
        token="syt_test_token",
        extra={
            "homeserver": "https://matrix.example.org",
            "allowed_rooms": [ALLOWED_ROOM],
            "allowed_rooms_apply_to_dms": True,
        },
    )

    result = await _standalone_send(config, OTHER_ROOM, "secret")

    assert result == {"error": "Matrix room is not allowed"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strict", "allowed_rooms"),
    [(False, [ALLOWED_ROOM]), (True, [])],
)
async def test_reaction_guard_preserves_default_and_empty_list_behavior(
    strict, allowed_rooms
):
    adapter = _make_adapter(strict=strict, allowed_rooms=allowed_rooms)
    event = SimpleNamespace(
        room_id=OTHER_ROOM,
        sender="@alice:example.org",
        event_id="$default-reaction",
        content={
            "m.relates_to": {
                "event_id": "$ordinary-event",
                "key": "✅",
            }
        },
    )

    await adapter._on_reaction(event)

    assert "$default-reaction" in adapter._processed_events_set


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt_kind", ["approval", "model", "choice"])
async def test_unlisted_room_reaction_cannot_mutate_any_prompt_before_dedup(
    monkeypatch, prompt_kind
):
    adapter = _make_adapter()
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    callback = AsyncMock(return_value="done")
    prompt = SimpleNamespace(
        chat_id=OTHER_ROOM,
        message_id="$prompt",
        session_key="matrix:room:user",
        requester_user_id="@alice:example.org",
        expires_at=time.monotonic() + 60,
        resolved=False,
        bot_reaction_events={},
        choices=(
            {"1️⃣": ("model", "provider")}
            if prompt_kind == "model"
            else {"1️⃣": "choice"}
        ),
        on_model_selected=callback,
        on_choice_selected=callback,
    )
    target = {
        "approval": adapter._approval_prompts_by_event,
        "model": adapter._model_picker_prompts_by_event,
        "choice": adapter._choice_picker_prompts_by_event,
    }[prompt_kind]
    target["$prompt"] = prompt
    event = SimpleNamespace(
        room_id=OTHER_ROOM,
        sender="@alice:example.org",
        event_id="$reaction",
        content={
            "m.relates_to": {
                "event_id": "$prompt",
                "key": "✅" if prompt_kind == "approval" else "1️⃣",
            }
        },
    )

    with patch(
        "tools.approval.resolve_gateway_approval",
        side_effect=AssertionError("approval resolver reached"),
    ):
        await adapter._on_reaction(event)

    assert prompt.resolved is False
    assert target["$prompt"] is prompt
    callback.assert_not_awaited()
    assert "$reaction" not in adapter._processed_events_set
