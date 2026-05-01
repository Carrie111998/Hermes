"""Tests for CriticSubscriber — wakes Critic on AGENT_FAILURE_CLUSTER."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority


@pytest.fixture
def bus(tmp_path):
    db_path = tmp_path / "events" / "event_bus.db"
    return EventBus(db_path=db_path)


def _seed_cursor_at_zero(bus: EventBus, subscriber_id: str = "critic-trigger") -> None:
    """Force the subscriber's cursor to 0 so it sees events emitted BEFORE its
    first poll. The bus's first-registration default (bus.py subscribe(),
    2026-04-28) jumps to head-of-bus to prevent backlog floods on real
    deploys; these tests construct CriticSubscriber, emit, then poll."""
    bus._execute(
        """INSERT INTO subscriber_cursors (subscriber_id, last_rowid, updated_at)
           VALUES (?, 0, datetime('now'))
           ON CONFLICT(subscriber_id) DO UPDATE SET last_rowid = 0""",
        (subscriber_id,),
    )


@pytest.fixture
def critic_script(tmp_path):
    """Mock critic_retro.py path."""
    script = tmp_path / "critic_retro.py"
    script.write_text("# stub")
    return script


class TestCriticSubscriberFiltering:
    def test_subscriber_id_is_critic_trigger(self, bus, critic_script):
        from events.subscribers.critic_trigger import CriticSubscriber
        sub = CriticSubscriber(bus, critic_script_path=critic_script)
        assert sub.subscriber_id == "critic-trigger"

    def test_event_types_filter_is_agent_failure_cluster_only(
        self, bus, critic_script,
    ):
        from events.subscribers.critic_trigger import CriticSubscriber
        sub = CriticSubscriber(bus, critic_script_path=critic_script)
        assert sub.event_types == [EventType.AGENT_FAILURE_CLUSTER]

    def test_ignores_other_event_types(self, bus, critic_script):
        from events.subscribers.critic_trigger import CriticSubscriber
        sub = CriticSubscriber(bus, critic_script_path=critic_script)
        _seed_cursor_at_zero(bus)
        bus.emit(EventType.CRON_FAILED, "scout", {"error": "x"})
        with patch("subprocess.Popen") as mock_popen:
            sub.poll()
        mock_popen.assert_not_called()


class TestCriticSubscriberInvocation:
    def test_emits_subprocess_with_cluster_args(self, bus, critic_script):
        from events.subscribers.critic_trigger import CriticSubscriber
        sub = CriticSubscriber(bus, critic_script_path=critic_script)
        _seed_cursor_at_zero(bus)
        bus.emit(
            event_type=EventType.AGENT_FAILURE_CLUSTER,
            source="scout",
            payload={
                "source": "scout",
                "failure_type": "captcha",
                "count": 3,
                "first_seen": "2026-04-26T10:00:00+00:00",
                "last_seen": "2026-04-26T10:10:00+00:00",
            },
            priority=Priority.HIGH,
        )
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            sub.poll()
        assert mock_popen.called
        cmd = mock_popen.call_args[0][0]
        # Command shape: [python, critic_retro.py, --cluster, "agent=scout,type=captcha"]
        assert str(critic_script) in cmd
        assert "--cluster" in cmd
        cluster_arg = cmd[cmd.index("--cluster") + 1]
        assert "agent=scout" in cluster_arg
        assert "type=captcha" in cluster_arg

    def test_subprocess_runs_detached_not_blocking(self, bus, critic_script):
        """Critic retro can take >5s; subscriber must not block the poll loop."""
        from events.subscribers.critic_trigger import CriticSubscriber
        sub = CriticSubscriber(bus, critic_script_path=critic_script)
        _seed_cursor_at_zero(bus)
        bus.emit(
            event_type=EventType.AGENT_FAILURE_CLUSTER,
            source="scout",
            payload={"source": "scout", "failure_type": "timeout",
                     "count": 3, "first_seen": "x", "last_seen": "y"},
        )
        with patch("subprocess.Popen") as mock_popen:
            sub.poll()
        # Must use Popen (non-blocking), not run/check_call (blocking)
        assert mock_popen.called

    def test_handles_subprocess_error_without_crashing(self, bus, critic_script):
        from events.subscribers.critic_trigger import CriticSubscriber
        sub = CriticSubscriber(bus, critic_script_path=critic_script)
        _seed_cursor_at_zero(bus)
        bus.emit(
            event_type=EventType.AGENT_FAILURE_CLUSTER,
            source="scout",
            payload={"source": "scout", "failure_type": "timeout",
                     "count": 3, "first_seen": "x", "last_seen": "y"},
        )
        with patch("subprocess.Popen", side_effect=OSError("boom")):
            # Must not raise — base subscriber would trip the breaker
            sub.poll()

    def test_missing_critic_script_logs_and_skips(self, bus, tmp_path):
        from events.subscribers.critic_trigger import CriticSubscriber
        missing = tmp_path / "does_not_exist.py"
        sub = CriticSubscriber(bus, critic_script_path=missing)
        _seed_cursor_at_zero(bus)
        bus.emit(
            event_type=EventType.AGENT_FAILURE_CLUSTER,
            source="scout",
            payload={"source": "scout", "failure_type": "timeout",
                     "count": 3, "first_seen": "x", "last_seen": "y"},
        )
        with patch("subprocess.Popen") as mock_popen:
            sub.poll()
        mock_popen.assert_not_called()


class TestCriticSubscriberDebounce:
    """Avoid storming Critic when many clusters arrive close together."""

    def test_debounces_repeat_same_cluster_within_window(
        self, bus, critic_script,
    ):
        from events.subscribers.critic_trigger import CriticSubscriber
        sub = CriticSubscriber(
            bus, critic_script_path=critic_script,
            debounce_seconds=300,
        )
        _seed_cursor_at_zero(bus)
        for _ in range(2):
            bus.emit(
                event_type=EventType.AGENT_FAILURE_CLUSTER,
                source="scout",
                payload={"source": "scout", "failure_type": "timeout",
                         "count": 3, "first_seen": "x", "last_seen": "y"},
            )
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            sub.poll()
        # Only the first cluster of the same (source, type) within the
        # debounce window invokes Critic.
        assert mock_popen.call_count == 1

    def test_does_not_debounce_different_clusters(self, bus, critic_script):
        from events.subscribers.critic_trigger import CriticSubscriber
        sub = CriticSubscriber(
            bus, critic_script_path=critic_script,
            debounce_seconds=300,
        )
        _seed_cursor_at_zero(bus)
        bus.emit(
            event_type=EventType.AGENT_FAILURE_CLUSTER,
            source="scout",
            payload={"source": "scout", "failure_type": "timeout",
                     "count": 3, "first_seen": "x", "last_seen": "y"},
        )
        bus.emit(
            event_type=EventType.AGENT_FAILURE_CLUSTER,
            source="matcher",
            payload={"source": "matcher", "failure_type": "captcha",
                     "count": 3, "first_seen": "x", "last_seen": "y"},
        )
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            sub.poll()
        assert mock_popen.call_count == 2
