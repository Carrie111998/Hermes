"""Fail-closed ownership + session-scoped delegation lifecycle (#55578).

Covers the hardening rules layered on top of the origin-routing salvage:

1. ``_session_owns_notification_event`` — positive-proof ownership. An
   async-delegation completion may only be injected into a session that
   PROVABLY commissioned it (origin UI id, or session-key/lineage match).
   Orphans are never adopted by a foreign chat.

2. ``interrupt_for_session`` — a session's in-flight async delegations end
   with the session. ``_finalize_session`` interrupts delegations owned by
   the closing session (by origin UI id always; by durable key only when the
   TUI owns the lifecycle).

3. Durable delivery is frozen-home scoped: restore, ACK, retry, and malformed
   event quarantine all target the event's trusted Hermes home.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

import tools.async_delegation as ad
import tui_gateway.server as tui_server
from tui_gateway.server import (
    _claim_notification_turn,
    _finalize_session,
    _restore_session_async_delegation_completions,
    _session_owns_notification_event,
)


@pytest.fixture(autouse=True)
def _reset_async_delegation():
    ad._reset_for_tests()
    tui_server._cancel_notification_retry_timers()
    from tools.process_registry import process_registry

    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    tui_server._cancel_notification_retry_timers()
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _persist_terminal_completion(
    home,
    delegation_id,
    *,
    session_key,
):
    home.mkdir(parents=True, exist_ok=True)
    record = {
        "delegation_id": delegation_id,
        "session_key": session_key,
        "origin_ui_session_id": "",
        "parent_session_id": session_key,
        "dispatched_at": 1.0,
    }
    event = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": session_key,
        "origin_ui_session_id": "",
        "parent_session_id": session_key,
        "status": "completed",
        "summary": "done",
        "completed_at": 2.0,
        "runtime_effect": None,
    }
    with ad._delivery_home_scope(home):
        ad._persist_dispatch(record)
        ad._persist_completion(
            event,
            {"status": "completed", "summary": "done"},
        )


def _durable_in_home(home, delegation_id):
    with ad._delivery_home_scope(home):
        return ad.get_durable_delegation(delegation_id)


class _FakeTimer:
    instances = []
    fail_start = False

    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False
        self.__class__.instances.append(self)

    def start(self):
        if self.__class__.fail_start:
            self.__class__.fail_start = False
            raise RuntimeError("timer start failed")
        self.started = True

    def is_alive(self):
        return self.started and not self.cancelled

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.started = False
        self.callback()


class TestSessionOwnsNotificationEvent:
    def _session(self, key="sess_key_1"):
        return {"session_key": key, "_finalized": False}

    def test_origin_ui_match_owns(self):
        evt = {"type": "async_delegation", "origin_ui_session_id": "tab1", "session_key": "other"}
        assert _session_owns_notification_event("tab1", self._session(), evt) is True

    def test_session_key_match_owns(self):
        evt = {"type": "async_delegation", "origin_ui_session_id": "", "session_key": "sess_key_1"}
        assert _session_owns_notification_event("tabX", self._session("sess_key_1"), evt) is True

    def test_orphan_is_not_owned(self):
        """No origin match, no key match, owner gone → NOT ours (fail closed)."""
        evt = {"type": "async_delegation", "origin_ui_session_id": "dead_tab", "session_key": "gone_key"}
        assert _session_owns_notification_event("tab1", self._session(), evt) is False

    def test_empty_key_and_origin_not_owned(self):
        """A delegation event with no return address at all is never adopted."""
        evt = {"type": "async_delegation", "origin_ui_session_id": "", "session_key": ""}
        assert _session_owns_notification_event("tab1", self._session(), evt) is False

    def test_finalized_session_owns_nothing(self):
        evt = {"type": "async_delegation", "origin_ui_session_id": "tab1", "session_key": "sess_key_1"}
        sess = self._session()
        sess["_finalized"] = True
        assert _session_owns_notification_event("tab1", sess, evt) is False

    def test_compression_chain_resolution_owns(self):
        evt = {"type": "async_delegation", "origin_ui_session_id": "", "session_key": "parent_key"}
        db = MagicMock()
        db.resolve_resume_session_id.return_value = "child_key"
        with patch("tui_gateway.server._get_db", return_value=db):
            assert _session_owns_notification_event("tabX", self._session("child_key"), evt) is True

class TestFrozenHomeCompletionDelivery:

    def test_forged_stamp_fails_closed_but_legacy_event_still_routes(
        self,
    ):
        session = {
            "session_key": "shared-key",
            "profile_home": None,
            "_finalized": False,
        }
        legacy_evt = {
            "type": "async_delegation",
            "session_key": "shared-key",
        }
        forged_evt = {
            **legacy_evt,
            ad._EVENT_DELIVERY_STORE_KEY: "forged-or-stale-token",
        }

        assert (
            _session_owns_notification_event(
                "default-tab",
                session,
                legacy_evt,
            )
            is True
        )
        assert (
            _session_owns_notification_event(
                "default-tab",
                session,
                forged_evt,
            )
            is False
        )

    def test_live_ack_updates_the_frozen_event_store(self, tmp_path):
        from tools.process_registry import process_registry

        home = tmp_path / "profile"
        delegation_id = "delegation-live-ack"
        _persist_terminal_completion(
            home,
            delegation_id,
            session_key="session",
        )

        restored = process_registry.restore_async_delegation_completions(
            hermes_home=home,
            event_filter=lambda evt: evt.get("session_key") == "session",
        )
        assert restored == 1
        evt = process_registry.completion_queue.get_nowait()

        claim, runtime_effect = _claim_notification_turn(
            evt,
            "tui-session",
        )
        assert claim
        assert runtime_effect is None
        assert ad.complete_event_delivery(evt, claim) is True

        assert (
            _durable_in_home(home, delegation_id)["delivery_state"]
            == "delivered"
        )

    def test_retry_carrier_waits_out_claim_then_requeues_once(
        self,
        tmp_path,
        monkeypatch,
    ):
        from tools.process_registry import process_registry

        home = tmp_path / "profile"
        delegation_id = "delegation-retry-carrier"
        _persist_terminal_completion(
            home,
            delegation_id,
            session_key="owner",
        )
        assert (
            process_registry.restore_async_delegation_completions(
                hermes_home=home,
            )
            == 1
        )
        evt = process_registry.completion_queue.get_nowait()
        claim = ad.claim_event_delivery(evt, "older-consumer")
        assert claim
        _FakeTimer.instances = []
        _FakeTimer.fail_start = False
        monkeypatch.setattr(tui_server.threading, "Timer", _FakeTimer)

        assert tui_server._schedule_async_notification_retry(evt)
        assert len(_FakeTimer.instances) == 1
        timer = _FakeTimer.instances[0]
        assert timer.daemon is True
        assert timer.delay > 299.0
        assert process_registry.completion_queue.empty()

        assert ad.release_event_delivery(evt, claim)
        assert tui_server._schedule_async_notification_retry(evt)
        assert len(_FakeTimer.instances) == 2
        replacement = _FakeTimer.instances[1]
        assert timer.cancelled is True
        assert replacement.delay == pytest.approx(
            tui_server._NOTIFICATION_RETRY_MIN_SECONDS
        )
        timer.fire()
        assert process_registry.completion_queue.empty()
        replacement.fire()
        assert process_registry.completion_queue.get_nowait() is evt
        assert tui_server._notification_retry_timers == {}

    def test_failed_replacement_timer_preserves_existing_carrier(
        self,
        tmp_path,
        monkeypatch,
    ):
        from tools.process_registry import process_registry

        home = tmp_path / "profile"
        delegation_id = "delegation-retry-start-failure"
        _persist_terminal_completion(
            home,
            delegation_id,
            session_key="owner",
        )
        process_registry.restore_async_delegation_completions(
            hermes_home=home,
        )
        evt = process_registry.completion_queue.get_nowait()
        claim = ad.claim_event_delivery(evt, "older-consumer")
        assert claim
        _FakeTimer.instances = []
        _FakeTimer.fail_start = False
        monkeypatch.setattr(tui_server.threading, "Timer", _FakeTimer)
        assert tui_server._schedule_async_notification_retry(evt)
        original = _FakeTimer.instances[0]

        assert ad.release_event_delivery(evt, claim)
        _FakeTimer.fail_start = True
        assert not tui_server._schedule_async_notification_retry(evt)
        assert original.cancelled is False
        assert (
            tui_server._notification_retry_timers[
                tui_server._notification_retry_identity(evt)
            ]
            is original
        )

        original.fire()
        assert process_registry.completion_queue.get_nowait() is evt
        assert tui_server._notification_retry_timers == {}

    def test_retry_carrier_does_not_requeue_terminal_row(
        self,
        tmp_path,
        monkeypatch,
    ):
        from tools.process_registry import process_registry

        home = tmp_path / "profile"
        delegation_id = "delegation-terminal-retry"
        _persist_terminal_completion(
            home,
            delegation_id,
            session_key="owner",
        )
        process_registry.restore_async_delegation_completions(
            hermes_home=home,
        )
        evt = process_registry.completion_queue.get_nowait()
        _FakeTimer.instances = []
        _FakeTimer.fail_start = False
        monkeypatch.setattr(tui_server.threading, "Timer", _FakeTimer)
        assert tui_server._schedule_async_notification_retry(evt)
        timer = _FakeTimer.instances[0]

        claim = ad.claim_event_delivery(evt, "terminal-consumer")
        assert claim
        assert ad.complete_event_delivery(evt, claim)
        timer.fire()

        assert process_registry.completion_queue.empty()
        assert tui_server._notification_retry_timers == {}

    @pytest.mark.parametrize(
        "failure_mode",
        (
            "fresh-foreign-claim",
            "claim-storage-error",
            "dispatch-exception",
            "release-storage-error",
        ),
    )
    def test_poller_retains_retry_carrier_after_failed_delivery_attempt(
        self,
        tmp_path,
        monkeypatch,
        failure_mode,
    ):
        import queue

        import tools.process_registry as process_registry_module
        from tools.process_registry import process_registry

        home = tmp_path / "profile"
        delegation_id = f"delegation-poller-{failure_mode}"
        _persist_terminal_completion(
            home,
            delegation_id,
            session_key="owner",
        )
        isolated_queue = queue.Queue()
        ad.restore_undelivered_completions(
            isolated_queue,
            hermes_home=home,
        )
        evt = isolated_queue.queue[0]
        if failure_mode == "fresh-foreign-claim":
            assert ad.claim_event_delivery(evt, "older-consumer")

        session = {
            "session_key": "owner",
            "resume_session_id": "owner",
            "profile_home": str(home),
            "_finalized": False,
            "history_lock": threading.Lock(),
            "running": False,
        }
        stop = threading.Event()
        scheduled = []

        def _schedule(candidate):
            scheduled.append(candidate)
            stop.set()
            return True

        monkeypatch.setattr(
            process_registry,
            "completion_queue",
            isolated_queue,
        )
        monkeypatch.setattr(
            process_registry_module,
            "format_process_notification",
            lambda _evt: "delegation completed",
        )
        monkeypatch.setattr(tui_server, "_emit", lambda *_a, **_k: None)
        monkeypatch.setattr(
            tui_server,
            "_schedule_async_notification_retry",
            _schedule,
        )
        if failure_mode == "claim-storage-error":
            monkeypatch.setattr(
                ad,
                "claim_event_delivery",
                MagicMock(side_effect=OSError("claim storage failed")),
            )
        if failure_mode in {
            "dispatch-exception",
            "release-storage-error",
        }:
            monkeypatch.setattr(
                tui_server,
                "_run_prompt_submit",
                MagicMock(side_effect=RuntimeError("dispatch failed")),
            )
        if failure_mode == "release-storage-error":
            monkeypatch.setattr(
                ad,
                "release_event_delivery",
                MagicMock(side_effect=OSError("release storage failed")),
            )

        with patch.dict(
            tui_server._sessions,
            {"owner-tab": session},
            clear=True,
        ):
            tui_server._notification_poller_loop(
                stop,
                "owner-tab",
                session,
            )

        assert scheduled == [evt]
        assert isolated_queue.empty()
        assert session["running"] is False
        durable = _durable_in_home(home, delegation_id)
        assert durable["delivery_state"] == "pending"

    def test_restart_resume_restores_from_frozen_home(self, tmp_path):
        from tools.process_registry import process_registry

        home = tmp_path / "profile"
        delegation_id = "delegation-restart"
        _persist_terminal_completion(
            home,
            delegation_id,
            session_key="session",
        )
        session = {
            "session_key": "session",
            "profile_home": None,
            "_finalized": False,
        }

        with patch.object(tui_server, "_hermes_home", str(home)):
            restored = _restore_session_async_delegation_completions(
                "tab-after-restart",
                session,
            )

        assert restored == 1
        evt = process_registry.completion_queue.get_nowait()
        assert evt["restored"] is True
        assert evt["session_key"] == "session"
        store = ad.get_event_delivery_store(evt)
        assert store is not None
        assert store.hermes_home == str(home.resolve())
        assert (
            _durable_in_home(home, delegation_id)["delivery_state"]
            == "pending"
        )

    def test_malformed_event_drop_updates_frozen_event_store(self, tmp_path):
        from tools.process_registry import process_registry

        home = tmp_path / "profile"
        delegation_id = "delegation-malformed"
        _persist_terminal_completion(
            home,
            delegation_id,
            session_key="session",
        )
        assert (
            process_registry.restore_async_delegation_completions(
                hermes_home=home,
                event_filter=lambda evt: evt.get("session_key") == "session",
            )
            == 1
        )
        evt = process_registry.completion_queue.get_nowait()
        evt["runtime_effect"] = {"schema": "forged"}

        claim, runtime_effect = _claim_notification_turn(
            evt,
            "tui-session",
        )

        assert claim is None
        assert runtime_effect is None
        assert (
            _durable_in_home(home, delegation_id)["delivery_state"]
            == "dropped"
        )

    def test_init_restores_frozen_home_before_starting_poller(
        self,
        tmp_path,
    ):
        home = tmp_path / "profile"
        db = MagicMock()
        db.get_session.return_value = {"cwd": str(tmp_path)}
        agent = MagicMock()
        agent.model = "test/model"
        order = []

        def _restore(sid, session):
            order.append(
                (
                    "restore",
                    sid,
                    session.get("profile_home"),
                )
            )
            return 0

        def _start_poller(sid, session):
            order.append(
                (
                    "poller",
                    sid,
                    session.get("profile_home"),
                )
            )
            return threading.Event()

        with (
            patch.dict(tui_server._sessions, {}, clear=True),
            patch(
                "tui_gateway.server._load_show_reasoning",
                return_value=False,
            ),
            patch(
                "tui_gateway.server._load_tool_progress_mode",
                return_value="all",
            ),
            patch(
                "tui_gateway.server._load_memory_notifications",
                return_value="on",
            ),
            patch(
                "tui_gateway.server._register_session_cwd",
            ),
            patch(
                "tui_gateway.server._wire_callbacks",
            ),
            patch(
                "tui_gateway.server._session_info",
                return_value={"model": "test/model"},
            ),
            patch(
                "tui_gateway.server._restore_session_async_delegation_completions",
                side_effect=_restore,
            ),
            patch(
                "tui_gateway.server._start_notification_poller",
                side_effect=_start_poller,
            ),
            patch(
                "tui_gateway.server._notify_session_boundary",
            ),
            patch(
                "tui_gateway.server._emit",
            ),
            patch(
                "tui_gateway.server._schedule_mcp_late_refresh",
            ),
        ):
            tui_server._init_session(
                "tab",
                "session",
                agent,
                [],
                cwd=str(tmp_path),
                session_db=db,
                profile_home=str(home),
            )

        assert order == [
            ("restore", "tab", str(home)),
            ("poller", "tab", str(home)),
        ]


class TestInterruptForSession:
    def _seed_record(self, delegation_id, session_key="", origin_ui_session_id="", status="running"):
        fn = MagicMock()
        with ad._records_lock:
            ad._records[delegation_id] = {
                "delegation_id": delegation_id,
                "status": status,
                "session_key": session_key,
                "origin_ui_session_id": origin_ui_session_id,
                "interrupt_fn": fn,
            }
        return fn

    def test_interrupts_only_matching_session(self):
        mine = self._seed_record("d1", session_key="sess_A")
        other = self._seed_record("d2", session_key="sess_B")
        n = ad.interrupt_for_session(session_key="sess_A")
        assert n == 1
        mine.assert_called_once()
        other.assert_not_called()

    def test_matches_by_origin_ui_session_id(self):
        mine = self._seed_record("d1", origin_ui_session_id="tab1")
        other = self._seed_record("d2", origin_ui_session_id="tab2")
        n = ad.interrupt_for_session(origin_ui_session_id="tab1")
        assert n == 1
        mine.assert_called_once()
        other.assert_not_called()

    def test_no_selector_is_noop(self):
        fn = self._seed_record("d1", session_key="sess_A")
        assert ad.interrupt_for_session() == 0
        fn.assert_not_called()

    def test_completed_records_untouched(self):
        fn = self._seed_record("d1", session_key="sess_A", status="completed")
        assert ad.interrupt_for_session(session_key="sess_A") == 0
        fn.assert_not_called()


class TestFinalizeInterruptsOwnDelegations:
    def _make_session(self, session_key="sess_A", sid="tab1"):
        agent = MagicMock()
        agent.session_id = session_key
        agent._session_messages = None
        agent.model = "m"
        agent.platform = "tui"
        return {
            "agent": agent,
            "history": [{"role": "user", "content": "x"}],
            "history_lock": threading.Lock(),
            "session_key": session_key,
            "_finalized": False,
            "_sid": sid,
        }

    @patch("tui_gateway.server._get_db")
    def test_finalize_interrupts_sessions_delegations(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.get_session.return_value = {"source": "tui"}
        mock_get_db.return_value = mock_db

        with patch("tools.async_delegation.interrupt_for_session") as mock_int:
            _finalize_session(self._make_session(), end_reason="tui_close")

        mock_int.assert_called_once()
        kwargs = mock_int.call_args.kwargs
        assert kwargs["session_key"] == "sess_A"
        assert kwargs["origin_ui_session_id"] == "tab1"

    @patch("tui_gateway.server._get_db")
    def test_viewer_of_gateway_session_only_interrupts_by_origin(self, mock_get_db):
        """Closing a TUI viewer tab on a live gateway session must not kill
        the gateway's own background work — key-based interrupt is skipped,
        origin-id interrupt (this tab's own dispatches) still applies."""
        mock_db = MagicMock()
        mock_db.get_session.return_value = {"source": "telegram"}
        mock_get_db.return_value = mock_db

        with patch("tools.async_delegation.interrupt_for_session") as mock_int:
            _finalize_session(
                self._make_session(session_key="agent:main:telegram:dm:123", sid="tab9"),
                end_reason="ws_orphan_reap",
            )

        kwargs = mock_int.call_args.kwargs
        assert kwargs["session_key"] == ""
        assert kwargs["origin_ui_session_id"] == "tab9"
