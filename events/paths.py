"""Canonical path resolver for Hermes notification/event infrastructure.

ALL notification and event-bus paths MUST use this module rather than
hermes_constants.get_hermes_home() directly.  get_hermes_home() returns
the profile-scoped directory when HERMES_HOME points at a profile,
but notification state is CROSS-PROFILE (all agents contribute, one user
consumes), so it must live at the canonical ~/.hermes root.
"""

import re
from pathlib import Path
from typing import Optional

from hermes_constants import get_default_hermes_root


def _root() -> Path:
    return get_default_hermes_root()


def hermes_repo_root() -> Path:
    """The ~/.hermes parent repo root — itself a git checkout whose WORKING
    TREE is production (cron script slots and Scheduled Tasks resolve
    absolute paths under it). Canonical root, never profile-scoped: the
    repo is ~/.hermes, not ~/.hermes/profiles/<name>.  Added 2026-07-28 for
    the CODE_DRIFT watched-repo list.
    """
    return _root()


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


def whatsapp_throttle_path() -> Path:
    """WhatsAppEscalator throttle-buffer persistence (2026-07-11).

    Mirrors notifier_batch_path(): buffered-but-unflushed escalations
    survive a gateway restart instead of being silently lost.
    """
    return notifications_home() / "whatsapp_throttle.json"


def rate_limit_state_path() -> Path:
    """Episode state for model rate limiting.

    Canonical root, never profile-scoped: a provider rate limit is global,
    so profile-scoped state would give every profile a private (and wrong)
    view of the same outage.
    """
    return notifications_home() / "rate_limit_state.json"


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


def code_drift_state_path(repo_name: Optional[str] = None) -> Path:
    """CodeDriftMonitor episode persistence, one file per watched repo.

    Holds {"alerting", "last_emit_wall", "last_shape"} so the falling-edge
    "resolved" event survives the common remediation path (FF the checkout,
    then restart the gateway). Wall-clock timestamps — same lesson as the
    notifier batch-age persistence. Cross-profile, so canonical root.

    agent-src keeps the original un-suffixed filename so the in-flight
    episode state of the only pre-2026-07-28 watched repo survives the
    multi-repo cutover; every other repo gets a slugged sibling.
    """
    if repo_name is None or repo_name == "agent-src":
        return notifications_home() / "code_drift_state.json"
    slug = re.sub(r"[^a-z0-9._-]+", "-", repo_name.lower()).strip("-.") or "repo"
    return notifications_home() / f"code_drift_state.{slug}.json"


def cron_trigger_log_path() -> Path:
    """Per-job rolling log of off-schedule cron fires (cron_triggered events).

    Maintained by the CronTriggerLog subscriber. JSONL format, append-only —
    never rotated (grows ~KB/week; the weekly-rotation promise was dead code
    from birth and was removed 2026-07-13). Operators grep this by job_id
    during postmortems instead of scanning audit.jsonl in full.
    """
    return events_dir() / "cron_triggers.jsonl"


def devflow_dir() -> Path:
    """Canonical home of the DevFlow Delegation Plane control plane (DDP).

    Cross-profile by construction (every agent delegates through ONE plane),
    so anchored at the canonical root like all other notification state.
    Added 2026-08-06 (spec: 2026-08-06-devflow-delegation-plane-design.md).
    """
    return _root() / "devflow"


def delegation_ledger_path() -> Path:
    """SQLite WAL lifecycle/dedup authority for delegated work requests."""
    return devflow_dir() / "delegation_ledger.db"


def devflow_allowlist_path() -> Path:
    """Operator-owned target allowlist (repos, path scopes, commands, ceilings)."""
    return devflow_dir() / "allowlist.json"


def devflow_policy_path() -> Path:
    """Optional per-source policy overrides (thresholds, rate limits, mode).

    Missing file = built-in defaults from devflow_delegation.policy.
    """
    return devflow_dir() / "policy.json"


def autonomy_sentinel_path() -> Path:
    """Global autonomy sentinel. Stage 3 gate — Stage 1 code must NEVER
    create this file; it exists here only so every stage resolves the same
    canonical path."""
    return devflow_dir() / ".autonomy_enabled"


def devflow_inbox_dir() -> Path:
    """Durable DEVFLOW_WORK_REQUEST envelope queue (atomic tmp+os.replace
    writes). Shares the existing devflow mailbox root used by the legacy
    DEVFLOW_FIX_REQUEST intake."""
    return mailbox_root() / "devflow" / "inbox"
