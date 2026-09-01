"""Gateway-side session binding for async delegations (#57498, #55578).

Three invariants on the messaging-gateway surface, mirroring the TUI rules:

1. Completions are pinned to the spawning session (contributor commit).
2. A dead/ended spawning session is never resurrected: the injection is
   dropped, fail-closed (never rerouted to the peer's current session).
3. /new interrupts the old conversation's in-flight async delegations.
"""

import queue
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tools.async_delegation as ad


@pytest.fixture(autouse=True)
def _reset_async_delegation():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _seed_record(delegation_id, session_key="", parent_session_id="", status="running"):
    fn = MagicMock()
    with ad._records_lock:
        ad._records[delegation_id] = {
            "delegation_id": delegation_id,
            "status": status,
            "session_key": session_key,
            "parent_session_id": parent_session_id,
            "interrupt_fn": fn,
        }
    return fn


SLACK_ROUTE = {
    "platform": "slack",
    "chat_id": "C0BTQQX0SLC",
    "chat_type": "thread",
    "thread_id": "1788217797.757469",
    "message_id": "1788217900.000001",
}


def test_capture_routing_origin_includes_exact_slack_parent(monkeypatch):
    env = {
        "HERMES_SESSION_PLATFORM": "slack",
        "HERMES_SESSION_CHAT_ID": "C0BTQQX0SLC",
        "HERMES_SESSION_CHAT_TYPE": "thread",
        "HERMES_SESSION_THREAD_ID": "1788217797.757469",
        "HERMES_SESSION_MESSAGE_ID": "1788217900.000001",
    }
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": env.get(name, default),
    )

    assert ad._capture_routing_origin() == SLACK_ROUTE


@pytest.mark.parametrize("is_batch", [False, True], ids=["single", "batch"])
def test_completion_and_restart_replay_preserve_exact_slack_thread(
    monkeypatch, is_batch,
):
    from tools.process_registry import process_registry

    now = time.time()
    record = {
        "delegation_id": f"d-{'batch' if is_batch else 'single'}",
        "goal": "check routing",
        "goals": ["check routing"] if is_batch else None,
        "context": None,
        "toolsets": None,
        "role": "researcher",
        "model": "test-model",
        "is_batch": is_batch,
        "session_key": "agent:main:slack:thread:C0BTQQX0SLC:1788217797.757469",
        "origin_ui_session_id": "",
        "origin_session_id": "origin-session",
        "parent_session_id": "parent-session",
        "status": "running",
        "dispatched_at": now,
        "completed_at": now,
        **SLACK_ROUTE,
    }
    ad._persist_dispatch(record)
    live_queue = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", live_queue)

    if is_batch:
        ad._push_batch_completion_event(
            record,
            {"results": [{"status": "completed", "summary": "done"}]},
            "completed",
        )
    else:
        ad._push_completion_event(
            record,
            {"status": "completed", "summary": "done"},
            "completed",
        )

    live_event = live_queue.get_nowait()
    assert {key: live_event.get(key) for key in SLACK_ROUTE} == SLACK_ROUTE

    restarted_queue = queue.Queue()
    assert ad.restore_undelivered_completions(restarted_queue) == 1
    restored_event = restarted_queue.get_nowait()
    assert restored_event["restored"] is True
    assert {key: restored_event.get(key) for key in SLACK_ROUTE} == SLACK_ROUTE


def _source_runner(session_key, origin=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    runner.session_store._entries = (
        {session_key: MagicMock(origin=origin)} if origin is not None else {}
    )
    runner._get_cached_session_source = MagicMock(return_value=None)
    return runner


def test_persisted_async_route_overrides_stale_session_origin():
    from gateway.config import Platform
    from gateway.session import SessionSource

    session_key = "agent:main:slack:thread:C0BTQQX0SLC:stale"
    stale_origin = SessionSource(
        platform=Platform.SLACK,
        chat_id="C0BTQQX0SLC",
        chat_type="channel",
        thread_id=None,
    )
    source = _source_runner(session_key, stale_origin)._build_process_event_source(
        {"session_key": session_key, **SLACK_ROUTE}
    )

    assert source is not None
    assert source is not stale_origin
    assert source.chat_type == "thread"
    assert source.thread_id == "1788217797.757469"
    assert source.message_id == "1788217900.000001"
    assert source.strict_machine_thread_affinity is True


def test_unanchored_async_route_is_still_marked_fail_closed():
    from gateway.config import Platform
    from gateway.platforms.base import strict_machine_thread_affinity_error

    source = _source_runner("")._build_process_event_source(
        {
            "platform": "slack",
            "chat_id": "C0BTQQX0SLC",
            "chat_type": "channel",
        }
    )

    assert source is not None
    assert source.thread_id is None
    assert source.message_id is None
    assert source.strict_machine_thread_affinity is True
    metadata = _source_runner("")._thread_metadata_for_source(source)
    assert strict_machine_thread_affinity_error(
        Platform.SLACK, metadata, None
    ) is not None


class TestInterruptForSessionByParentId:
    def test_parent_session_id_selector(self):
        mine = _seed_record("d1", session_key="agent:main:telegram:dm:1", parent_session_id="sess_old")
        other = _seed_record("d2", session_key="agent:main:telegram:dm:2", parent_session_id="sess_other")
        n = ad.interrupt_for_session(parent_session_id="sess_old")
        assert n == 1
        mine.assert_called_once()
        other.assert_not_called()


class TestGatewayPinningFailsClosed:
    """The gateway must follow only verified compression continuations."""

    @staticmethod
    def _entry(session_id):
        from datetime import datetime

        from gateway.config import Platform
        from gateway.session import SessionEntry

        return SessionEntry(
            session_key="agent:main:telegram:group:-100:4",
            session_id=session_id,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            platform=Platform.TELEGRAM,
            chat_type="group",
        )

    def _make_runner(
        self,
        rows,
        *,
        compression_tip=None,
        compression_error=None,
        switched_entry=None,
    ):
        from gateway.run import GatewayRunner
        from gateway.session import AsyncSessionStore

        runner = object.__new__(GatewayRunner)
        db = MagicMock()
        db.get_session = AsyncMock(side_effect=lambda session_id: rows.get(session_id))
        db.get_compression_tip = AsyncMock(
            return_value=compression_tip,
            side_effect=compression_error,
        )
        runner._session_db = db
        runner.session_store = MagicMock()
        runner.session_store.switch_session = MagicMock(return_value=switched_entry)
        runner.session_store.advance_compression_session = MagicMock(
            return_value=switched_entry
        )
        runner._async_session_store = AsyncSessionStore(runner.session_store)
        return runner

    @staticmethod
    def _assert_no_route_change(runner):
        getattr(runner.session_store, "switch_session").assert_not_called()
        getattr(
            runner.session_store, "advance_compression_session"
        ).assert_not_called()


    @pytest.mark.asyncio
    async def test_live_spawning_session_rebinds_from_different_route(self):
        current = self._entry("sess_current")
        pinned = self._entry("sess_live")
        runner = self._make_runner(
            {"sess_live": {"id": "sess_live", "ended_at": None}},
            switched_entry=pinned,
        )

        resolved = await runner._resolve_async_delegation_session(
            current, "sess_live"
        )

        assert resolved is pinned
        getattr(runner.session_store, "switch_session").assert_called_once_with(
            current.session_key, "sess_live"
        )

    @pytest.mark.asyncio
    async def test_non_compression_ended_parent_drops(self):
        current = self._entry("sess_old")
        runner = self._make_runner(
            {
                "sess_old": {
                    "id": "sess_old",
                    "ended_at": "2026-07-08T00:00:00",
                    "end_reason": "session_reset",
                }
            }
        )

        resolved = await runner._resolve_async_delegation_session(
            current, "sess_old"
        )

        assert resolved is None
        self._assert_no_route_change(runner)


    @pytest.mark.asyncio
    async def test_intermediate_compression_route_advances_to_same_live_tip(self):
        current = self._entry("sess_middle")
        tip = self._entry("sess_tip")
        runner = self._make_runner(
            {
                "sess_parent": {
                    "id": "sess_parent",
                    "ended_at": "2026-07-08T00:00:00",
                    "end_reason": "compression",
                },
                "sess_middle": {
                    "id": "sess_middle",
                    "ended_at": "2026-07-08T00:01:00",
                    "end_reason": "compression",
                    "parent_session_id": "sess_parent",
                },
                "sess_tip": {
                    "id": "sess_tip",
                    "ended_at": None,
                    "parent_session_id": "sess_middle",
                },
            },
            compression_tip="sess_tip",
            switched_entry=tip,
        )

        resolved = await runner._resolve_async_delegation_session(
            current, "sess_parent"
        )

        assert resolved is tip
        getattr(
            runner.session_store, "advance_compression_session"
        ).assert_called_once_with(current.session_key, "sess_middle", "sess_tip")

    @pytest.mark.asyncio
    async def test_compression_parent_follows_real_sessiondb_lineage(self, tmp_path):
        from gateway.run import GatewayRunner
        from gateway.session import AsyncSessionStore
        from hermes_state import AsyncSessionDB, SessionDB

        session_db = SessionDB(db_path=tmp_path / "state.db")
        session_db.create_session("sess_parent", source="telegram")
        session_db.end_session("sess_parent", end_reason="compression")
        session_db.create_session(
            "sess_tip",
            source="telegram",
            parent_session_id="sess_parent",
        )

        current = self._entry("sess_parent")
        tip = self._entry("sess_tip")
        runner = object.__new__(GatewayRunner)
        runner._session_db = AsyncSessionDB(session_db)
        runner.session_store = MagicMock()
        runner.session_store.switch_session = MagicMock(return_value=tip)
        runner.session_store.advance_compression_session = MagicMock(return_value=tip)
        runner._async_session_store = AsyncSessionStore(runner.session_store)

        resolved = await runner._resolve_async_delegation_session(
            current, "sess_parent"
        )

        assert resolved is tip
        getattr(
            runner.session_store, "advance_compression_session"
        ).assert_called_once_with(current.session_key, "sess_parent", "sess_tip")


class TestResetHandlerInterruptsDelegations:
    def test_reset_command_calls_interrupt_for_session(self):
        """The /new handler must sever the old conversation's delegations."""
        import inspect
        from gateway import slash_commands

        src = inspect.getsource(slash_commands.GatewaySlashCommandsMixin._handle_reset_command)
        assert "interrupt_for_session" in src
        assert "session_reset" in src
