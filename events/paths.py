"""Canonical path resolver for Hermes notification/event infrastructure.

ALL notification and event-bus paths MUST use this module rather than
hermes_constants.get_hermes_home() directly.  get_hermes_home() returns
the profile-scoped directory when HERMES_HOME points at a profile,
but notification state is CROSS-PROFILE (all agents contribute, one user
consumes), so it must live at the canonical ~/.hermes root.
"""

from pathlib import Path

from hermes_constants import get_default_hermes_root


def _root() -> Path:
    return get_default_hermes_root()


def events_dir() -> Path:
    return _root() / "events"


def notifications_home() -> Path:
    return _root() / "notifications"


def telegram_home() -> Path:
    return _root() / "telegram"


def events_db_path() -> Path:
    return events_dir() / "event_bus.db"


def audit_log_path() -> Path:
    return events_dir() / "audit.jsonl"


def telegram_topics_path() -> Path:
    return telegram_home() / "topics.json"


def telegram_verbosity_path() -> Path:
    return telegram_home() / "verbosity.json"


def quiet_hours_path() -> Path:
    return notifications_home() / "quiet_hours.json"


def quiet_queue_path() -> Path:
    return notifications_home() / "quiet_queue.json"


def digest_state_path() -> Path:
    return notifications_home() / "digest_state.json"


def notifier_batch_path() -> Path:
    return notifications_home() / "notifier_batch.json"


def whatsapp_flush_state_path() -> Path:
    return notifications_home() / "whatsapp_flush_state.json"


def cron_stale_thresholds_path() -> Path:
    """Optional per-job stale-threshold overrides (CronStaleMonitor).

    JSON shape: {"default_seconds": 1200, "per_job": {"jaum-skill-evolution": 3600}}
    Missing file = use built-in defaults (no overrides).
    """
    return notifications_home() / "cron_stale_thresholds.json"


def mailbox_root() -> Path:
    return _root() / "mailbox"


def gateway_heartbeat_path() -> Path:
    """Liveness signal file written by the gateway's subscriber poll loop.

    External watchers stat this file and alert on staleness (> a few minutes
    old means the gateway polling thread has stopped or the process died).
    """
    return _root() / "gateway.heartbeat"


def failure_cluster_state_path() -> Path:
    """Persistent state file for FailureClusterDetector.

    Holds per-source rolling windows of (timestamp, failure_type) tuples so
    the detector survives gateway/scheduler restarts and so cluster
    detection works across the gateway/cron-worker process boundary.
    Cross-profile (every agent's failures funnel here), so canonical root.
    """
    return events_dir() / "failure_cluster_state.json"


def cron_trigger_log_path() -> Path:
    """Per-job rolling log of off-schedule cron fires (cron_triggered events).

    Maintained by the CronTriggerLog subscriber. JSONL format, weekly
    rotation into events/audit/, 30-day retention. Operators grep this
    by job_id during postmortems instead of scanning audit.jsonl in full.
    """
    return events_dir() / "cron_triggers.jsonl"
