from __future__ import annotations

import asyncio
import inspect
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent import secret_scope
from gateway.config import Platform, PlatformConfig, load_gateway_config
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import build_session_key
from plugins.platforms.slack.adapter import SlackAdapter
from plugins.platforms.slack.plan_cards import (
    is_user_task_id,
    sign_private_metadata,
    verify_private_metadata,
)


def _adapter(
    tmp_path,
    *,
    secret: str | None = "signing-secret",
    enabled: bool = True,
) -> SlackAdapter:
    config = PlatformConfig(
        enabled=True,
        token="xoxb-test",
        extra={"native_plan_cards": enabled},
    )
    with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
        adapter = SlackAdapter(config)
    adapter._app = MagicMock()
    adapter._team_clients = {"T1": AsyncMock()}
    adapter._channel_team = {"C1": "T1"}
    adapter._plan_signing_secret_override = secret
    return adapter


@pytest.mark.parametrize(
    ("relative_home", "expected_profile"),
    [
        ((".hermes",), None),
        (("hermes-root", "profiles", "secondary"), "secondary"),
        (("profiles",), None),
    ],
)
def test_adapter_derives_profile_only_from_profile_home_shape(
    tmp_path, relative_home, expected_profile
) -> None:
    home = tmp_path.joinpath(*relative_home)
    config = PlatformConfig(
        enabled=True,
        token="xoxb-test",
        extra={"native_plan_cards": True},
    )
    with patch("hermes_constants.get_hermes_home", return_value=home):
        adapter = SlackAdapter(config)

    assert adapter._plan_profile == expected_profile
    assert adapter._plan_store.state_path == home / "gateway" / "slack_plan_cards.json"


def _state(
    adapter: SlackAdapter,
    todos=None,
    *,
    route_user_id: str = "U-owner",
    chat_type: str = "group",
):
    return adapter.record_desired_plan_snapshot(
        session_key="sk",
        session_id="sid",
        team_id="T1",
        channel_id="C1",
        thread_ts="10.0",
        route_user_id=route_user_id,
        chat_type=chat_type,
        todos=todos or [{"id": "a", "content": "A", "status": "pending"}],
    )


def _converged_state_with_retired_anchor(adapter: SlackAdapter):
    first = _state(adapter)
    first_create = adapter._plan_store.prepare_create("sk", expected_route=first)
    assert adapter._plan_store.mark_applied(
        "sk", revision=first["desired_revision"], snapshot_hash=first["desired_hash"],
        message_ts="20.0", expected_message_ts="",
        expected_client_msg_id=first_create["client_msg_id"],
    )
    moved = adapter.record_desired_plan_snapshot(
        session_key="sk", session_id="sid-2", team_id="T1", channel_id="C2",
        thread_ts="11.0", route_user_id="U-owner", chat_type="group",
        todos=[{"id": "b", "content": "Moved", "status": "pending"}],
    )
    moved_create = adapter._plan_store.prepare_create("sk", expected_route=moved)
    assert adapter._plan_store.mark_applied(
        "sk", revision=moved["desired_revision"], snapshot_hash=moved["desired_hash"],
        message_ts="30.0", expected_message_ts="",
        expected_client_msg_id=moved_create["client_msg_id"],
    )
    return adapter._plan_store.list_retired("sk")[0]


async def _reconcile(
    adapter: SlackAdapter,
    session_key: str,
    _scheduled_revision: int | None = None,
) -> bool:
    """Drive the adapter-owned worker until this session settles once."""
    before = adapter._plan_store.get_session(session_key) or {}
    before_retry = int(before.get("retry_count") or 0)
    adapter._running = True
    adapter._start_plan_reconcile_worker()
    adapter.request_plan_reconcile()
    result = False
    for _ in range(200):
        state = adapter._plan_store.get_session(session_key) or {}
        dirty_keys = {
            str(item.get("session_key") or "")
            for item in adapter._plan_store.list_dirty()
        }
        if session_key not in dirty_keys:
            result = bool(state) and (
                int(state.get("applied_revision") or 0)
                >= int(state.get("desired_revision") or 0)
                and not state.get("retired_anchors")
            )
            break
        if int(state.get("retry_count") or 0) > before_retry:
            break
        await asyncio.sleep(0.005)
    await adapter._stop_plan_reconcile_worker()
    return result


def test_plan_wake_is_event_only_and_adapter_has_one_worker_field(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    source = inspect.getsource(SlackAdapter.request_plan_reconcile)
    assert "create_task" not in source
    assert "reconcile_plan_card" not in source
    assert not hasattr(adapter, "_plan_reconcile_tasks")
    assert not hasattr(adapter, "_plan_reconcile_locks")
    assert hasattr(adapter, "_plan_reconcile_task")

    adapter.request_plan_reconcile()
    assert adapter._plan_reconcile_task is None
    assert adapter._plan_reconcile_wakeup.is_set()


@pytest.mark.asyncio
async def test_plan_worker_serializes_all_session_slack_io(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    adapter._running = True
    _state(adapter)
    adapter.record_desired_plan_snapshot(
        session_key="other", session_id="sid-2", team_id="T1", channel_id="C1",
        thread_ts="11.0", route_user_id="U-owner", chat_type="group",
        todos=[{"id": "b", "content": "B", "status": "pending"}],
    )
    active = 0
    max_active = 0

    async def post(**kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return {"ts": "20.0" if kwargs.get("thread_ts") == "10.0" else "21.0"}

    adapter._team_clients["T1"].chat_postMessage = AsyncMock(side_effect=post)
    for _ in range(10):
        adapter.request_plan_reconcile()
    adapter._start_plan_reconcile_worker()
    for _ in range(100):
        if not adapter._plan_store.list_dirty():
            break
        await asyncio.sleep(0.01)
    await adapter.disconnect()

    assert max_active == 1
    assert adapter._plan_store.list_dirty() == []


@pytest.mark.asyncio
async def test_start_reuses_one_worker_then_reconnect_advances_generation(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    adapter._running = True

    adapter._start_plan_reconcile_worker()
    first_task = adapter._plan_reconcile_task
    first_generation = adapter._plan_reconcile_generation
    adapter._start_plan_reconcile_worker()

    assert adapter._plan_reconcile_task is first_task
    assert adapter._plan_reconcile_generation == first_generation

    await adapter._stop_plan_reconcile_worker()
    adapter._start_plan_reconcile_worker()
    assert adapter._plan_reconcile_task is not first_task
    assert adapter._plan_reconcile_generation == first_generation + 1
    await adapter._stop_plan_reconcile_worker()


@pytest.mark.asyncio
async def test_old_queued_wake_after_reconnect_only_wakes_new_worker(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    state = _state(adapter)
    adapter._running = True
    adapter._team_clients["T1"].chat_postMessage = AsyncMock(
        return_value={"ts": "20.0"}
    )

    adapter._start_plan_reconcile_worker()
    old_generation = adapter._plan_reconcile_generation
    queued_old_wake = adapter.request_plan_reconcile
    await adapter._stop_plan_reconcile_worker()
    assert adapter._plan_reconcile_task is None

    adapter._start_plan_reconcile_worker()
    assert adapter._plan_reconcile_generation == old_generation + 1
    queued_old_wake()
    for _ in range(100):
        persisted = adapter._plan_store.get_session("sk")
        if persisted["applied_revision"] == state["desired_revision"]:
            break
        await asyncio.sleep(0.01)
    await adapter._stop_plan_reconcile_worker()

    adapter._team_clients["T1"].chat_postMessage.assert_awaited_once()
    assert adapter._plan_store.get_session("sk")["message_ts"] == "20.0"


@pytest.mark.asyncio
async def test_wake_at_quiescent_boundary_is_not_lost(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    adapter._running = True
    client = adapter._team_clients["T1"]
    client.chat_postMessage = AsyncMock(return_value={"ts": "20.0"})
    original_list_dirty = adapter._plan_store.list_dirty
    first_scan = True

    def list_dirty(*args, **kwargs):
        nonlocal first_scan
        if first_scan:
            first_scan = False
            _state(adapter)
            adapter.request_plan_reconcile()
            return []
        return original_list_dirty(*args, **kwargs)

    adapter._plan_store.list_dirty = MagicMock(side_effect=list_dirty)
    adapter._start_plan_reconcile_worker()
    for _ in range(100):
        state = adapter._plan_store.get_session("sk")
        if state and state["applied_revision"] == state["desired_revision"]:
            break
        await asyncio.sleep(0.01)
    await adapter._stop_plan_reconcile_worker()

    client.chat_postMessage.assert_awaited_once()
    state = adapter._plan_store.get_session("sk")
    assert state["applied_revision"] == state["desired_revision"]


@pytest.mark.asyncio
async def test_disconnect_cancels_single_worker_and_blocks_post_return_commit(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    state = _state(adapter)
    adapter._running = True
    adapter._stop_socket_mode_handler = AsyncMock()
    adapter._release_platform_lock = MagicMock()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_post(**_kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {"ts": "20.0"}

    adapter._team_clients["T1"].chat_postMessage = AsyncMock(side_effect=blocked_post)
    adapter._start_plan_reconcile_worker()
    await started.wait()
    disconnect = asyncio.create_task(adapter.disconnect())
    await asyncio.sleep(0)
    assert not disconnect.done()
    release.set()
    await disconnect

    persisted = adapter._plan_store.get_session("sk")
    assert persisted["desired_revision"] == state["desired_revision"]
    assert persisted["applied_revision"] == 0
    assert persisted["message_ts"] == "20.0"
    assert adapter._plan_reconcile_task is None
    assert type(adapter._plan_store)(tmp_path).get_session("sk")["message_ts"] == "20.0"

    adapter._team_clients["T1"].chat_postMessage = AsyncMock(return_value={"ts": "30.0"})
    adapter._team_clients["T1"].chat_update = AsyncMock(return_value={"ts": "20.0"})
    adapter._app = MagicMock()
    adapter._running = True
    adapter._start_plan_reconcile_worker()
    for _ in range(100):
        persisted = adapter._plan_store.get_session("sk")
        if persisted["applied_revision"] == state["desired_revision"]:
            break
        await asyncio.sleep(0.01)
    await adapter._stop_plan_reconcile_worker()
    adapter._team_clients["T1"].chat_postMessage.assert_not_awaited()
    adapter._team_clients["T1"].chat_update.assert_awaited_once()
    assert adapter._plan_store.get_session("sk")["message_ts"] == "20.0"


@pytest.mark.asyncio
async def test_cancelled_create_route_change_persists_retired_lineage_before_return(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter)
    adapter._running = True
    adapter._stop_socket_mode_handler = AsyncMock()
    adapter._release_platform_lock = MagicMock()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_post(**_kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {"ts": "20.0"}

    adapter._team_clients["T1"].chat_postMessage = AsyncMock(side_effect=blocked_post)
    adapter._start_plan_reconcile_worker()
    await started.wait()
    moved = adapter.record_desired_plan_snapshot(
        session_key="sk", session_id="sid-2", team_id="T1", channel_id="C2",
        thread_ts="11.0", route_user_id="U-owner", chat_type="group",
        todos=[{"id": "b", "content": "Moved", "status": "pending"}],
    )
    disconnect = asyncio.create_task(adapter.disconnect())
    await asyncio.sleep(0)
    release.set()
    await disconnect

    persisted = type(adapter._plan_store)(tmp_path).get_session("sk")
    assert persisted["channel_id"] == "C2"
    assert persisted["message_ts"] == ""
    assert [anchor["message_ts"] for anchor in persisted["retired_anchors"]] == ["20.0"]

    restarted = _adapter(tmp_path)
    client = restarted._team_clients["T1"]
    client.chat_delete = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ts": "30.0"})
    restarted._running = True
    restarted._start_plan_reconcile_worker()
    for _ in range(100):
        current = restarted._plan_store.get_session("sk")
        if current["applied_revision"] == moved["desired_revision"]:
            break
        await asyncio.sleep(0.01)
    await restarted._stop_plan_reconcile_worker()

    client.chat_delete.assert_awaited_once_with(channel="C1", ts="20.0")
    client.chat_postMessage.assert_awaited_once()
    assert client.chat_postMessage.call_args.kwargs["channel"] == "C2"
    assert restarted._plan_store.get_session("sk")["message_ts"] == "30.0"


@pytest.mark.asyncio
async def test_create_lineage_store_failure_recovers_by_client_id_without_duplicate(
    tmp_path, caplog
) -> None:
    adapter = _adapter(tmp_path)
    state = _state(adapter)
    adapter._running = True
    adapter._stop_socket_mode_handler = AsyncMock()
    adapter._release_platform_lock = MagicMock()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_post(**_kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {"ts": "20.0"}

    client = adapter._team_clients["T1"]
    client.chat_postMessage = AsyncMock(side_effect=blocked_post)
    client.chat_delete = AsyncMock()
    adapter._plan_store.record_create_result = MagicMock(
        side_effect=OSError("disk unavailable")
    )
    adapter._start_plan_reconcile_worker()
    await started.wait()
    disconnect = asyncio.create_task(adapter.disconnect())
    await asyncio.sleep(0)
    with caplog.at_level("ERROR", logger="plugins.platforms.slack.adapter"):
        release.set()
        await disconnect

    persisted = type(adapter._plan_store)(tmp_path).get_session("sk")
    assert persisted["message_ts"] == ""
    assert persisted["client_msg_id"]
    assert persisted["applied_revision"] == 0
    client.chat_delete.assert_not_awaited()
    assert "failed to persist its lineage" in caplog.text

    restarted = _adapter(tmp_path)
    recovered_client = restarted._team_clients["T1"]
    recovered_client.conversations_replies = AsyncMock(return_value={"messages": [{
        "ts": "20.0", "client_msg_id": persisted["client_msg_id"],
    }]})
    recovered_client.chat_postMessage = AsyncMock(return_value={"ts": "21.0"})
    recovered_client.chat_update = AsyncMock(return_value={"ts": "20.0"})
    assert await _reconcile(restarted, "sk", state["desired_revision"])

    recovered_client.chat_postMessage.assert_not_awaited()
    assert restarted._plan_store.get_session("sk")["message_ts"] == "20.0"


@pytest.mark.asyncio
async def test_disconnect_blocks_retry_commit_after_cancelled_apply_returns(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter)
    adapter._running = True
    adapter._stop_socket_mode_handler = AsyncMock()
    adapter._release_platform_lock = MagicMock()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_post(**_kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        raise RuntimeError("timeout")

    adapter._team_clients["T1"].chat_postMessage = AsyncMock(side_effect=blocked_post)
    adapter._start_plan_reconcile_worker()
    await started.wait()
    disconnect = asyncio.create_task(adapter.disconnect())
    await asyncio.sleep(0)
    release.set()
    await disconnect

    persisted = adapter._plan_store.get_session("sk")
    assert persisted["retry_count"] == 0
    assert persisted["applied_revision"] == 0


@pytest.mark.asyncio
async def test_disconnect_blocks_missing_anchor_reset_after_cancelled_update(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    first = _state(adapter)
    prepared = adapter._plan_store.prepare_create("sk", expected_route=first)
    assert adapter._plan_store.mark_applied(
        "sk", revision=first["desired_revision"], snapshot_hash=first["desired_hash"],
        message_ts="20.0", expected_message_ts="",
        expected_client_msg_id=prepared["client_msg_id"],
    )
    _state(adapter, [{"id": "a", "content": "changed", "status": "pending"}])
    adapter._running = True
    adapter._stop_socket_mode_handler = AsyncMock()
    adapter._release_platform_lock = MagicMock()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_update(**_kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        raise RuntimeError("message_not_found")

    adapter._team_clients["T1"].chat_update = AsyncMock(side_effect=blocked_update)
    adapter._start_plan_reconcile_worker()
    await started.wait()
    disconnect = asyncio.create_task(adapter.disconnect())
    await asyncio.sleep(0)
    release.set()
    await disconnect

    persisted = adapter._plan_store.get_session("sk")
    assert persisted["message_ts"] == "20.0"
    assert persisted["client_msg_id"] == prepared["client_msg_id"]
    assert persisted["retry_count"] == 0


@pytest.mark.asyncio
async def test_disconnect_blocks_retired_retry_after_cancelled_readback(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    first = _state(adapter)
    adapter._plan_store.prepare_create("sk", expected_route=first)
    adapter.record_desired_plan_snapshot(
        session_key="sk", session_id="sid-2", team_id="T1", channel_id="C2",
        thread_ts="11.0", route_user_id="U-owner", chat_type="group",
        todos=[{"id": "b", "content": "Moved", "status": "pending"}],
    )
    adapter._running = True
    adapter._stop_socket_mode_handler = AsyncMock()
    adapter._release_platform_lock = MagicMock()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_history(**_kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {"messages": []}

    adapter._team_clients["T1"].conversations_replies = AsyncMock(
        side_effect=blocked_history
    )
    adapter._start_plan_reconcile_worker()
    await started.wait()
    disconnect = asyncio.create_task(adapter.disconnect())
    await asyncio.sleep(0)
    release.set()
    await disconnect

    retired = adapter._plan_store.get_session("sk")["retired_anchors"]
    assert len(retired) == 1
    assert retired[0]["retry_count"] == 0
    assert retired[0]["next_retry_at"] == 0


@pytest.mark.asyncio
async def test_conflict_is_retired_before_cleanup_and_cancel_keeps_it(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter)
    adapter._running = True
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def post(**_kwargs):
        competing = type(adapter._plan_store)(tmp_path)
        prepared = competing.get_session("sk")
        assert competing.record_create_result(
            "sk", expected_route=prepared,
            client_msg_id=prepared["client_msg_id"], message_ts="30.0",
        ) == "current"
        return {"ts": "20.0"}

    async def blocked_delete(**_kwargs):
        retired = adapter._plan_store.get_session("sk")["retired_anchors"]
        assert [anchor["message_ts"] for anchor in retired] == ["20.0"]
        cleanup_started.set()
        await release_cleanup.wait()

    client = adapter._team_clients["T1"]
    client.chat_postMessage = AsyncMock(side_effect=post)
    client.chat_delete = AsyncMock(side_effect=blocked_delete)
    adapter._start_plan_reconcile_worker()
    await cleanup_started.wait()
    await adapter.disconnect()

    assert [
        anchor["message_ts"]
        for anchor in adapter._plan_store.get_session("sk")["retired_anchors"]
    ] == ["20.0"]


@pytest.mark.asyncio
async def test_conflict_retirement_store_failure_prevents_remote_cleanup(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter)
    adapter._running = True
    client = adapter._team_clients["T1"]

    async def post(**_kwargs):
        competing = type(adapter._plan_store)(tmp_path)
        prepared = competing.get_session("sk")
        assert competing.record_create_result(
            "sk", expected_route=prepared,
            client_msg_id=prepared["client_msg_id"], message_ts="30.0",
        ) == "current"
        return {"ts": "20.0"}

    client.chat_postMessage = AsyncMock(side_effect=post)
    client.chat_delete = AsyncMock()
    client.chat_update = AsyncMock()
    adapter._plan_store.retire_orphan_anchor = MagicMock(side_effect=OSError("disk"))

    adapter._start_plan_reconcile_worker()
    for _ in range(100):
        if adapter._plan_reconcile_task and adapter._plan_reconcile_task.done():
            break
        if adapter._plan_store.get_session("sk")["retry_count"]:
            break
        await asyncio.sleep(0.01)
    await adapter.disconnect()

    client.chat_delete.assert_not_awaited()
    client.chat_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_creates_updates_and_dedupes_hash(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    client = adapter._team_clients["T1"]
    client.chat_postMessage = AsyncMock(return_value={"ts": "20.0"})
    client.chat_update = AsyncMock(return_value={"ts": "20.0"})

    first = _state(adapter)
    assert await _reconcile(adapter, "sk", first["desired_revision"])
    client.chat_postMessage.assert_awaited_once()
    posted = client.chat_postMessage.call_args.kwargs
    assert posted["thread_ts"] == "10.0"
    assert posted["blocks"][0]["type"] == "plan"

    same = _state(adapter)
    assert await _reconcile(adapter, "sk", same["desired_revision"])
    client.chat_update.assert_not_awaited()
    persisted = adapter._plan_store.get_session("sk")
    assert persisted["applied_revision"] == same["desired_revision"]
    assert persisted["applied_render_revision"] == first["desired_revision"]

    changed = _state(adapter, [{"id": "a", "content": "A", "status": "completed"}])
    assert await _reconcile(adapter, "sk", changed["desired_revision"])
    client.chat_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_hash_dedupe_keeps_existing_card_actions_valid(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    client = adapter._team_clients["T1"]
    client.chat_postMessage = AsyncMock(return_value={"ts": "20.0"})
    first = _state(adapter, [{"id": "user:a", "content": "A", "status": "pending"}])
    assert await _reconcile(adapter, "sk", first["desired_revision"])
    same = _state(adapter, [{"id": "user:a", "content": "A", "status": "pending"}])
    assert await _reconcile(adapter, "sk", same["desired_revision"])
    client.chat_update.assert_not_awaited()

    events = []
    async def handle(event):
        events.append(event)

    adapter.set_message_handler(handle)
    adapter._is_interactive_user_authorized = MagicMock(return_value=True)
    await adapter._handle_plan_action(AsyncMock(), {
        "team": {"id": "T1"}, "channel": {"id": "C1"},
        "message": {"ts": "20.0", "thread_ts": "10.0"},
        "user": {"id": "U-owner"}, "actions": [{"action_ts": "same-hash"}],
    }, {
        "action_id": "hermes_plan_complete",
        "block_id": "hermes-plan-controls-r1-" + first["desired_hash"][:10],
        "selected_options": [{"value": "user:a"}],
    })
    await asyncio.sleep(0)
    assert len(events) == 1
    assert events[0].metadata["slack_plan_action"]["revision"] == same["desired_revision"]


@pytest.mark.asyncio
async def test_reconcile_native_failure_falls_back_and_missing_update_reanchors(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    client = adapter._team_clients["T1"]
    client.chat_postMessage = AsyncMock(return_value={"ts": "20.0"})
    first = _state(adapter)
    await _reconcile(adapter, "sk", first["desired_revision"])

    changed = _state(adapter, [{"id": "a", "content": "changed", "status": "pending"}])
    client.chat_update = AsyncMock(side_effect=[RuntimeError("invalid_blocks"), {"ts": "20.0"}])
    assert await _reconcile(adapter, "sk", changed["desired_revision"])
    assert client.chat_update.await_count == 2
    assert client.chat_update.call_args_list[1].kwargs["blocks"][0]["type"] == "section"

    newer = _state(adapter, [{"id": "b", "content": "new", "status": "pending"}])
    client.chat_update = AsyncMock(side_effect=RuntimeError("message_not_found"))
    client.chat_postMessage.reset_mock()
    client.chat_postMessage.return_value = {"ts": "30.0"}
    assert await _reconcile(adapter, "sk", newer["desired_revision"])
    client.chat_postMessage.assert_awaited_once()
    assert adapter._plan_store.get_session("sk")["message_ts"] == "30.0"


@pytest.mark.asyncio
async def test_inflight_stale_create_adopts_anchor_and_updates_latest_without_duplicate(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    client = adapter._team_clients["T1"]
    post_started = asyncio.Event()
    release_post = asyncio.Event()

    async def delayed_post(**_kwargs):
        post_started.set()
        await release_post.wait()
        return {"ts": "20.0"}

    client.chat_postMessage = AsyncMock(side_effect=delayed_post)
    client.chat_update = AsyncMock(return_value={"ts": "20.0"})
    first = _state(adapter)
    reconcile = asyncio.create_task(
        _reconcile(adapter, "sk", first["desired_revision"])
    )
    await post_started.wait()
    latest = _state(adapter, [{"id": "b", "content": "Latest", "status": "pending"}])
    release_post.set()

    assert await reconcile
    assert await _reconcile(adapter, "sk", latest["desired_revision"])
    client.chat_postMessage.assert_awaited_once()
    client.chat_update.assert_awaited_once()
    assert client.chat_update.call_args.kwargs["ts"] == "20.0"
    assert client.chat_update.call_args.kwargs["blocks"][0]["tasks"][0]["task_id"] == "b"

    restarted = type(adapter._plan_store)(tmp_path)
    state = restarted.get_session("sk")
    assert state["message_ts"] == "20.0"
    assert state["applied_revision"] == latest["desired_revision"]
    assert restarted.lookup_route("T1", "C1", "20.0")["session_key"] == "sk"


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_ts", ["10.0", ""])
async def test_restart_recovers_attempted_create_by_client_msg_id_before_post(tmp_path, thread_ts) -> None:
    adapter = _adapter(tmp_path)
    state = adapter.record_desired_plan_snapshot(
        session_key="sk", session_id="sid", team_id="T1", channel_id="C1",
        thread_ts=thread_ts, route_user_id="U-owner", chat_type="group",
        todos=[{"id": "a", "content": "A", "status": "pending"}],
    )
    prepared = adapter._plan_store.prepare_create("sk", expected_route=state)
    client = adapter._team_clients["T1"]
    recovered = {
        "ts": "20.0", "client_msg_id": prepared["client_msg_id"], "user": "U-bot",
    }
    client.conversations_replies = AsyncMock(return_value={"messages": [recovered]})
    client.conversations_history = AsyncMock(return_value={"messages": [recovered]})
    client.chat_postMessage = AsyncMock()
    client.chat_update = AsyncMock(return_value={"ts": "20.0"})
    adapter._team_bot_user_ids = {"T1": "U-bot"}

    assert await _reconcile(adapter, "sk", state["desired_revision"])
    client.chat_postMessage.assert_not_awaited()
    client.chat_update.assert_awaited_once()
    if thread_ts:
        client.conversations_replies.assert_awaited_once()
        client.conversations_history.assert_not_awaited()
    else:
        client.conversations_history.assert_awaited_once()
        client.conversations_replies.assert_not_awaited()
    assert adapter._plan_store.get_session("sk")["message_ts"] == "20.0"


@pytest.mark.asyncio
async def test_unavailable_readback_posts_with_same_persisted_client_msg_id(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    state = _state(adapter)
    prepared = adapter._plan_store.prepare_create("sk", expected_route=state)
    client = adapter._team_clients["T1"]
    client.conversations_replies = AsyncMock(side_effect=RuntimeError("missing_scope"))
    client.chat_postMessage = AsyncMock(return_value={"ts": "20.0"})

    assert await _reconcile(adapter, "sk", state["desired_revision"])
    assert client.chat_postMessage.call_args.kwargs["client_msg_id"] == prepared["client_msg_id"]
    assert adapter._plan_store.get_session("sk")["client_msg_id"] == prepared["client_msg_id"]


@pytest.mark.asyncio
async def test_same_revision_dual_create_uses_one_uuid_and_one_anchor(tmp_path) -> None:
    first_adapter = _adapter(tmp_path)
    second_adapter = _adapter(tmp_path)
    client = AsyncMock()
    first_adapter._team_clients = second_adapter._team_clients = {"T1": client}
    client.conversations_replies = AsyncMock(return_value={"messages": []})
    client.chat_update = AsyncMock(return_value={"ts": "20.0"})
    client.chat_delete = AsyncMock()
    started = asyncio.Event()
    release = asyncio.Event()
    post_ids = []

    async def idempotent_post(**kwargs):
        post_ids.append(kwargs["client_msg_id"])
        if len(post_ids) == 2:
            started.set()
        await release.wait()
        return {"ts": "20.0"}

    client.chat_postMessage = AsyncMock(side_effect=idempotent_post)
    state = _state(first_adapter)
    first = asyncio.create_task(_reconcile(first_adapter, "sk", state["desired_revision"]))
    await asyncio.sleep(0)
    second = asyncio.create_task(_reconcile(second_adapter, "sk", state["desired_revision"]))
    await started.wait()
    latest = _state(first_adapter, [{"id": "b", "content": "Latest", "status": "pending"}])
    release.set()

    assert all(await asyncio.gather(first, second))
    assert len(set(post_ids)) == 1
    assert client.chat_delete.await_count == 0
    stored = first_adapter._plan_store.get_session("sk")
    assert stored["message_ts"] == "20.0"
    assert stored["applied_revision"] == latest["desired_revision"]
    assert first_adapter._plan_store.lookup_route("T1", "C1", "20.0")["session_key"] == "sk"
    assert client.chat_update.call_args.kwargs["blocks"][0]["tasks"][0]["task_id"] == "b"


@pytest.mark.asyncio
async def test_non_idempotent_dual_create_cleans_conflicting_loser(tmp_path) -> None:
    first_adapter = _adapter(tmp_path)
    second_adapter = _adapter(tmp_path)
    client = AsyncMock()
    first_adapter._team_clients = second_adapter._team_clients = {"T1": client}
    client.conversations_replies = AsyncMock(return_value={"messages": []})
    client.chat_delete = AsyncMock()
    started = asyncio.Event()
    release = asyncio.Event()
    call_count = 0

    async def non_idempotent_post(**_kwargs):
        nonlocal call_count
        call_count += 1
        result_ts = f"2{call_count}.0"
        if call_count == 2:
            started.set()
        await release.wait()
        return {"ts": result_ts}

    client.chat_postMessage = AsyncMock(side_effect=non_idempotent_post)
    state = _state(first_adapter)
    first = asyncio.create_task(_reconcile(first_adapter, "sk", state["desired_revision"]))
    await asyncio.sleep(0)
    second = asyncio.create_task(_reconcile(second_adapter, "sk", state["desired_revision"]))
    await started.wait()
    release.set()

    assert all(await asyncio.gather(first, second))
    stored = first_adapter._plan_store.get_session("sk")
    assert stored["message_ts"] in {"21.0", "22.0"}
    assert client.chat_delete.await_count == 1
    loser_ts = client.chat_delete.call_args.kwargs["ts"]
    assert loser_ts != stored["message_ts"]
    assert first_adapter._plan_store.lookup_route("T1", "C1", loser_ts) is None


@pytest.mark.asyncio
async def test_conflicting_create_cleanup_failure_is_retired_and_retried_after_restart(tmp_path) -> None:
    first_adapter = _adapter(tmp_path)
    second_adapter = _adapter(tmp_path)
    client = AsyncMock()
    first_adapter._team_clients = second_adapter._team_clients = {"T1": client}
    client.conversations_replies = AsyncMock(return_value={"messages": []})
    client.chat_delete = AsyncMock(side_effect=RuntimeError("delete failed"))
    client.chat_update = AsyncMock(side_effect=RuntimeError("neutralize failed"))
    started = asyncio.Event()
    release = asyncio.Event()
    call_count = 0

    async def non_idempotent_post(**_kwargs):
        nonlocal call_count
        call_count += 1
        result_ts = f"4{call_count}.0"
        if call_count == 2:
            started.set()
        await release.wait()
        return {"ts": result_ts}

    client.chat_postMessage = AsyncMock(side_effect=non_idempotent_post)
    state = _state(first_adapter)
    first = asyncio.create_task(_reconcile(first_adapter, "sk", state["desired_revision"]))
    await asyncio.sleep(0)
    second = asyncio.create_task(_reconcile(second_adapter, "sk", state["desired_revision"]))
    await started.wait()
    release.set()
    await asyncio.gather(first, second)

    persisted = first_adapter._plan_store.get_session("sk")
    assert len(persisted["retired_anchors"]) == 1
    loser = persisted["retired_anchors"][0]
    assert loser["message_ts"] != persisted["message_ts"]
    assert loser["client_msg_id"] == persisted["client_msg_id"]

    restarted = _adapter(tmp_path)
    restarted_client = restarted._team_clients["T1"]
    restarted_client.chat_delete = AsyncMock()
    with patch(
        "plugins.platforms.slack.plan_cards.time.time",
        return_value=time.time() + 120,
    ):
        assert await _reconcile(restarted, "sk", state["desired_revision"])
    restarted_client.chat_delete.assert_awaited_once_with(channel="C1", ts=loser["message_ts"])
    assert restarted._plan_store.get_session("sk")["retired_anchors"] == []


@pytest.mark.asyncio
async def test_readback_conflict_cleanup_failure_retires_recovered_loser(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    state = _state(adapter)
    prepared = adapter._plan_store.prepare_create("sk", expected_route=state)
    client = adapter._team_clients["T1"]

    async def conflicting_readback(**_kwargs):
        competing = type(adapter._plan_store)(tmp_path)
        assert competing.record_create_result(
            "sk", expected_route=prepared,
            client_msg_id=prepared["client_msg_id"], message_ts="20.0",
        ) == "current"
        return {"messages": [{
            "ts": "21.0", "client_msg_id": prepared["client_msg_id"],
        }]}

    async def update_by_ts(**kwargs):
        if kwargs["ts"] == "21.0":
            raise RuntimeError("neutralize failed")
        return {"ts": kwargs["ts"]}

    client.conversations_replies = AsyncMock(side_effect=conflicting_readback)
    client.chat_delete = AsyncMock(side_effect=RuntimeError("delete failed"))
    client.chat_update = AsyncMock(side_effect=update_by_ts)

    assert not await _reconcile(adapter, "sk", state["desired_revision"])
    persisted = adapter._plan_store.get_session("sk")
    assert persisted["message_ts"] == "20.0"
    assert [anchor["message_ts"] for anchor in persisted["retired_anchors"]] == ["21.0"]


@pytest.mark.asyncio
async def test_thread_to_non_thread_route_uses_new_uuid_and_history_recovery(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    store = adapter._plan_store
    threaded = _state(adapter)
    prepared = store.prepare_create("sk", expected_route=threaded)
    assert store.mark_applied(
        "sk", revision=threaded["desired_revision"], snapshot_hash=threaded["desired_hash"],
        message_ts="20.0", rendered_revision=threaded["desired_revision"],
        expected_message_ts="", expected_client_msg_id=prepared["client_msg_id"],
    )
    moved = adapter.record_desired_plan_snapshot(
        session_key="sk", session_id="sid", team_id="T1", channel_id="C1",
        thread_ts="", route_user_id="U-owner", chat_type="group",
        todos=[{"id": "b", "content": "Moved", "status": "pending"}],
    )
    next_generation = store.prepare_create("sk", expected_route=moved)
    client = adapter._team_clients["T1"]
    client.chat_delete = AsyncMock()
    client.conversations_history = AsyncMock(return_value={"messages": [{
        "ts": "30.0", "client_msg_id": next_generation["client_msg_id"],
    }]})
    client.conversations_replies = AsyncMock()
    client.chat_postMessage = AsyncMock()
    client.chat_update = AsyncMock(return_value={"ts": "30.0"})

    assert await _reconcile(adapter, "sk", moved["desired_revision"])
    client.conversations_history.assert_awaited_once()
    client.conversations_replies.assert_not_awaited()
    client.chat_postMessage.assert_not_awaited()
    assert store.get_session("sk")["client_msg_id"] == next_generation["client_msg_id"]
    assert next_generation["client_msg_id"] != prepared["client_msg_id"]


@pytest.mark.asyncio
async def test_inflight_stale_update_converges_latest_on_existing_anchor(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    client = adapter._team_clients["T1"]
    client.chat_postMessage = AsyncMock(return_value={"ts": "20.0"})
    first = _state(adapter)
    assert await _reconcile(adapter, "sk", first["desired_revision"])

    update_started = asyncio.Event()
    release_update = asyncio.Event()

    async def delayed_update(**_kwargs):
        update_started.set()
        await release_update.wait()
        return {"ts": "20.0"}

    client.chat_update = AsyncMock(side_effect=delayed_update)
    stale = _state(adapter, [{"id": "b", "content": "Stale", "status": "pending"}])
    reconcile = asyncio.create_task(
        _reconcile(adapter, "sk", stale["desired_revision"])
    )
    await update_started.wait()
    latest = _state(adapter, [{"id": "c", "content": "Latest", "status": "pending"}])
    release_update.set()

    assert await reconcile
    assert client.chat_postMessage.await_count == 1
    assert client.chat_update.await_count == 2
    assert client.chat_update.call_args.kwargs["blocks"][0]["tasks"][0]["task_id"] == "c"
    assert adapter._plan_store.get_session("sk")["applied_revision"] == latest["desired_revision"]


@pytest.mark.asyncio
async def test_stale_create_cleans_up_orphan_when_other_process_anchor_wins(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    client = adapter._team_clients["T1"]
    post_started = asyncio.Event()
    release_post = asyncio.Event()

    async def delayed_post(**_kwargs):
        post_started.set()
        await release_post.wait()
        return {"ts": "20.0"}

    client.chat_postMessage = AsyncMock(side_effect=delayed_post)
    client.chat_update = AsyncMock(return_value={"ts": "30.0"})
    client.chat_delete = AsyncMock()
    first = _state(adapter)
    reconcile = asyncio.create_task(
        _reconcile(adapter, "sk", first["desired_revision"])
    )
    await post_started.wait()
    latest = _state(adapter, [{"id": "b", "content": "Latest", "status": "pending"}])
    competing_store = type(adapter._plan_store)(tmp_path)
    assert competing_store.record_create_result(
        "sk", expected_route=latest,
        client_msg_id=latest["client_msg_id"], message_ts="30.0",
    ) == "current"
    release_post.set()

    assert await reconcile
    client.chat_postMessage.assert_awaited_once()
    client.chat_delete.assert_awaited_once_with(channel="C1", ts="20.0")
    client.chat_update.assert_awaited_once()
    assert adapter._plan_store.get_session("sk")["message_ts"] == "30.0"


@pytest.mark.asyncio
async def test_stale_create_cleans_up_orphan_when_route_changes(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    client = adapter._team_clients["T1"]
    post_started = asyncio.Event()
    release_post = asyncio.Event()

    async def delayed_post(**_kwargs):
        post_started.set()
        await release_post.wait()
        return {"ts": "20.0"}

    client.chat_postMessage = AsyncMock(side_effect=delayed_post)
    client.chat_delete = AsyncMock()
    first = _state(adapter)
    reconcile = asyncio.create_task(
        _reconcile(adapter, "sk", first["desired_revision"])
    )
    await post_started.wait()
    latest = adapter.record_desired_plan_snapshot(
        session_key="sk", session_id="sid-2", team_id="T1", channel_id="C2",
        thread_ts="11.0", route_user_id="U-owner", chat_type="group",
        todos=[{"id": "b", "content": "Moved", "status": "pending"}],
    )
    release_post.set()

    assert await reconcile
    assert client.chat_postMessage.await_count == 2
    assert client.chat_postMessage.call_args_list[0].kwargs["channel"] == "C1"
    assert client.chat_postMessage.call_args_list[1].kwargs["channel"] == "C2"
    client.chat_delete.assert_awaited_once_with(channel="C1", ts="20.0")
    state = adapter._plan_store.get_session("sk")
    assert state["channel_id"] == "C2"
    assert state["applied_revision"] == latest["desired_revision"]


@pytest.mark.asyncio
async def test_retired_cleanup_failure_is_retried_after_adapter_restart(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    client = adapter._team_clients["T1"]
    client.chat_postMessage = AsyncMock(side_effect=[{"ts": "20.0"}, {"ts": "30.0"}])
    first = _state(adapter)
    assert await _reconcile(adapter, "sk", first["desired_revision"])
    moved = adapter.record_desired_plan_snapshot(
        session_key="sk", session_id="sid-2", team_id="T1", channel_id="C2",
        thread_ts="11.0", route_user_id="U-owner", chat_type="group",
        todos=[{"id": "b", "content": "Moved", "status": "pending"}],
    )
    client.chat_delete = AsyncMock(side_effect=RuntimeError("delete failed"))
    client.chat_update = AsyncMock(side_effect=RuntimeError("neutralize failed"))

    assert not await _reconcile(adapter, "sk", moved["desired_revision"])
    persisted = adapter._plan_store.get_session("sk")
    assert persisted["message_ts"] == "30.0"
    assert persisted["retired_anchors"][0]["message_ts"] == "20.0"
    assert persisted["retired_anchors"][0]["retry_count"] == 1

    restarted = _adapter(tmp_path)
    restarted_client = restarted._team_clients["T1"]
    restarted_client.chat_delete = AsyncMock()
    with patch(
        "plugins.platforms.slack.plan_cards.time.time",
        return_value=time.time() + 120,
    ):
        assert await _reconcile(restarted, "sk", moved["desired_revision"])
    restarted_client.chat_delete.assert_awaited_once_with(channel="C1", ts="20.0")
    assert restarted._plan_store.get_session("sk")["retired_anchors"] == []


@pytest.mark.asyncio
async def test_transient_failure_keeps_dirty_for_worker_retry(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    state = _state(adapter)
    adapter._team_clients["T1"].chat_postMessage = AsyncMock(side_effect=RuntimeError("timeout"))
    assert not await _reconcile(adapter, "sk", state["desired_revision"])
    persisted = adapter._plan_store.get_session("sk")
    assert persisted["desired_revision"] > persisted["applied_revision"]
    assert persisted["retry_count"] == 1


@pytest.mark.asyncio
async def test_apply_and_retry_store_failure_uses_worker_backoff_then_wake_recovers(
    tmp_path,
) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter)
    client = adapter._team_clients["T1"]
    client.chat_postMessage = AsyncMock(
        side_effect=[RuntimeError("timeout"), {"ts": "20.0"}]
    )
    client.conversations_replies = AsyncMock(return_value={"messages": []})
    retry_failed = asyncio.Event()

    def fail_retry(_session_key):
        retry_failed.set()
        raise OSError("disk unavailable")

    adapter._plan_store.mark_retry = MagicMock(side_effect=fail_retry)
    adapter._plan_store.retry_schedule = MagicMock(
        wraps=adapter._plan_store.retry_schedule
    )
    adapter._running = True
    with patch("plugins.platforms.slack.adapter.random.uniform", return_value=0):
        adapter._start_plan_reconcile_worker()
        worker = adapter._plan_reconcile_task
        await retry_failed.wait()
        await asyncio.sleep(0)

        assert adapter._plan_reconcile_task is worker
        assert adapter._plan_store.retry_schedule.call_count == 0

        adapter._plan_store.mark_retry = MagicMock()
        adapter.request_plan_reconcile()
        for _ in range(50):
            if client.chat_postMessage.await_count == 2:
                break
            await asyncio.sleep(0.01)
        await adapter._stop_plan_reconcile_worker()

    assert client.chat_postMessage.await_count == 2
    assert adapter._plan_store.get_session("sk")["applied_revision"] == 1


@pytest.mark.asyncio
async def test_session_crash_and_retry_store_failure_reaches_same_worker_backoff(
    tmp_path,
) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter)
    crashed = asyncio.Event()

    original_cleanup = adapter._cleanup_retired_plan_anchors
    cleanup_calls = 0

    async def cleanup_side_effect(generation, session_key):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            crashed.set()
            raise RuntimeError("unexpected session failure")
        return await original_cleanup(generation, session_key)

    adapter._cleanup_retired_plan_anchors = AsyncMock(
        side_effect=cleanup_side_effect
    )
    retry_failed = asyncio.Event()

    def fail_retry(_session_key):
        retry_failed.set()
        raise OSError("disk unavailable")

    adapter._plan_store.mark_retry = MagicMock(side_effect=fail_retry)
    adapter._plan_store.retry_schedule = MagicMock(
        wraps=adapter._plan_store.retry_schedule
    )
    adapter._team_clients["T1"].chat_postMessage = AsyncMock(
        return_value={"ts": "20.0"}
    )
    adapter._running = True
    with patch("plugins.platforms.slack.adapter.random.uniform", return_value=0):
        adapter._start_plan_reconcile_worker()
        worker = adapter._plan_reconcile_task
        await crashed.wait()
        await retry_failed.wait()
        await asyncio.sleep(0)

        assert adapter._plan_reconcile_task is worker
        assert adapter._plan_store.retry_schedule.call_count == 0

        adapter._plan_store.mark_retry = MagicMock()
        adapter.request_plan_reconcile()
        for _ in range(50):
            if adapter._plan_store.get_session("sk")["applied_revision"] == 1:
                break
            await asyncio.sleep(0.01)
        await adapter._stop_plan_reconcile_worker()

    assert adapter._plan_store.get_session("sk")["applied_revision"] == 1


@pytest.mark.asyncio
async def test_persistent_retry_store_failure_is_rate_bounded_and_disconnects(
    tmp_path,
) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter)
    adapter._team_clients["T1"].chat_postMessage = AsyncMock(
        side_effect=RuntimeError("timeout")
    )
    adapter._team_clients["T1"].conversations_replies = AsyncMock(
        return_value={"messages": []}
    )
    adapter._plan_store.mark_retry = MagicMock(
        side_effect=OSError("disk unavailable")
    )
    adapter._running = True
    adapter._stop_socket_mode_handler = AsyncMock()
    adapter._release_platform_lock = MagicMock()

    with patch("plugins.platforms.slack.adapter.random.uniform", return_value=0):
        adapter._start_plan_reconcile_worker()
        worker = adapter._plan_reconcile_task
        await asyncio.sleep(0.36)

        assert adapter._plan_reconcile_task is worker
        assert worker is not None and not worker.done()
        assert 2 <= adapter._plan_store.mark_retry.call_count <= 4
        await asyncio.wait_for(adapter.disconnect(), timeout=0.2)

    assert worker.done()
    assert adapter._plan_reconcile_task is None


@pytest.mark.asyncio
async def test_retired_cleanup_and_retry_persistence_double_failure_uses_worker_backoff(
    tmp_path,
) -> None:
    adapter = _adapter(tmp_path)
    _converged_state_with_retired_anchor(adapter)
    client = adapter._team_clients["T1"]
    client.chat_delete = AsyncMock(side_effect=[RuntimeError("delete failed"), None])
    client.chat_update = AsyncMock(side_effect=RuntimeError("neutralize failed"))
    retry_persistence_failed = asyncio.Event()

    def fail_retired_retry(*_args, **_kwargs):
        retry_persistence_failed.set()
        raise OSError("disk unavailable")

    adapter._plan_store.mark_retired_retry = MagicMock(
        side_effect=fail_retired_retry
    )
    adapter._plan_store.mark_retry = MagicMock(
        wraps=adapter._plan_store.mark_retry
    )
    adapter._running = True

    with patch("plugins.platforms.slack.adapter.random.uniform", return_value=0):
        adapter._start_plan_reconcile_worker()
        worker = adapter._plan_reconcile_task
        await retry_persistence_failed.wait()
        for _ in range(50):
            if not adapter._plan_store.get_session("sk")["retired_anchors"]:
                break
            await asyncio.sleep(0.01)
        await adapter._stop_plan_reconcile_worker()

    assert adapter._plan_reconcile_task is None
    assert worker is not None
    assert adapter._plan_store.get_session("sk")["retired_anchors"] == []
    assert client.chat_delete.await_count == 2
    adapter._plan_store.mark_retry.assert_not_called()


@pytest.mark.asyncio
async def test_persistent_retired_retry_persistence_failure_is_rate_bounded_and_disconnects(
    tmp_path,
) -> None:
    adapter = _adapter(tmp_path)
    _converged_state_with_retired_anchor(adapter)
    client = adapter._team_clients["T1"]
    client.chat_delete = AsyncMock(side_effect=RuntimeError("delete failed"))
    client.chat_update = AsyncMock(side_effect=RuntimeError("neutralize failed"))
    adapter._plan_store.mark_retired_retry = MagicMock(
        side_effect=OSError("disk unavailable")
    )
    adapter._plan_store.mark_retry = MagicMock(
        wraps=adapter._plan_store.mark_retry
    )
    adapter._running = True
    adapter._stop_socket_mode_handler = AsyncMock()
    adapter._release_platform_lock = MagicMock()

    with patch("plugins.platforms.slack.adapter.random.uniform", return_value=0):
        adapter._start_plan_reconcile_worker()
        worker = adapter._plan_reconcile_task
        await asyncio.sleep(0.36)

        assert adapter._plan_reconcile_task is worker
        assert worker is not None and not worker.done()
        assert 2 <= adapter._plan_store.mark_retired_retry.call_count <= 4
        adapter._plan_store.mark_retry.assert_not_called()
        await asyncio.wait_for(adapter.disconnect(), timeout=0.2)

    assert worker.done()
    assert adapter._plan_reconcile_task is None


@pytest.mark.asyncio
async def test_apply_failure_with_persisted_retry_uses_exact_deadline(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter)
    client = adapter._team_clients["T1"]
    client.chat_postMessage = AsyncMock(
        side_effect=[RuntimeError("timeout"), {"ts": "20.0"}]
    )
    client.conversations_replies = AsyncMock(return_value={"messages": []})
    original_mark_retry = adapter._plan_store.mark_retry

    def short_retry(session_key):
        return original_mark_retry(
            session_key, base_seconds=0.03, max_seconds=0.03
        )

    adapter._plan_store.mark_retry = MagicMock(side_effect=short_retry)
    adapter._plan_store.list_dirty = MagicMock(wraps=adapter._plan_store.list_dirty)
    adapter._running = True
    started = time.monotonic()
    with patch("plugins.platforms.slack.plan_cards.random.uniform", return_value=0):
        adapter._start_plan_reconcile_worker()
        for _ in range(50):
            if client.chat_postMessage.await_count == 2:
                break
            await asyncio.sleep(0.01)
        elapsed = time.monotonic() - started
        await adapter._stop_plan_reconcile_worker()

    assert client.chat_postMessage.await_count == 2
    assert elapsed >= 0.02
    assert adapter._plan_store.list_dirty.call_count <= 3


@pytest.mark.asyncio
async def test_startup_worker_reconciles_dirty_state_and_disconnect_stops_it(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    state = _state(adapter)
    adapter._team_clients["T1"].chat_postMessage = AsyncMock(return_value={"ts": "20.0"})
    adapter._plan_reconcile_interval_s = 0.01
    adapter._running = True
    adapter._stop_socket_mode_handler = AsyncMock()
    adapter._release_platform_lock = MagicMock()

    adapter._start_plan_reconcile_worker()
    for _ in range(50):
        if adapter._plan_store.get_session("sk")["applied_revision"] == state["desired_revision"]:
            break
        await asyncio.sleep(0.01)
    assert adapter._plan_store.get_session("sk")["applied_revision"] == state["desired_revision"]

    await adapter.disconnect()
    assert adapter._plan_reconcile_task is None
    adapter._stop_socket_mode_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_background_tasks_stops_blocked_plan_worker_before_base_cleanup(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter)
    adapter._running = True
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_post(**_kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {"ts": "20.0"}

    adapter._team_clients["T1"].chat_postMessage = AsyncMock(side_effect=blocked_post)
    adapter._start_plan_reconcile_worker()
    await started.wait()
    cancelling = asyncio.create_task(adapter.cancel_background_tasks())
    await asyncio.sleep(0)
    assert adapter._plan_reconcile_stopping is True
    release.set()
    await cancelling

    assert adapter._plan_reconcile_task is None
    assert adapter._plan_store.get_session("sk")["applied_revision"] == 0
    assert adapter._plan_store.get_session("sk")["message_ts"] == "20.0"


@pytest.mark.asyncio
async def test_cancel_then_disconnect_is_idempotent_for_plan_worker(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    adapter._running = True
    adapter._stop_socket_mode_handler = AsyncMock()
    adapter._release_platform_lock = MagicMock()
    adapter._start_plan_reconcile_worker()

    await adapter.cancel_background_tasks()
    await adapter.disconnect()

    assert adapter._plan_reconcile_task is None
    adapter._stop_socket_mode_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_idle_worker_waits_without_fixed_interval_rescans(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    adapter._running = True
    adapter._plan_reconcile_interval_s = 0.01
    adapter._plan_store.list_dirty = MagicMock(wraps=adapter._plan_store.list_dirty)
    adapter._plan_store.retry_schedule = MagicMock(
        wraps=adapter._plan_store.retry_schedule
    )

    adapter._start_plan_reconcile_worker()
    await asyncio.sleep(0.05)
    await adapter._stop_plan_reconcile_worker()

    assert adapter._plan_store.list_dirty.call_count == 1
    assert adapter._plan_store.retry_schedule.call_count == 1


@pytest.mark.asyncio
async def test_due_now_after_stale_dirty_scan_immediately_rescans_then_quiesces(
    tmp_path,
) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter)
    original_list_dirty = adapter._plan_store.list_dirty
    calls = 0

    def stale_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return []
        return original_list_dirty(*args, **kwargs)

    adapter._plan_store.list_dirty = MagicMock(side_effect=stale_once)
    adapter._plan_reconcile_wakeup.wait = AsyncMock(
        wraps=adapter._plan_reconcile_wakeup.wait
    )
    adapter._team_clients["T1"].chat_postMessage = AsyncMock(
        return_value={"ts": "20.0"}
    )
    adapter._running = True

    adapter._start_plan_reconcile_worker()
    for _ in range(50):
        if adapter._plan_store.get_session("sk")["applied_revision"] == 1:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0)
    await adapter._stop_plan_reconcile_worker()

    assert adapter._plan_store.get_session("sk")["applied_revision"] == 1
    assert adapter._plan_store.list_dirty.call_count == 2
    assert adapter._plan_reconcile_wakeup.wait.await_count == 1


@pytest.mark.asyncio
async def test_current_retry_runs_at_earliest_deadline(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter)
    with patch("plugins.platforms.slack.plan_cards.random.uniform", return_value=0):
        adapter._plan_store.mark_retry("sk", base_seconds=0.03, max_seconds=0.03)
    adapter._running = True
    adapter._plan_reconcile_interval_s = 1.0
    client = adapter._team_clients["T1"]
    client.chat_postMessage = AsyncMock(return_value={"ts": "20.0"})

    adapter._start_plan_reconcile_worker()
    for _ in range(50):
        if client.chat_postMessage.await_count:
            break
        await asyncio.sleep(0.01)
    await adapter._stop_plan_reconcile_worker()

    client.chat_postMessage.assert_awaited_once()


@pytest.mark.asyncio
async def test_retired_retry_runs_at_earliest_deadline(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    first = _state(adapter)
    prepared = adapter._plan_store.prepare_create("sk", expected_route=first)
    assert adapter._plan_store.mark_applied(
        "sk", revision=first["desired_revision"], snapshot_hash=first["desired_hash"],
        message_ts="20.0", expected_message_ts="",
        expected_client_msg_id=prepared["client_msg_id"],
    )
    adapter.record_desired_plan_snapshot(
        session_key="sk", session_id="sid-2", team_id="T1", channel_id="C2",
        thread_ts="11.0", route_user_id="U-owner", chat_type="group",
        todos=[{"id": "b", "content": "Moved", "status": "pending"}],
    )
    retired = adapter._plan_store.list_retired("sk")[0]
    with patch("plugins.platforms.slack.plan_cards.random.uniform", return_value=0):
        adapter._plan_store.mark_retry("sk", base_seconds=1, max_seconds=1)
        adapter._plan_store.mark_retired_retry(
            "sk", retired["anchor_id"], base_seconds=0.03, max_seconds=0.03
        )
    adapter._running = True
    client = adapter._team_clients["T1"]
    client.chat_delete = AsyncMock()

    adapter._start_plan_reconcile_worker()
    for _ in range(50):
        if client.chat_delete.await_count:
            break
        await asyncio.sleep(0.01)
    await adapter._stop_plan_reconcile_worker()

    client.chat_delete.assert_awaited_once_with(channel="C1", ts="20.0")


@pytest.mark.asyncio
async def test_new_snapshot_wake_preempts_long_retry_deadline(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter)
    with patch("plugins.platforms.slack.plan_cards.random.uniform", return_value=0):
        adapter._plan_store.mark_retry("sk", base_seconds=10, max_seconds=10)
    adapter._running = True
    client = adapter._team_clients["T1"]
    client.chat_postMessage = AsyncMock(return_value={"ts": "20.0"})
    adapter._start_plan_reconcile_worker()
    await asyncio.sleep(0.02)
    client.chat_postMessage.assert_not_awaited()

    _state(adapter, [{"id": "b", "content": "New", "status": "pending"}])
    adapter.request_plan_reconcile()
    for _ in range(50):
        if client.chat_postMessage.await_count:
            break
        await asyncio.sleep(0.01)
    await adapter._stop_plan_reconcile_worker()

    client.chat_postMessage.assert_awaited_once()


@pytest.mark.asyncio
async def test_many_converged_sessions_get_one_quiescent_scan(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    for index in range(50):
        key = f"sk-{index}"
        state = adapter._plan_store.record_desired_snapshot({
            "session_key": key, "session_id": f"sid-{index}", "team_id": "T1",
            "channel_id": "C1", "thread_ts": "",
        }, [{"id": "a", "content": "A", "status": "pending"}])
        assert adapter._plan_store.mark_applied(
            key, revision=state["desired_revision"], snapshot_hash=state["desired_hash"],
            message_ts=f"20.{index}",
        )
    adapter._running = True
    adapter._plan_reconcile_interval_s = 0.01
    adapter._plan_store.list_dirty = MagicMock(wraps=adapter._plan_store.list_dirty)
    adapter._plan_store.retry_schedule = MagicMock(
        wraps=adapter._plan_store.retry_schedule
    )

    adapter._start_plan_reconcile_worker()
    await asyncio.sleep(0.05)
    await adapter._stop_plan_reconcile_worker()

    assert adapter._plan_store.list_dirty.call_count == 1
    assert adapter._plan_store.retry_schedule.call_count == 1


@pytest.mark.asyncio
async def test_worker_recovers_from_transient_list_error_on_wake_same_task(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    client = adapter._team_clients["T1"]
    client.chat_postMessage = AsyncMock(return_value={"ts": "20.0"})
    original = adapter._plan_store.list_dirty
    failed = asyncio.Event()
    calls = 0

    def flaky_list_dirty(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            failed.set()
            raise OSError("transient lock error")
        return original(*args, **kwargs)

    adapter._plan_store.list_dirty = MagicMock(side_effect=flaky_list_dirty)
    adapter._running = True
    adapter._start_plan_reconcile_worker()
    worker = adapter._plan_reconcile_task
    await failed.wait()
    _state(adapter)
    adapter.request_plan_reconcile()
    for _ in range(50):
        if client.chat_postMessage.await_count:
            break
        await asyncio.sleep(0.01)
    await adapter._stop_plan_reconcile_worker()

    client.chat_postMessage.assert_awaited_once()
    assert worker is not None
    assert calls >= 2


@pytest.mark.asyncio
async def test_worker_recovers_from_transient_deadline_error_on_wake(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    original = adapter._plan_store.retry_schedule
    failed = asyncio.Event()
    calls = 0

    def flaky_deadline(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            failed.set()
            raise OSError("transient read error")
        return original(*args, **kwargs)

    adapter._plan_store.retry_schedule = MagicMock(side_effect=flaky_deadline)
    adapter._running = True
    client = adapter._team_clients["T1"]
    client.chat_postMessage = AsyncMock(return_value={"ts": "20.0"})
    adapter._start_plan_reconcile_worker()
    worker = adapter._plan_reconcile_task
    await failed.wait()
    _state(adapter)
    adapter.request_plan_reconcile()
    for _ in range(50):
        if client.chat_postMessage.await_count:
            break
        await asyncio.sleep(0.01)
    assert adapter._plan_reconcile_task is worker
    await adapter._stop_plan_reconcile_worker()

    client.chat_postMessage.assert_awaited_once()
    assert calls >= 2


@pytest.mark.asyncio
async def test_persistent_worker_error_is_rate_bounded_without_replacement(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    adapter._plan_store.list_dirty = MagicMock(side_effect=OSError("persistent"))
    adapter._running = True
    with patch("plugins.platforms.slack.adapter.random.uniform", return_value=0):
        adapter._start_plan_reconcile_worker()
        worker = adapter._plan_reconcile_task
        await asyncio.sleep(0.36)
        assert adapter._plan_reconcile_task is worker
        assert worker is not None and not worker.done()
        assert 2 <= adapter._plan_store.list_dirty.call_count <= 4
        await adapter._stop_plan_reconcile_worker()


@pytest.mark.asyncio
async def test_disconnect_during_worker_error_backoff_exits_same_task(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    failed = asyncio.Event()

    def fail_list_dirty():
        failed.set()
        raise OSError("persistent")

    adapter._plan_store.list_dirty = MagicMock(side_effect=fail_list_dirty)
    adapter._running = True
    adapter._stop_socket_mode_handler = AsyncMock()
    adapter._release_platform_lock = MagicMock()
    adapter._start_plan_reconcile_worker()
    worker = adapter._plan_reconcile_task
    await failed.wait()

    await asyncio.wait_for(adapter.disconnect(), timeout=0.2)

    assert worker is not None and worker.done()
    assert adapter._plan_reconcile_task is None


@pytest.mark.asyncio
async def test_stale_schedule_applies_latest_and_refresh_forces_update(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    client = adapter._team_clients["T1"]
    client.chat_postMessage = AsyncMock(return_value={"ts": "20.0"})
    first = _state(adapter)
    second = _state(adapter, [{"id": "b", "content": "latest", "status": "pending"}])
    assert await _reconcile(adapter, "sk", first["desired_revision"])
    assert adapter._plan_store.get_session("sk")["applied_revision"] == second["desired_revision"]

    client.chat_update = AsyncMock(return_value={"ts": "20.0"})
    refreshed = adapter._plan_store.request_refresh("sk")
    assert refreshed["desired_revision"] == second["desired_revision"] + 1
    assert await _reconcile(adapter, "sk")
    client.chat_update.assert_awaited_once()
    assert (
        client.chat_update.call_args.kwargs["blocks"][0]["block_id"]
        != f"hermes-plan-r{second['desired_revision']}-{second['desired_hash'][:10]}"
    )


@pytest.mark.asyncio
async def test_plan_action_acks_authorizes_validates_dedupes_and_emits_internal_event(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter, [
        {"id": "agent:a", "content": "Agent", "status": "pending"},
        {"id": "user:a", "content": "A", "status": "pending"},
        {"id": "user:b", "content": "B", "status": "completed"},
    ])
    adapter._plan_store.mark_applied(
        "sk",
        revision=1,
        snapshot_hash=adapter._plan_store.get_session("sk")["desired_hash"],
        message_ts="20.0",
    )
    events: list[MessageEvent] = []

    async def handle(event: MessageEvent):
        events.append(event)

    adapter.set_message_handler(handle)
    adapter._is_interactive_user_authorized = MagicMock(return_value=True)
    ack = AsyncMock()
    body = {
        "team": {"id": "T1"},
        "channel": {"id": "C1"},
        "message": {"ts": "20.0", "thread_ts": "10.0"},
        "user": {"id": "U-owner", "name": "owner"},
        "actions": [{"action_ts": "99.1"}],
    }
    action = {
        "action_id": "hermes_plan_complete",
        "block_id": "hermes-plan-controls-r1-" + adapter._plan_store.get_session("sk")["desired_hash"][:10],
        "selected_options": [{"value": "user:a"}],
    }

    await adapter._handle_plan_action(ack, body, action)
    await asyncio.sleep(0)
    assert ack.await_count == 1
    assert len(events) == 1
    event = events[0]
    assert event.internal is True
    assert event.source.chat_id == "C1"
    assert event.source.thread_id == "10.0"
    assert event.source.user_id == "U-owner"
    assert event.metadata["gateway_session_id"] == "sid"
    trusted = event.metadata["slack_plan_action"]
    assert trusted["action_user_id"] == "U-owner"
    assert trusted["action_kind"] == "complete_reopen"
    assert trusted["task_ids"] == ["user:a", "user:b"]
    assert trusted["complete_task_ids"] == ["user:a"]
    assert trusted["reopen_task_ids"] == ["user:b"]
    assert trusted["revision"] == 1
    assert "todo" in event.text.lower()
    assert adapter.validate_plan_action_metadata(trusted)

    await adapter._handle_plan_action(ack, body, action)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_plan_action_rejects_mixed_duplicate_and_ineligible_ids_atomically(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter, [
        {"id": "agent:a", "content": "Agent", "status": "pending"},
        {"id": "user:pending", "content": "Pending", "status": "pending"},
        {"id": "user:done", "content": "Done", "status": "completed"},
        {"id": "user:active", "content": "Active", "status": "in_progress"},
    ])
    state = adapter._plan_store.get_session("sk")
    adapter._plan_store.mark_applied(
        "sk", revision=1, snapshot_hash=state["desired_hash"], message_ts="20.0",
    )
    events = []

    async def handle(event):
        events.append(event)

    adapter.set_message_handler(handle)
    adapter._is_interactive_user_authorized = MagicMock(return_value=True)
    body = {
        "team": {"id": "T1"}, "channel": {"id": "C1"},
        "message": {"ts": "20.0", "thread_ts": "10.0"},
        "user": {"id": "U-owner"}, "actions": [{"action_ts": "mixed"}],
    }
    block_id = "hermes-plan-controls-r1-" + state["desired_hash"][:10]

    for index, action in enumerate((
        {
            "action_id": "hermes_plan_complete", "block_id": block_id,
            "selected_options": [
                {"value": "user:pending"}, {"value": "agent:a"},
            ],
        },
        {
            "action_id": "hermes_plan_complete", "block_id": block_id,
            "selected_options": [
                {"value": "user:pending"}, {"value": "user:pending"},
            ],
        },
        {
            "action_id": "hermes_plan_complete", "block_id": block_id,
            "selected_options": [{"value": "user:missing"}],
        },
        {
            "action_id": "hermes_plan_cancel", "block_id": block_id,
            "selected_option": {"value": "agent:a"},
        },
        {
            "action_id": "hermes_plan_cancel", "block_id": block_id,
            "selected_option": {"value": "user:active"},
        },
    )):
        body["actions"][0]["action_ts"] = f"invalid-{index}"
        await adapter._handle_plan_action(AsyncMock(), body, action)

    await asyncio.sleep(0)
    assert events == []

    body["actions"][0]["action_ts"] = "valid-cancel"
    await adapter._handle_plan_action(AsyncMock(), body, {
        "action_id": "hermes_plan_cancel", "block_id": block_id,
        "selected_option": {"value": "user:pending"},
    })
    await asyncio.sleep(0)
    assert len(events) == 1
    assert events[0].metadata["slack_plan_action"]["action_kind"] == "cancel"
    assert events[0].metadata["slack_plan_action"]["cancel_task_ids"] == ["user:pending"]


def test_adapter_claim_validator_rejects_direct_forged_plan_metadata(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    state = _state(adapter, [
        {"id": "agent:a", "content": "Agent", "status": "pending"},
        {"id": "user:a", "content": "User", "status": "pending"},
    ])
    assert adapter._plan_store.mark_applied(
        "sk", revision=1, snapshot_hash=state["desired_hash"], message_ts="20.0",
    )
    base = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0", "message_ts": "20.0",
        "revision": 1, "snapshot_hash": state["desired_hash"],
        "action_user_id": "U-owner", "action_dedupe_id": "dedupe",
    }
    assert adapter._plan_store.consume_action_id("dedupe")
    assert not adapter.validate_plan_action_metadata({
        **base, "action_kind": "complete_reopen",
        "task_ids": ["agent:a", "user:a"],
        "complete_task_ids": ["agent:a", "user:a"], "reopen_task_ids": [],
    })
    assert not adapter.validate_plan_action_metadata({
        **base, "action_kind": "complete_reopen", "task_ids": ["agent:a"],
        "complete_task_ids": [], "reopen_task_ids": ["agent:a"],
    })
    assert not adapter.validate_plan_action_metadata({
        **base, "action_kind": "add_user_task", "task_ids": ["user:new"],
        "add_task_ids": ["user:replacement"], "add_task_content": "New",
    })


@pytest.mark.asyncio
async def test_plan_action_rejects_wrong_route_or_unauthorized_user(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter)
    adapter._plan_store.mark_applied(
        "sk", revision=1,
        snapshot_hash=adapter._plan_store.get_session("sk")["desired_hash"],
        message_ts="20.0",
    )
    adapter.set_message_handler(AsyncMock())
    ack = AsyncMock()
    body = {
        "team": {"id": "T1"}, "channel": {"id": "WRONG"},
        "message": {"ts": "20.0"}, "user": {"id": "U1"},
        "actions": [{"action_ts": "1"}],
    }
    adapter._is_interactive_user_authorized = MagicMock(return_value=True)
    block_id = "hermes-plan-controls-r1-" + adapter._plan_store.get_session("sk")["desired_hash"][:10]
    await adapter._handle_plan_action(ack, body, {
        "action_id": "hermes_plan_refresh", "block_id": block_id,
    })
    adapter._message_handler.assert_not_awaited()

    body["channel"]["id"] = "C1"
    adapter._is_interactive_user_authorized.return_value = False
    await adapter._handle_plan_action(ack, body, {
        "action_id": "hermes_plan_refresh", "block_id": block_id,
    })
    adapter._message_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "dm"])
async def test_plan_action_requires_persisted_route_owner_for_group_and_dm(tmp_path, chat_type) -> None:
    adapter = _adapter(tmp_path)
    _state(
        adapter,
        [{"id": "user:a", "content": "A", "status": "pending"}],
        route_user_id="U-owner",
        chat_type=chat_type,
    )
    state = adapter._plan_store.get_session("sk")
    adapter._plan_store.mark_applied(
        "sk", revision=1, snapshot_hash=state["desired_hash"], message_ts="20.0",
    )
    events = []

    async def handle(event):
        events.append(event)

    adapter.set_message_handler(handle)
    adapter._is_interactive_user_authorized = MagicMock(return_value=True)
    body = {
        "team": {"id": "T1"}, "channel": {"id": "C1"},
        "message": {"ts": "20.0", "thread_ts": "10.0"},
        "user": {"id": "U-other"}, "actions": [{"action_ts": f"other-{chat_type}"}],
    }
    action = {
        "action_id": "hermes_plan_complete",
        "block_id": "hermes-plan-controls-r1-" + state["desired_hash"][:10],
        "selected_options": [{"value": "user:a"}],
    }
    await adapter._handle_plan_action(AsyncMock(), body, action)
    await asyncio.sleep(0)
    assert events == []

    body["user"]["id"] = "U-owner"
    body["actions"][0]["action_ts"] = f"owner-{chat_type}"
    await adapter._handle_plan_action(AsyncMock(), body, action)
    await asyncio.sleep(0)
    assert len(events) == 1
    assert events[0].source.user_id == "U-owner"
    assert events[0].source.chat_type == chat_type


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_field", ["team", "channel", "message", "thread", "block"])
async def test_plan_action_rejects_each_stale_or_wrong_route_dimension(tmp_path, invalid_field) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter, [{"id": "user:a", "content": "A", "status": "pending"}])
    state = adapter._plan_store.get_session("sk")
    adapter._plan_store.mark_applied(
        "sk", revision=1, snapshot_hash=state["desired_hash"], message_ts="20.0",
    )
    adapter.set_message_handler(AsyncMock())
    adapter._is_interactive_user_authorized = MagicMock(return_value=True)
    body = {
        "team": {"id": "T1"}, "channel": {"id": "C1"},
        "message": {"ts": "20.0", "thread_ts": "10.0"},
        "user": {"id": "U1"}, "actions": [{"action_ts": invalid_field}],
    }
    block_id = "hermes-plan-controls-r1-" + state["desired_hash"][:10]
    if invalid_field == "team":
        body["team"]["id"] = "WRONG"
    elif invalid_field == "channel":
        body["channel"]["id"] = "WRONG"
    elif invalid_field == "message":
        body["message"]["ts"] = "WRONG"
    elif invalid_field == "thread":
        body["message"]["thread_ts"] = "WRONG"
    else:
        block_id = "hermes-plan-controls-r0-stale"

    await adapter._handle_plan_action(AsyncMock(), body, {
        "action_id": "hermes_plan_complete", "block_id": block_id,
        "selected_options": [{"value": "user:a"}],
    })
    adapter._message_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_action_queues_silently_when_original_session_is_busy(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter, [{"id": "user:a", "content": "A", "status": "pending"}])
    state = adapter._plan_store.get_session("sk")
    adapter._plan_store.mark_applied(
        "sk", revision=1, snapshot_hash=state["desired_hash"], message_ts="20.0",
    )
    adapter.set_message_handler(AsyncMock())
    adapter._is_interactive_user_authorized = MagicMock(return_value=True)
    adapter.set_busy_session_handler(AsyncMock(return_value=False))
    source = adapter.build_source(
        chat_id="C1", chat_type="group", user_id="U-owner",
        thread_id="10.0", scope_id="T1",
    )
    session_key = build_session_key(source)
    adapter._active_sessions[session_key] = asyncio.Event()
    body = {
        "team": {"id": "T1"}, "channel": {"id": "C1"},
        "message": {"ts": "20.0", "thread_ts": "10.0"},
        "user": {"id": "U-owner"}, "actions": [{"action_ts": "busy"}],
    }
    action = {
        "action_id": "hermes_plan_complete",
        "block_id": "hermes-plan-controls-r1-" + state["desired_hash"][:10],
        "selected_options": [{"value": "user:a"}],
    }

    await adapter._handle_plan_action(AsyncMock(), body, action)
    await asyncio.sleep(0)
    assert adapter._pending_messages[session_key].internal is True
    adapter._message_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_modal_is_signed_and_submission_tamper_fails_closed(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    _state(adapter)
    adapter._plan_store.mark_applied(
        "sk", revision=1,
        snapshot_hash=adapter._plan_store.get_session("sk")["desired_hash"],
        message_ts="20.0",
    )
    adapter._is_interactive_user_authorized = MagicMock(return_value=True)
    client = adapter._team_clients["T1"]
    client.views_open = AsyncMock()
    body = {
        "trigger_id": "trigger",
        "team": {"id": "T1"}, "channel": {"id": "C1"},
        "message": {"ts": "20.0", "thread_ts": "10.0"},
        "user": {"id": "U-owner"}, "actions": [{"action_ts": "1"}],
    }
    block_id = "hermes-plan-controls-r1-" + adapter._plan_store.get_session("sk")["desired_hash"][:10]
    await adapter._handle_plan_action(AsyncMock(), body, {
        "action_id": "hermes_plan_add", "block_id": block_id,
    })
    view = client.views_open.call_args.kwargs["view"]
    assert view["callback_id"] == "hermes_plan_add_task"
    assert view["title"]["text"] == "Add user task"
    envelope = json.loads(view["private_metadata"])
    assert envelope["signature"]
    payload = verify_private_metadata(view["private_metadata"], b"signing-secret")
    generated_id = payload["add_task_ids"][0]
    assert is_user_task_id(generated_id)
    assert payload["task_ids"] == [generated_id]
    assert payload["action_kind"] == "add_user_task"
    assert payload["action_user_id"] == "U-owner"
    assert generated_id not in {
        task["id"] for task in adapter._plan_store.get_session("sk")["last_desired_snapshot"]
    }

    events = []
    async def handle(event):
        events.append(event)

    adapter.set_message_handler(handle)
    rejected_body = {
        "team": {"id": "T1"}, "user": {"id": "U-other"},
        "view": {
            "id": "V-other", "hash": "H-other",
            "private_metadata": view["private_metadata"],
            "state": {"values": {"task": {"content": {"value": "Wrong owner"}}}},
        },
    }
    await adapter._handle_plan_add_view(AsyncMock(), rejected_body, AsyncMock())
    await asyncio.sleep(0)
    assert events == []

    submit_body = {
        "team": {"id": "T1"}, "user": {"id": "U-owner"},
        "view": {
            "private_metadata": view["private_metadata"],
            "state": {"values": {"task": {"content": {"value": "New task"}}}},
        },
    }
    ack = AsyncMock()
    await adapter._handle_plan_add_view(ack, submit_body, AsyncMock())
    await asyncio.sleep(0)
    assert ack.await_count == 1
    assert len(events) == 1
    trusted = events[0].metadata["slack_plan_action"]
    assert trusted["action_kind"] == "add_user_task"
    assert trusted["add_task_ids"] == [generated_id]
    assert trusted["task_ids"] == [generated_id]
    assert trusted["add_task_content"] == "New task"
    assert generated_id in events[0].text

    await adapter._handle_plan_add_view(ack, submit_body, AsyncMock())
    assert len(events) == 1

    tampered = json.loads(view["private_metadata"])
    tampered["payload"]["add_task_ids"] = ["user:replacement"]
    submit_body["view"]["private_metadata"] = json.dumps(tampered)
    await adapter._handle_plan_add_view(ack, submit_body, AsyncMock())
    assert len(events) == 1


@pytest.mark.asyncio
async def test_inactive_user_tasks_do_not_block_pending_action_or_add_modal(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    inactive = [
        {
            "id": f"user:inactive-{index}",
            "content": str(index),
            "status": "cancelled" if index % 2 else "in_progress",
        }
        for index in range(15)
    ]
    state = _state(adapter, inactive + [
        {"id": "user:pending", "content": "Pending", "status": "pending"},
    ])
    assert adapter._plan_store.mark_applied(
        "sk", revision=state["desired_revision"],
        snapshot_hash=state["desired_hash"], message_ts="20.0",
    )
    events = []

    async def handle(event):
        events.append(event)

    adapter.set_message_handler(handle)
    adapter._is_interactive_user_authorized = MagicMock(return_value=True)
    client = adapter._team_clients["T1"]
    client.views_open = AsyncMock()
    body = {
        "trigger_id": "trigger", "team": {"id": "T1"}, "channel": {"id": "C1"},
        "message": {"ts": "20.0", "thread_ts": "10.0"},
        "user": {"id": "U-owner"}, "actions": [{"action_ts": "complete"}],
    }
    block_id = "hermes-plan-controls-r1-" + state["desired_hash"][:10]
    await adapter._handle_plan_action(AsyncMock(), body, {
        "action_id": "hermes_plan_complete", "block_id": block_id,
        "selected_options": [{"value": "user:pending"}],
    })
    await asyncio.sleep(0)
    assert len(events) == 1
    assert events[0].metadata["slack_plan_action"]["complete_task_ids"] == ["user:pending"]

    body["actions"][0]["action_ts"] = "add"
    await adapter._handle_plan_action(AsyncMock(), body, {
        "action_id": "hermes_plan_add", "block_id": block_id,
    })
    client.views_open.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_modal_keeps_validated_lineage_across_concurrent_route_change(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    original = _state(adapter)
    assert adapter._plan_store.mark_applied(
        "sk", revision=original["desired_revision"],
        snapshot_hash=original["desired_hash"], message_ts="20.0",
    )
    payload = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0", "message_ts": "20.0",
        "revision": original["desired_revision"],
        "snapshot_hash": original["desired_hash"], "task_ids": ["user:new"],
        "action_kind": "add_user_task", "add_task_ids": ["user:new"],
        "action_user_id": "U-owner", "action_dedupe_id": "open-dedupe",
    }
    private_metadata = sign_private_metadata(payload, b"signing-secret")
    assert adapter._plan_store.consume_action_id("open-dedupe")
    adapter._is_interactive_user_authorized = MagicMock(return_value=True)
    events = []

    async def handle(event):
        events.append(event)

    adapter.set_message_handler(handle)
    validate = adapter._plan_store.validate_action
    changed = False

    def validate_then_change(metadata):
        nonlocal changed
        validated = validate(metadata)
        if validated and not changed:
            changed = True
            adapter.record_desired_plan_snapshot(
                session_key="sk", session_id="sid-new", team_id="T1",
                channel_id="C2", thread_ts="11.0", route_user_id="U-owner",
                chat_type="group",
                todos=[{"id": "b", "content": "New", "status": "pending"}],
            )
        return validated

    adapter._plan_store.validate_action = MagicMock(side_effect=validate_then_change)
    await adapter._handle_plan_add_view(AsyncMock(), {
        "team": {"id": "T1"}, "user": {"id": "U-owner"},
        "view": {
            "id": "V-race", "hash": "H-race", "private_metadata": private_metadata,
            "state": {"values": {"task": {"content": {"value": "Old modal"}}}},
        },
    })
    await asyncio.sleep(0)

    assert len(events) == 1
    trusted = events[0].metadata["slack_plan_action"]
    assert trusted["session_id"] == "sid"
    assert trusted["channel_id"] == "C1"
    assert trusted["thread_ts"] == "10.0"
    assert trusted["revision"] == original["desired_revision"]
    assert trusted["snapshot_hash"] == original["desired_hash"]
    assert trusted["add_task_ids"] == ["user:new"]

    runner = object.__new__(GatewayRunner)
    runner.adapters = {events[0].source.platform: adapter}
    runner.session_store = MagicMock()
    runner.session_store._entries = {
        "sk": type("Entry", (), {"session_id": "sid-new"})()
    }
    adapter.send = AsyncMock()
    assert not await runner._validate_slack_plan_action_after_claim(events[0], "sk")
    adapter.send.assert_awaited_once()


def test_add_control_unavailable_without_signing_secret(tmp_path) -> None:
    adapter = _adapter(tmp_path, secret=None)
    state = _state(adapter)
    rendered = adapter._render_plan_state(state)
    action_ids = [
        element["action_id"]
        for block in rendered.native_blocks
        if block["type"] == "actions"
        for element in block["elements"]
    ]
    assert "hermes_plan_add" not in action_ids


@pytest.mark.asyncio
async def test_crafted_add_action_is_rejected_without_signing_secret(tmp_path) -> None:
    adapter = _adapter(tmp_path, secret=None)
    _state(adapter)
    state = adapter._plan_store.get_session("sk")
    adapter._plan_store.mark_applied(
        "sk", revision=1, snapshot_hash=state["desired_hash"], message_ts="20.0",
    )
    adapter._is_interactive_user_authorized = MagicMock(return_value=True)
    client = adapter._team_clients["T1"]
    client.views_open = AsyncMock()
    await adapter._handle_plan_action(AsyncMock(), {
        "trigger_id": "trigger", "team": {"id": "T1"}, "channel": {"id": "C1"},
        "message": {"ts": "20.0", "thread_ts": "10.0"},
        "user": {"id": "U-owner"}, "actions": [{"action_ts": "no-secret"}],
    }, {
        "action_id": "hermes_plan_add",
        "block_id": "hermes-plan-controls-r1-" + state["desired_hash"][:10],
    })
    client.views_open.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_plan_handlers_ack_without_mutation(tmp_path) -> None:
    adapter = _adapter(tmp_path, enabled=False)
    adapter.set_message_handler(AsyncMock())
    ack = AsyncMock()

    await adapter._handle_plan_action(ack, {
        "team": {"id": "T1"}, "channel": {"id": "C1"},
        "message": {"ts": "20.0"}, "user": {"id": "U1"},
    }, {"action_id": "hermes_plan_refresh", "block_id": "stale"})
    await adapter._handle_plan_add_view(ack, {
        "team": {"id": "T1"}, "user": {"id": "U1"}, "view": {},
    })

    assert ack.await_count == 2
    adapter._message_handler.assert_not_awaited()
    assert adapter._plan_store.list_dirty() == []
    assert adapter._plan_reconcile_task is None


def test_feature_gate_defaults_off_and_coerces_false_string(tmp_path) -> None:
    with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
        assert SlackAdapter(PlatformConfig(enabled=True, token="x", extra={}))._plan_cards_enabled is False
        assert SlackAdapter(PlatformConfig(
            enabled=True, token="x", extra={"native_plan_cards": "false"}
        ))._plan_cards_enabled is False


def test_loaded_config_constructs_enabled_adapter_with_signing_key(
    tmp_path, monkeypatch
) -> None:
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    secret = "loaded-plan-signing-secret"
    (hermes_home / "config.yaml").write_text(
        "slack:\n"
        "  native_plan_cards: true\n"
        f"  native_plan_cards_signing_secret: {secret}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)

    config = load_gateway_config()
    adapter = SlackAdapter(config.platforms[Platform.SLACK])

    assert adapter._plan_cards_enabled is True
    assert adapter._plan_signing_secret() == secret.encode("utf-8")


def _adapter_with_config_signing_secret(tmp_path, secret: str | None) -> SlackAdapter:
    extra = {}
    if secret is not None:
        extra["native_plan_cards_signing_secret"] = secret
    with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
        return SlackAdapter(PlatformConfig(enabled=True, token="x", extra=extra))


def test_unscoped_multiplex_signing_secret_uses_adapter_config_not_global_env(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret-from-other-profile")
    adapter = _adapter_with_config_signing_secret(tmp_path, "adapter-local-secret")
    was_multiplex_active = secret_scope.is_multiplex_active()
    scope_token = secret_scope.set_secret_scope(None)
    secret_scope.set_multiplex_active(True)
    try:
        assert adapter._plan_signing_secret() == b"adapter-local-secret"
    finally:
        secret_scope.reset_secret_scope(scope_token)
        secret_scope.set_multiplex_active(was_multiplex_active)


def test_unscoped_multiplex_signing_secret_fails_closed_without_adapter_config(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret-from-other-profile")
    adapter = _adapter_with_config_signing_secret(tmp_path, None)
    was_multiplex_active = secret_scope.is_multiplex_active()
    scope_token = secret_scope.set_secret_scope(None)
    secret_scope.set_multiplex_active(True)
    try:
        assert adapter._plan_signing_secret() is None
    finally:
        secret_scope.reset_secret_scope(scope_token)
        secret_scope.set_multiplex_active(was_multiplex_active)


def test_scoped_signing_secret_takes_priority_over_adapter_config(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret-from-other-profile")
    adapter = _adapter_with_config_signing_secret(tmp_path, "adapter-local-secret")
    was_multiplex_active = secret_scope.is_multiplex_active()
    scope_token = secret_scope.set_secret_scope(
        {"SLACK_SIGNING_SECRET": "properly-scoped-secret"}
    )
    secret_scope.set_multiplex_active(True)
    try:
        assert adapter._plan_signing_secret() == b"properly-scoped-secret"
    finally:
        secret_scope.reset_secret_scope(scope_token)
        secret_scope.set_multiplex_active(was_multiplex_active)


def test_signing_secret_store_failure_uses_adapter_config(tmp_path) -> None:
    adapter = _adapter_with_config_signing_secret(tmp_path, "adapter-local-secret")

    with patch(
        "plugins.platforms.slack.adapter.get_secret",
        side_effect=RuntimeError("secret store unavailable"),
    ):
        assert adapter._plan_signing_secret() == b"adapter-local-secret"


def test_plan_signing_secret_has_no_direct_process_env_fallback() -> None:
    source = inspect.getsource(SlackAdapter._plan_signing_secret)

    assert "os.getenv" not in source
