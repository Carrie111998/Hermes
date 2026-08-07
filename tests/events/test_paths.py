from pathlib import Path
from unittest.mock import patch

from events.paths import (
    notifications_home, events_db_path, audit_log_path,
    telegram_topics_path, telegram_verbosity_path,
    quiet_hours_path, quiet_queue_path,
    digest_state_path, notifier_batch_path, whatsapp_flush_state_path,
    mailbox_root, failure_cluster_state_path,
    devflow_dir, delegation_ledger_path, devflow_allowlist_path,
    devflow_policy_path, autonomy_sentinel_path, devflow_inbox_dir,
)
from hermes_constants import get_default_hermes_root


def test_all_paths_anchored_at_canonical_root(tmp_path):
    with patch("events.paths.get_default_hermes_root", return_value=tmp_path):
        assert notifications_home() == tmp_path / "notifications"
        assert events_db_path() == tmp_path / "events" / "event_bus.db"
        assert audit_log_path() == tmp_path / "events" / "audit.jsonl"
        assert telegram_topics_path() == tmp_path / "telegram" / "topics.json"
        assert telegram_verbosity_path() == tmp_path / "telegram" / "verbosity.json"
        assert quiet_hours_path() == tmp_path / "notifications" / "quiet_hours.json"
        assert quiet_queue_path() == tmp_path / "notifications" / "quiet_queue.json"
        assert digest_state_path() == tmp_path / "notifications" / "digest_state.json"
        assert notifier_batch_path() == tmp_path / "notifications" / "notifier_batch.json"
        assert whatsapp_flush_state_path() == tmp_path / "notifications" / "whatsapp_flush_state.json"
        assert mailbox_root() == tmp_path / "mailbox"
        assert failure_cluster_state_path() == tmp_path / "events" / "failure_cluster_state.json"
        assert devflow_dir() == tmp_path / "devflow"
        assert delegation_ledger_path() == tmp_path / "devflow" / "delegation_ledger.db"
        assert devflow_allowlist_path() == tmp_path / "devflow" / "allowlist.json"
        assert devflow_policy_path() == tmp_path / "devflow" / "policy.json"
        assert autonomy_sentinel_path() == tmp_path / "devflow" / ".autonomy_enabled"
        assert devflow_inbox_dir() == tmp_path / "mailbox" / "devflow" / "inbox"


def test_paths_ignore_profile_scoping(tmp_path, monkeypatch):
    root = tmp_path
    profile = tmp_path / "profiles" / "main"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    from events.paths import events_db_path
    assert "profiles" not in str(events_db_path())


class TestFailureClusterStatePath:
    """Locks down the canonical (cross-profile) path for FailureClusterDetector
    state.  Notification/event-bus state must NOT be profile-scoped."""

    def test_returns_canonical_root_relative_path(self):
        result = failure_cluster_state_path()
        expected = get_default_hermes_root() / "events" / "failure_cluster_state.json"
        assert result == expected
