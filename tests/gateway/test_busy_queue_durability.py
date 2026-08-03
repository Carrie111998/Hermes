import asyncio
import json
import logging
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.yuanbao import RecallGuardMiddleware
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class _Adapter:
    def __init__(self) -> None:
        self._pending_messages = {}


def _source(*, profile: str | None = "alpha") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="private-chat-42",
        chat_type="dm",
        user_id="private-user-7",
        thread_id="private-thread-3",
        profile=profile,
    )


def _event(text: str, source: SessionSource, message_id: str) -> MessageEvent:
    return MessageEvent(text=text, source=source, message_id=message_id)


def _runner(profile_home, adapter: _Adapter) -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._queued_events = {}
    runner._busy_queue_root_override = profile_home
    runner._busy_queue_lock = threading.RLock()
    runner._busy_queue_uncertain_sessions = set()
    runner._busy_queue_restored_sessions = set()
    runner._adapter_for_source = lambda _source: adapter
    runner._busy_queue_max_bytes = lambda: 1024 * 1024
    return runner


def test_positive_busy_queue_receipt_is_durable_before_returning(tmp_path):
    profile_home = tmp_path / "profiles" / "alpha"
    adapter = _Adapter()
    runner = _runner(profile_home, adapter)
    source = _source()
    event = MessageEvent(
        text="payload that must survive a crash",
        message_type=MessageType.PHOTO,
        source=source,
        message_id="platform-message-9",
        media_urls=["/owned/media/photo.jpg"],
        media_types=["image/jpeg"],
        reply_to_message_id="reply-anchor-2",
        reply_to_text="quoted context",
        metadata={"transport_hint": "telegram"},
    )
    session_key = runner._session_key_for_source(source)

    accepted = runner._queue_or_replace_pending_event(session_key, event)

    assert accepted is True
    state_files = list(profile_home.rglob("*.json"))
    assert len(state_files) == 1
    state_path = state_files[0]
    assert "private-chat-42" not in state_path.name
    assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["claim"] is None
    assert payload["queue"][0]["event"]["text"] == event.text
    assert payload["queue"][0]["event"]["media_urls"] == event.media_urls
    assert payload["queue"][0]["event"]["reply_to_text"] == event.reply_to_text


def test_busy_queue_admission_without_durable_lock_fails_closed(tmp_path):
    adapter = _Adapter()
    runner = _runner(tmp_path / "profile", adapter)
    runner.__dict__["_busy_queue_lock"] = None
    source = _source()
    session_key = runner._session_key_for_source(source)

    accepted = runner._queue_or_replace_pending_event(
        session_key,
        _event("must not receive a volatile ACK", source, "no-lock-1"),
    )

    assert accepted is False
    assert adapter._pending_messages == {}
    assert runner._queued_events == {}
    assert not list((tmp_path / "profile").rglob("*.json"))


def test_restart_restores_strict_envelope_into_new_adapter_binding(tmp_path):
    profile_home = tmp_path / "profiles" / "alpha"
    first_adapter = _Adapter()
    first = _runner(profile_home, first_adapter)
    source = _source()
    first_event = MessageEvent(
        text="first",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m-1",
        platform_update_id=101,
        metadata={"nested": [1, True, None, {"ok": 2.5}]},
    )
    second_event = MessageEvent(
        text="second",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m-2",
    )
    session_key = first._session_key_for_source(source)
    assert first._queue_or_replace_pending_event(session_key, first_event)
    assert first._queue_or_replace_pending_event(session_key, second_event)

    replacement_adapter = _Adapter()
    restarted = _runner(profile_home, replacement_adapter)
    restored = restarted._restore_busy_queues([profile_home])

    assert restored == [session_key]
    recovered = replacement_adapter._pending_messages[session_key]
    assert recovered.text == "first"
    assert recovered.message_id == "m-1"
    assert recovered.platform_update_id == 101
    assert recovered.metadata == first_event.metadata
    assert recovered.raw_message is None
    assert recovered.source is not source
    assert recovered.source.chat_id == source.chat_id
    assert recovered.source.user_id == source.user_id
    assert recovered.source.profile == source.profile
    assert len(recovered._busy_queue_receipt_ids) == 1
    assert recovered._busy_queue_item_count == 1
    queued = restarted._queued_events[session_key]
    assert [item.text for item in queued] == ["second"]
    assert queued[0].message_id == "m-2"


def test_restart_preserves_parent_owner_and_thread_dispatch_source(tmp_path):
    profile_home = tmp_path / "profiles" / "alpha"
    first_adapter = _Adapter()
    first = _runner(profile_home, first_adapter)
    owner_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="private-chat-42",
        chat_type="dm",
        user_id="private-user-7",
        profile="alpha",
    )
    thread_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="private-chat-42",
        chat_type="dm",
        user_id="private-user-7",
        thread_id="private-thread-3",
        profile="alpha",
    )
    owner_key = first._session_key_for_source(owner_source)
    thread_key = first._session_key_for_source(thread_source)
    assert thread_key.startswith(f"{owner_key}:")
    assert first._queue_or_replace_pending_event(
        owner_key,
        _event("thread follow-up", thread_source, "thread-restore-1"),
    )

    payload = json.loads(next(profile_home.rglob("*.json")).read_text())
    assert payload["owner_session_key"] == owner_key

    replacement_adapter = _Adapter()
    restarted = _runner(profile_home, replacement_adapter)
    assert restarted._restore_busy_queues([profile_home]) == [owner_key]
    recovered = replacement_adapter._pending_messages[owner_key]
    assert recovered.text == "thread follow-up"
    assert recovered.source.thread_id == "private-thread-3"
    assert restarted._session_key_for_source(recovered.source) == thread_key


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-follow descriptor contract")
def test_restore_does_not_follow_state_swapped_to_symlink_after_validation(
    tmp_path, monkeypatch
):
    profile_home = tmp_path / "profiles" / "alpha"
    source = _source()
    first_adapter = _Adapter()
    first = _runner(profile_home, first_adapter)
    session_key = first._session_key_for_source(source)
    assert first._queue_or_replace_pending_event(
        session_key, _event("original payload", source, "swap-state-1")
    )
    state_path = next(profile_home.rglob("*.json"))
    held_path = state_path.with_suffix(".held")
    injected_path = tmp_path / "attacker-state.json"
    injected = json.loads(state_path.read_text(encoding="utf-8"))
    injected["queue"][0]["event"]["text"] = "injected payload"
    injected_path.write_text(json.dumps(injected), encoding="utf-8")
    os.chmod(injected_path, 0o600)

    real_lstat = Path.lstat
    swapped = False

    def racing_lstat(path, *args, **kwargs):
        nonlocal swapped
        metadata = real_lstat(path, *args, **kwargs)
        if not swapped and path == state_path:
            state_path.rename(held_path)
            state_path.symlink_to(injected_path)
            swapped = True
        return metadata

    monkeypatch.setattr(Path, "lstat", racing_lstat)
    replacement_adapter = _Adapter()
    restarted = _runner(profile_home, replacement_adapter)

    assert restarted._restore_busy_queues([profile_home]) == [session_key]
    assert replacement_adapter._pending_messages[session_key].text == "original payload"


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership contract")
def test_restore_rejects_state_not_owned_by_effective_user(tmp_path, monkeypatch):
    profile_home = tmp_path / "profiles" / "alpha"
    source = _source()
    first = _runner(profile_home, _Adapter())
    session_key = first._session_key_for_source(source)
    assert first._queue_or_replace_pending_event(
        session_key, _event("private payload", source, "foreign-owner-1")
    )
    real_fstat = os.fstat

    def foreign_directory_owner(fd):
        metadata = real_fstat(fd)
        if stat.S_ISDIR(metadata.st_mode):
            values = list(metadata)
            values[4] = metadata.st_uid + 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(os, "fstat", foreign_directory_owner)

    replacement_adapter = _Adapter()
    restarted = _runner(profile_home, replacement_adapter)

    assert restarted._restore_busy_queues([profile_home]) == []
    assert replacement_adapter._pending_messages == {}


def test_failed_strict_json_admission_preserves_prior_state_and_redacts_logs(
    tmp_path, caplog
):
    profile_home = tmp_path / "profiles" / "alpha"
    adapter = _Adapter()
    runner = _runner(profile_home, adapter)
    source = _source()
    session_key = runner._session_key_for_source(source)
    accepted = MessageEvent(text="accepted", source=source)
    assert runner._queue_or_replace_pending_event(session_key, accepted)
    state_path = next(profile_home.rglob("*.json"))
    before = state_path.read_bytes()

    secret = "USER-SECRET-MUST-NOT-BE-LOGGED"
    unsupported = MessageEvent(
        text=secret,
        source=source,
        metadata={"not_json": object()},
    )
    with caplog.at_level(logging.ERROR):
        assert runner._queue_or_replace_pending_event(session_key, unsupported) is False

    assert state_path.read_bytes() == before
    assert adapter._pending_messages[session_key] is accepted
    assert runner._queued_events.get(session_key) in (None, [])
    assert secret not in caplog.text
    assert session_key not in caplog.text
    assert runner._busy_queue_session_digest(session_key)[:10] not in caplog.text


def test_uncertain_admission_log_has_no_stable_session_correlator(tmp_path, caplog):
    runner = _runner(tmp_path / "profile", _Adapter())
    source = _source()
    session_key = runner._session_key_for_source(source)
    runner._busy_queue_uncertain_sessions.add(session_key)

    with caplog.at_level(logging.ERROR):
        assert runner._queue_or_replace_pending_event(
            session_key, _event("private", source, "uncertain-log-1")
        ) is False

    assert session_key not in caplog.text
    assert runner._busy_queue_session_digest(session_key)[:10] not in caplog.text


def test_claim_fences_restart_until_terminal_commit(tmp_path):
    profile_home = tmp_path / "profiles" / "alpha"
    adapter = _Adapter()
    runner = _runner(profile_home, adapter)
    source = _source()
    session_key = runner._session_key_for_source(source)
    first = MessageEvent(text="first", source=source)
    second = MessageEvent(text="second", source=source)
    assert runner._queue_or_replace_pending_event(session_key, first)
    assert runner._queue_or_replace_pending_event(session_key, second)

    claimed = adapter._pending_messages.pop(session_key)
    assert claimed is first
    token = runner._busy_queue_claim_event(session_key, claimed)
    assert token
    assert runner._promote_queued_event(session_key, adapter, claimed) is claimed

    state_path = next(profile_home.rglob("*.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["claim"]["token"] == token
    assert state["claim"]["event"]["event"]["text"] == "first"
    assert [item["event"]["text"] for item in state["queue"]] == ["second"]

    crash_adapter = _Adapter()
    crashed = _runner(profile_home, crash_adapter)
    assert crashed._restore_busy_queues([profile_home]) == []
    assert session_key in crashed._busy_queue_uncertain_sessions
    assert crash_adapter._pending_messages == {}

    assert runner._busy_queue_commit_claim(session_key, source, token) is True
    replacement_adapter = _Adapter()
    restarted = _runner(profile_home, replacement_adapter)
    assert restarted._restore_busy_queues([profile_home]) == [session_key]
    assert replacement_adapter._pending_messages[session_key].text == "second"


def test_central_admission_rejects_external_drain_before_mutation(tmp_path):
    """The durable admission point must fail closed for dashboard drains too."""
    profile_home = tmp_path / "profile"
    adapter = _Adapter()
    runner = _runner(profile_home, adapter)
    runner._external_drain_active = True
    source = _source()
    session_key = runner._session_key_for_source(source)

    accepted = runner._queue_or_replace_pending_event(
        session_key, _event("must-not-land", source, "drain-1")
    )

    assert accepted is False
    assert adapter._pending_messages == {}
    assert runner._queued_events == {}
    assert not list(profile_home.rglob("*.json"))


@pytest.mark.asyncio
async def test_restore_scheduler_claims_before_async_dispatch_and_keeps_fifo(tmp_path):
    """Recovered work is scheduled, never dispatched inline or without a claim."""
    profile_home = tmp_path / "profile"
    source = _source()
    original_adapter = _Adapter()
    original = _runner(profile_home, original_adapter)
    session_key = original._session_key_for_source(source)
    assert original._queue_or_replace_pending_event(
        session_key, _event("first", source, "sched-1")
    )
    assert original._queue_or_replace_pending_event(
        session_key, _event("second", source, "sched-2")
    )

    replacement_adapter = _Adapter()
    replacement_adapter.handle_message = AsyncMock()
    restarted = _runner(profile_home, replacement_adapter)
    restored = restarted._restore_busy_queues([profile_home])

    scheduled = restarted._schedule_busy_queue_replays(restored)

    assert scheduled == 1
    replacement_adapter.handle_message.assert_not_awaited()
    await asyncio.sleep(0)
    replacement_adapter.handle_message.assert_awaited_once()
    dispatched = replacement_adapter.handle_message.await_args.args[0]
    assert dispatched.text == "first"
    assert getattr(dispatched, "_hermes_busy_queue_claim_token", "")
    assert replacement_adapter._pending_messages[session_key].text == "second"

    state_path = next(profile_home.rglob("*.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["claim"]["event"]["event"]["text"] == "first"
    assert [item["event"]["text"] for item in state["queue"]] == ["second"]


@pytest.mark.asyncio
async def test_startup_restore_gate_owns_busy_replay_launch_task(tmp_path):
    """Fresh inbound stays gated until each restored busy replay is launched."""
    runner = _runner(tmp_path / "profile", _Adapter())
    source = _source()
    session_key = runner._session_key_for_source(source)
    runner._busy_queue_restored_sources = {session_key: source}
    runner._startup_restore_in_progress = True
    runner._startup_restore_tasks = []
    release = asyncio.Event()

    async def blocked_replay(session_key: str, source: SessionSource):
        del session_key, source
        await release.wait()

    runner._run_busy_queue_replay = blocked_replay

    assert runner._schedule_busy_queue_replays([session_key]) == 1
    assert runner._startup_restore_tasks == [
        runner._busy_queue_replay_tasks[session_key]
    ]

    release.set()
    await runner._startup_restore_tasks[0]


@pytest.mark.asyncio
async def test_recursive_drain_claims_before_dispatch_with_frozen_context(tmp_path):
    profile_home = tmp_path / "profile"
    source = _source()
    adapter = _Adapter()
    runner = _runner(profile_home, adapter)
    session_key = runner._session_key_for_source(source)
    assert runner._queue_or_replace_pending_event(
        session_key, _event("first", source, "recursive-1")
    )
    assert runner._queue_or_replace_pending_event(
        session_key, _event("second", source, "recursive-2")
    )

    seen = []

    async def run_claimed_event(**kwargs):
        claim_key, frozen_source, token = kwargs["busy_queue_claim"]
        assert claim_key == session_key
        assert token
        assert frozen_source is kwargs["source"]
        assert frozen_source is not source
        state_path = next(profile_home.rglob("*.json"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["claim"]["token"] == token
        assert state["claim"]["event"]["event"]["text"] == kwargs["message"]
        seen.append(kwargs["message"])
        await runner._drain_busy_queue_recursively(
            session_key=session_key,
            source=kwargs["source"],
            result={
                "completed": True,
                "receipt_terminal_success": True,
                "final_response": "done",
            },
            busy_queue_claim=kwargs["busy_queue_claim"],
            context_prompt=kwargs["context_prompt"],
            history=kwargs["history"],
            session_id=kwargs["session_id"],
            system_prompt=kwargs.get("system_prompt"),
            ephemeral_system_prompt=kwargs.get("ephemeral_system_prompt"),
            reasoning_config=kwargs.get("reasoning_config"),
            provider_routing=kwargs.get("provider_routing"),
            fallback_model=kwargs.get("fallback_model"),
            restart_context=kwargs.get("restart_context"),
        )
        return {
            "completed": True,
            "receipt_terminal_success": True,
            "final_response": "done",
        }

    runner._run_agent = AsyncMock(side_effect=run_claimed_event)

    await runner._drain_busy_queue_recursively(
        session_key=session_key,
        source=source,
        result={
            "completed": True,
            "receipt_terminal_success": True,
            "final_response": "predecessor done",
        },
        busy_queue_claim=None,
        context_prompt="context",
        history=[],
        session_id="session-1",
        system_prompt=None,
        ephemeral_system_prompt=None,
        reasoning_config=None,
        provider_routing=None,
        fallback_model=None,
        restart_context=None,
    )

    assert seen == ["first", "second"]
    assert not list(profile_home.rglob("*.json"))
    assert not adapter._pending_messages


def test_claim_finalization_commits_terminal_and_quarantines_nonterminal(tmp_path):
    profile_home = tmp_path / "profile"
    source = _source()
    adapter = _Adapter()
    runner = _runner(profile_home, adapter)
    session_key = runner._session_key_for_source(source)

    first = _event("terminal", source, "finalize-1")
    assert runner._queue_or_replace_pending_event(session_key, first)
    claimed, token = runner._busy_queue_claim_next_event(session_key, adapter)
    assert claimed is first and token
    assert runner._busy_queue_finalize_claim(
        session_key,
        source,
        token,
        {
            "completed": True,
            "receipt_terminal_success": True,
            "final_response": "done",
        },
    )
    assert not list(profile_home.rglob("*.json"))

    second = _event("uncertain", source, "finalize-2")
    assert runner._queue_or_replace_pending_event(session_key, second)
    claimed, token = runner._busy_queue_claim_next_event(session_key, adapter)
    assert claimed is second and token
    assert not runner._busy_queue_finalize_claim(session_key, source, token, None)
    assert session_key in runner._busy_queue_uncertain_sessions
    assert not adapter._pending_messages


def test_terminal_result_requires_explicit_receipt_success(tmp_path):
    runner = _runner(tmp_path / "profile", _Adapter())

    assert not runner._busy_queue_result_is_terminal(
        {"completed": True, "final_response": "legacy result"}
    )
    assert runner._busy_queue_result_is_terminal(
        {
            "completed": True,
            "receipt_terminal_success": True,
            "failed": False,
            "partial": False,
            "interrupted": False,
            "cleanup_errors": [],
            "final_response": "done",
        }
    )


def test_terminal_discard_result_is_canonical_and_committable(tmp_path):
    runner = _runner(tmp_path / "profile", _Adapter())

    result = runner._busy_queue_terminal_discard_result()

    assert result == {
        "completed": True,
        "receipt_terminal_success": True,
    }
    assert runner._busy_queue_result_is_terminal(result)


def test_duplicate_finalizer_is_stale_and_cannot_authorize_progress(tmp_path):
    source = _source()
    adapter = _Adapter()
    runner = _runner(tmp_path / "profile", adapter)
    session_key = runner._session_key_for_source(source)
    terminal = {
        "completed": True,
        "receipt_terminal_success": True,
        "failed": False,
        "partial": False,
        "interrupted": False,
        "cleanup_errors": [],
        "final_response": "done",
    }

    assert runner._queue_or_replace_pending_event(
        session_key, _event("once", source, "duplicate-finalizer-1")
    )
    _claimed, token = runner._busy_queue_claim_next_event(session_key, adapter)
    assert token
    assert runner._busy_queue_finalize_claim(session_key, source, token, terminal)
    assert not runner._busy_queue_finalize_claim(session_key, source, token, terminal)


@pytest.mark.asyncio
async def test_stale_finalizer_cannot_authorize_current_claim(tmp_path):
    profile_home = tmp_path / "profile"
    source = _source()
    adapter = _Adapter()
    runner = _runner(profile_home, adapter)
    session_key = runner._session_key_for_source(source)
    terminal = {
        "completed": True,
        "receipt_terminal_success": True,
        "failed": False,
        "partial": False,
        "interrupted": False,
        "cleanup_errors": [],
        "final_response": "done",
    }

    assert runner._queue_or_replace_pending_event(
        session_key, _event("first", source, "stale-finalizer-1")
    )
    assert runner._queue_or_replace_pending_event(
        session_key, _event("second", source, "stale-finalizer-2")
    )
    _first, stale_token = runner._busy_queue_claim_next_event(session_key, adapter)
    assert stale_token
    assert runner._busy_queue_finalize_claim(
        session_key, source, stale_token, terminal
    )
    second, current_token = runner._busy_queue_claim_next_event(session_key, adapter)
    assert second is not None and second.text == "second"
    assert current_token and current_token != stale_token

    runner._run_agent = AsyncMock(return_value=terminal)
    await runner._drain_busy_queue_recursively(
        session_key=session_key,
        source=source,
        result=terminal,
        busy_queue_claim=(session_key, source, stale_token),
        context_prompt="context",
        history=[],
        session_id="session-1",
        system_prompt=None,
        ephemeral_system_prompt=None,
        reasoning_config=None,
        provider_routing=None,
        fallback_model=None,
        restart_context=None,
    )

    runner._run_agent.assert_not_awaited()
    state_path = next(profile_home.rglob("*.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["claim"]["token"] == current_token


def test_claim_finalization_quarantines_partial_result_with_response(tmp_path):
    profile_home = tmp_path / "profile"
    source = _source()
    adapter = _Adapter()
    runner = _runner(profile_home, adapter)
    session_key = runner._session_key_for_source(source)
    event = _event("partial", source, "finalize-partial-1")
    assert runner._queue_or_replace_pending_event(session_key, event)
    _claimed, token = runner._busy_queue_claim_next_event(session_key, adapter)
    assert token

    finalized = runner._busy_queue_finalize_claim(
        session_key,
        source,
        token,
        {
            "completed": False,
            "partial": True,
            "final_response": "only a partial answer",
        },
    )

    assert finalized is False
    assert session_key in runner._busy_queue_uncertain_sessions
    state_path = next(profile_home.rglob("*.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["claim"]["token"] == token


@pytest.mark.asyncio
async def test_run_agent_inner_commits_claim_only_at_terminal_worker_boundary(tmp_path):
    """The terminal worker, not the async launcher, owns durable claim commit."""
    profile_home = tmp_path / "profile"
    source = _source()
    adapter = _Adapter()
    runner = _runner(profile_home, adapter)
    session_key = runner._session_key_for_source(source)

    assert runner._queue_or_replace_pending_event(
        session_key, _event("run me", source, "worker-boundary-1")
    )
    _claimed, token = runner._busy_queue_claim_next_event(session_key, adapter)
    assert token

    runner._get_proxy_url = lambda: "http://proxy.invalid"
    runner._run_agent_via_proxy = AsyncMock(
        return_value={
            "completed": True,
            "receipt_terminal_success": True,
            "final_response": "done",
            "messages": [],
        }
    )

    result = await runner._run_agent_inner(
        message="run me",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
        session_key=session_key,
        busy_queue_claim=(session_key, source, token),
    )

    assert result["final_response"] == "done"
    assert not list((profile_home / "state" / "busy_queue").glob("*.json"))


@pytest.mark.asyncio
async def test_proxy_terminal_boundary_claims_and_dispatches_fifo_successor(tmp_path):
    """Proxy execution drains durable successors instead of returning after one turn."""
    profile_home = tmp_path / "profile"
    source = _source()
    adapter = _Adapter()
    runner = _runner(profile_home, adapter)
    session_key = runner._session_key_for_source(source)
    assert runner._queue_or_replace_pending_event(
        session_key, _event("first", source, "proxy-drain-1")
    )
    assert runner._queue_or_replace_pending_event(
        session_key, _event("second", source, "proxy-drain-2")
    )
    _first, first_token = runner._busy_queue_claim_next_event(session_key, adapter)
    assert first_token

    runner._get_proxy_url = lambda: "http://proxy.invalid"
    runner._run_agent_via_proxy = AsyncMock(
        return_value={
            "completed": True,
            "receipt_terminal_success": True,
            "final_response": "first done",
            "messages": [],
        }
    )
    runner._run_agent = AsyncMock(
        return_value={
            "completed": True,
            "receipt_terminal_success": True,
            "final_response": "second done",
            "messages": [],
        }
    )

    await runner._run_agent_inner(
        message="first",
        context_prompt="context",
        history=[],
        source=source,
        session_id="session-1",
        session_key=session_key,
        busy_queue_claim=(session_key, source, first_token),
    )

    runner._run_agent.assert_awaited_once()
    assert runner._run_agent.await_args is not None
    followup = runner._run_agent.await_args.kwargs
    assert followup["message"] == "second"
    claim_key, claim_source, claim_token = followup["busy_queue_claim"]
    assert claim_key == session_key
    assert claim_source is not source
    assert claim_token


@pytest.mark.asyncio
async def test_run_agent_carries_frozen_claim_context_to_inner_worker(tmp_path):
    source = _source()
    runner = _runner(tmp_path / "profile", _Adapter())
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._smart_active_missions = {}
    runner._run_agent_inner = AsyncMock(
        return_value={"final_response": "done", "messages": []}
    )
    session_key = runner._session_key_for_source(source)
    claim = (session_key, source, "claim-token")

    await runner._run_agent(
        message="queued",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
        session_key=session_key,
        busy_queue_claim=claim,
    )

    assert runner._run_agent_inner.await_args.kwargs["busy_queue_claim"] == claim


@pytest.mark.asyncio
async def test_nonproxy_exception_quarantines_claim_instead_of_dropping_it(
    tmp_path, monkeypatch
):
    profile_home = tmp_path / "profile"
    source = _source()
    adapter = _Adapter()
    runner = _runner(profile_home, adapter)
    session_key = runner._session_key_for_source(source)
    assert runner._queue_or_replace_pending_event(
        session_key, _event("non-proxy", source, "nonproxy-1")
    )
    _claimed, token = runner._busy_queue_claim_next_event(session_key, adapter)
    assert token

    runner._get_proxy_url = lambda: None

    def fail_config_load():
        raise RuntimeError("deterministic non-proxy setup failure")

    monkeypatch.setattr("gateway.run._load_gateway_config", fail_config_load)

    with pytest.raises(RuntimeError, match="deterministic non-proxy setup failure"):
        await runner._run_agent_inner(
            message="non-proxy",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            session_key=session_key,
            busy_queue_claim=(session_key, source, token),
        )

    assert session_key in runner._busy_queue_uncertain_sessions
    state_path = next(profile_home.rglob("*.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["claim"]["token"] == token


@pytest.mark.asyncio
async def test_interrupt_tombstones_durable_queue_before_agent_interrupt(tmp_path):
    profile_home = tmp_path / "profile"
    source = _source()
    adapter = _Adapter()
    runner = _runner(profile_home, adapter)
    session_key = runner._session_key_for_source(source)
    runner._pending_messages = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._smart_active_missions = {}
    runner._active_session_leases = {}
    runner._persist_active_agents = lambda: None
    runner._invalidate_session_run_generation = MagicMock()
    runner._release_running_agent_state = MagicMock()
    runner._evict_cached_agent = MagicMock()

    order = []
    assert runner._queue_or_replace_pending_event(
        session_key, _event("cancel-me", source, "cancel-1")
    )
    real_cancel = runner._busy_queue_cancel_session

    def cancel(*args, **kwargs):
        order.append("durable-cancel")
        return real_cancel(*args, **kwargs)

    runner._busy_queue_cancel_session = MagicMock(side_effect=cancel)
    agent = MagicMock()
    agent.interrupt.side_effect = lambda *_: order.append("agent-interrupt")
    runner._running_agents[session_key] = agent

    await runner._interrupt_and_clear_session(
        session_key,
        source,
        interrupt_reason="stop",
        invalidation_reason="test",
    )

    assert order[:2] == ["durable-cancel", "agent-interrupt"]
    assert adapter._pending_messages == {}
    assert not list(profile_home.rglob("*.json"))


def test_cancelled_claim_token_fences_late_worker_finalizer(tmp_path):
    """A stale worker cannot resurrect or quarantine an explicitly cancelled claim."""
    profile_home = tmp_path / "profile"
    source = _source()
    adapter = _Adapter()
    runner = _runner(profile_home, adapter)
    session_key = runner._session_key_for_source(source)
    assert runner._queue_or_replace_pending_event(
        session_key, _event("cancel-active", source, "cancel-active-1")
    )
    _claimed, token = runner._busy_queue_claim_next_event(session_key, adapter)
    assert token

    assert runner._busy_queue_cancel_session(session_key, source, adapter)
    assert token in runner._busy_queue_cancelled_claim_tokens
    assert not runner._busy_queue_finalize_claim(
        session_key,
        source,
        token,
        {"completed": False, "interrupted": True},
    )

    assert session_key not in runner._busy_queue_uncertain_sessions
    assert not list(profile_home.rglob("*.json"))


@pytest.mark.asyncio
async def test_cancel_session_cancels_not_yet_dispatched_replay(tmp_path):
    adapter = _Adapter()
    runner = _runner(tmp_path, adapter)
    source = _source()
    session_key = runner._session_key_for_source(source)
    release = asyncio.Event()

    async def pending_replay():
        await release.wait()

    replay_task = asyncio.create_task(pending_replay())
    runner._busy_queue_replay_tasks = {session_key: replay_task}

    assert runner._busy_queue_cancel_session(session_key, source, adapter) is True
    await asyncio.sleep(0)

    assert replay_task.cancelled()
    assert session_key not in runner._busy_queue_replay_tasks


@pytest.mark.asyncio
async def test_cancelled_claimed_replay_does_not_recreate_uncertain_fence(tmp_path):
    profile_home = tmp_path / "profile"
    adapter = _Adapter()
    runner = _runner(profile_home, adapter)
    source = _source()
    session_key = runner._session_key_for_source(source)
    started = asyncio.Event()
    block = asyncio.Event()

    async def handle_message(_event):
        started.set()
        await block.wait()

    cast(Any, adapter).handle_message = handle_message
    assert runner._queue_or_replace_pending_event(
        session_key,
        _event("cancel after claim", source, "cancel-race-1"),
    )

    replay_task = asyncio.create_task(
        runner._run_busy_queue_replay(session_key, source)
    )
    runner._busy_queue_replay_tasks = {session_key: replay_task}
    await asyncio.wait_for(started.wait(), timeout=2)
    token = runner._busy_queue_active_claims[session_key]

    assert runner._busy_queue_cancel_session(session_key, source, adapter) is True
    with pytest.raises(asyncio.CancelledError):
        await replay_task

    assert token in runner._busy_queue_cancelled_claim_tokens
    assert session_key not in runner._busy_queue_uncertain_sessions
    assert not list(profile_home.rglob("*.json"))


@pytest.mark.asyncio
async def test_internal_synthetic_is_durably_admitted_before_return(tmp_path):
    profile_home = tmp_path / "profile"
    source = _source()
    adapter = _Adapter()
    runner = _runner(profile_home, adapter)
    runner._is_user_authorized = lambda _source: True
    runner._draining = False
    runner._running_agents = {}
    runner._busy_input_mode = "smart"
    runner._busy_text_mode = "interrupt"
    session_key = runner._session_key_for_source(source)
    runner._running_agents[session_key] = MagicMock()
    event = _event("background completion", source, "internal-1")
    event.internal = True

    handled = await runner._handle_active_session_busy_message(event, session_key)

    assert handled is True
    state_path = runner._busy_queue_state_path(session_key, source)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["claim"] is None
    assert state["queue"][0]["event"]["text"] == "background completion"
    assert state["queue"][0]["event"]["internal"] is True
    assert adapter._pending_messages[session_key] is event


def test_yuanbao_recall_is_durable_front_inserted_before_interrupt(tmp_path):
    profile_home = tmp_path / "profile"
    source = SessionSource(
        platform=Platform.YUANBAO,
        chat_id="group:private-group",
        chat_type="group",
        user_id="private-user",
        thread_id="main",
        profile="alpha",
    )
    adapter = cast(Any, _Adapter())
    adapter.name = "Yuanbao"
    adapter.build_source = lambda **_kwargs: source
    adapter._processing_msg_texts = {}
    adapter._background_tasks = set()
    runner = _runner(profile_home, adapter)
    runner._running_agents = {}
    adapter.gateway_runner = runner
    session_key = runner._session_key_for_source(source)
    user_followup = _event("existing user follow-up", source, "user-1")
    assert runner._queue_or_replace_pending_event(session_key, user_followup)

    state_path = runner._busy_queue_state_path(session_key, source)
    order_seen_at_interrupt = []
    active_event = MagicMock()

    def capture_durable_order():
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        order_seen_at_interrupt.append(
            [item["event"]["text"] for item in payload["queue"]]
        )

    active_event.set.side_effect = capture_durable_order
    adapter._active_sessions = {session_key: active_event}

    RecallGuardMiddleware._interrupt_for_recall(
        adapter,
        session_key,
        "recalled-platform-id",
        "private-group",
        "private-user",
    )

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    durable_texts = [item["event"]["text"] for item in payload["queue"]]
    assert "MESSAGE RECALLED" in durable_texts[0]
    assert durable_texts[1] == "existing user follow-up"
    assert order_seen_at_interrupt == [durable_texts]
    assert "MESSAGE RECALLED" in adapter._pending_messages[session_key].text
    assert runner._queued_events[session_key][0].text == "existing user follow-up"

    replacement = _Adapter()
    replacement_runner = _runner(profile_home, replacement)
    assert replacement_runner._restore_busy_queues([profile_home]) == [session_key]
    assert "MESSAGE RECALLED" in replacement._pending_messages[session_key].text
    assert replacement_runner._queued_events[session_key][0].text == "existing user follow-up"


@pytest.mark.asyncio
async def test_interrupted_handoff_dispatches_preclaimed_event_exactly_once(tmp_path):
    adapter = _Adapter()
    runner = _runner(tmp_path / "profile", adapter)
    source = _source()
    session_key = runner._session_key_for_source(source)
    event = _event("durable interrupt payload", source, "interrupt-1")
    assert runner._queue_or_replace_pending_event(session_key, event)
    claimed_event, token = runner._busy_queue_claim_next_event(session_key, adapter)
    assert claimed_event is event
    assert token is not None
    pending_claim = (session_key, event.source, token)
    terminal_result = {
        "completed": True,
        "receipt_terminal_success": True,
        "failed": False,
        "partial": False,
        "interrupted": False,
        "cleanup_errors": [],
    }
    runner._run_agent = AsyncMock(return_value=terminal_result)

    followup = await runner._drain_busy_queue_recursively(
        session_key=session_key,
        source=source,
        result={
            "completed": False,
            "receipt_terminal_success": False,
            "failed": False,
            "partial": True,
            "interrupted": True,
            "interrupt_message": event.text,
            "cleanup_errors": [],
        },
        busy_queue_claim=None,
        context_prompt="context",
        history=[],
        session_id="session-1",
        run_generation=7,
        interrupt_depth=0,
        pending_event=event,
        pending_claim=pending_claim,
        prepared_message=event.text,
        updated_history=[],
        event_message_id=event.message_id,
    )

    assert followup == terminal_result
    runner._run_agent.assert_awaited_once()
    kwargs = runner._run_agent.await_args.kwargs
    assert kwargs["message"] == event.text
    assert kwargs["busy_queue_claim"] == pending_claim


def test_interrupting_event_supersedes_active_claim_before_signal(tmp_path):
    adapter = cast(Any, _Adapter())
    runner = _runner(tmp_path / "profile", adapter)
    source = _source()
    session_key = runner._session_key_for_source(source)
    predecessor = _event("claimed predecessor", source, "claim-old")
    successor = _event("new interrupt", source, "interrupt-new")
    assert runner._queue_or_replace_pending_event(session_key, predecessor)
    claimed, old_token = runner._busy_queue_claim_next_event(session_key, adapter)
    assert claimed is predecessor
    assert old_token is not None

    path = runner._busy_queue_state_path(session_key, source)
    order_seen_at_signal = []
    signal = MagicMock()

    def capture_state():
        state = json.loads(path.read_text(encoding="utf-8"))
        order_seen_at_signal.append(
            (
                state["claim"],
                [item["event"]["text"] for item in state["queue"]],
            )
        )

    signal.set.side_effect = capture_state
    adapter._active_sessions = {session_key: signal}

    assert runner._admit_interrupting_event(session_key, successor)
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["claim"] is None
    assert [item["event"]["text"] for item in state["queue"]] == [successor.text]
    assert order_seen_at_signal == [(None, [successor.text])]
    assert old_token in runner._busy_queue_cancelled_claim_tokens

    assert not runner._busy_queue_finalize_claim(
        session_key,
        source,
        old_token,
        {
            "completed": False,
            "receipt_terminal_success": False,
            "interrupted": True,
        },
    )
    next_event, next_token = runner._busy_queue_claim_next_event(session_key, adapter)
    assert next_event is successor
    assert next_token is not None and next_token != old_token
