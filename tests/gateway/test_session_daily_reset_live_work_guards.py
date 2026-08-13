"""Daily session reset must not cut live work.

The daily boundary fires on wall-clock time rather than on user activity, so
unlike the idle boundary it can land on top of a session that is mid-turn or
whose profile is actively working a kanban board. ``updated_at`` does not move
while a turn runs, so the staleness check alone cannot see either condition.

``SessionStore._is_session_expired`` therefore consults two optional callables
before letting the *daily* branch expire a session — and both fail closed, so a
probe that raises keeps the conversation alive rather than resetting it on a
guess. The idle branch is deliberately unguarded: idle expiry already implies
``idle_minutes`` of no activity.
"""

from datetime import datetime, timedelta

from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import SessionEntry, SessionStore


def _make_store(
    tmp_path,
    policy=None,
    is_session_running_fn=None,
    has_active_kanban_fn=None,
):
    config = GatewayConfig()
    if policy:
        config.default_reset_policy = policy
    return SessionStore(
        sessions_dir=tmp_path,
        config=config,
        is_session_running_fn=is_session_running_fn,
        has_active_kanban_fn=has_active_kanban_fn,
    )


def _stale_entry(days: int = 3) -> SessionEntry:
    """An entry whose updated_at is unambiguously before today's reset hour."""
    now = datetime.now()
    return SessionEntry(
        session_key="agent:main:telegram:dm:123",
        session_id="s1",
        created_at=now - timedelta(days=days + 1),
        updated_at=now - timedelta(days=days),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )


def _raise(_session_key):
    raise RuntimeError("probe unavailable")


DAILY = SessionResetPolicy(mode="daily", at_hour=4)


# ---------------------------------------------------------------------------
# Base case — the guards must not break ordinary daily expiry
# ---------------------------------------------------------------------------

class TestDailyExpiryStillWorks:

    def test_stale_session_expires_with_no_guards_wired(self, tmp_path):
        store = _make_store(tmp_path, DAILY)
        assert store._is_session_expired(_stale_entry()) is True

    def test_stale_session_expires_when_both_guards_say_idle(self, tmp_path):
        store = _make_store(
            tmp_path, DAILY,
            is_session_running_fn=lambda key: False,
            has_active_kanban_fn=lambda key: False,
        )
        assert store._is_session_expired(_stale_entry()) is True

    def test_fresh_session_does_not_expire(self, tmp_path):
        store = _make_store(
            tmp_path, DAILY,
            is_session_running_fn=lambda key: False,
            has_active_kanban_fn=lambda key: False,
        )
        entry = _stale_entry()
        entry.updated_at = datetime.now()
        assert store._is_session_expired(entry) is False


# ---------------------------------------------------------------------------
# Guard: a turn is in flight
# ---------------------------------------------------------------------------

class TestRunningTurnGuard:

    def test_running_session_not_expired(self, tmp_path):
        store = _make_store(
            tmp_path, DAILY,
            is_session_running_fn=lambda key: True,
        )
        assert store._is_session_expired(_stale_entry()) is False

    def test_guard_receives_the_session_key(self, tmp_path):
        seen = []

        store = _make_store(
            tmp_path, DAILY,
            is_session_running_fn=lambda key: seen.append(key) or False,
        )
        store._is_session_expired(_stale_entry())
        assert seen == ["agent:main:telegram:dm:123"]

    def test_probe_error_fails_closed(self, tmp_path):
        store = _make_store(tmp_path, DAILY, is_session_running_fn=_raise)
        assert store._is_session_expired(_stale_entry()) is False

    def test_both_mode_also_guarded(self, tmp_path):
        """``both`` reaches the daily branch once idle_minutes is generous."""
        store = _make_store(
            tmp_path,
            SessionResetPolicy(mode="both", at_hour=4, idle_minutes=60 * 24 * 30),
            is_session_running_fn=lambda key: True,
        )
        assert store._is_session_expired(_stale_entry()) is False


# ---------------------------------------------------------------------------
# Guard: live kanban work
# ---------------------------------------------------------------------------

class TestKanbanGuard:

    def test_active_kanban_not_expired(self, tmp_path):
        store = _make_store(
            tmp_path, DAILY,
            has_active_kanban_fn=lambda key: True,
        )
        assert store._is_session_expired(_stale_entry()) is False

    def test_probe_error_fails_closed(self, tmp_path):
        store = _make_store(tmp_path, DAILY, has_active_kanban_fn=_raise)
        assert store._is_session_expired(_stale_entry()) is False

    def test_not_consulted_when_a_turn_is_already_running(self, tmp_path):
        """The running-turn guard short-circuits — no need for the DB hit."""
        calls = []

        store = _make_store(
            tmp_path, DAILY,
            is_session_running_fn=lambda key: True,
            has_active_kanban_fn=lambda key: calls.append(key) or True,
        )
        assert store._is_session_expired(_stale_entry()) is False
        assert calls == []

    def test_both_mode_also_guarded(self, tmp_path):
        store = _make_store(
            tmp_path,
            SessionResetPolicy(mode="both", at_hour=4, idle_minutes=60 * 24 * 30),
            has_active_kanban_fn=lambda key: True,
        )
        assert store._is_session_expired(_stale_entry()) is False


# ---------------------------------------------------------------------------
# The idle branch is intentionally NOT guarded
# ---------------------------------------------------------------------------

class TestIdleBranchUnguarded:

    def test_idle_expiry_ignores_the_daily_guards(self, tmp_path):
        store = _make_store(
            tmp_path,
            SessionResetPolicy(mode="idle", idle_minutes=30),
            is_session_running_fn=lambda key: True,
            has_active_kanban_fn=lambda key: True,
        )
        entry = _stale_entry()
        entry.updated_at = datetime.now() - timedelta(hours=1)
        assert store._is_session_expired(entry) is True

    def test_both_mode_idle_trigger_ignores_the_daily_guards(self, tmp_path):
        store = _make_store(
            tmp_path,
            SessionResetPolicy(mode="both", at_hour=4, idle_minutes=30),
            is_session_running_fn=lambda key: True,
            has_active_kanban_fn=lambda key: True,
        )
        entry = _stale_entry()
        entry.updated_at = datetime.now() - timedelta(hours=1)
        assert store._is_session_expired(entry) is True

    def test_mode_none_never_expires(self, tmp_path):
        store = _make_store(tmp_path, SessionResetPolicy(mode="none"))
        assert store._is_session_expired(_stale_entry()) is False
