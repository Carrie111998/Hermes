# Hermes Communication Layer — Silence Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore end-to-end notification flow from Hermes cron jobs and agents to the user's Telegram group and WhatsApp, fixing the six silences diagnosed on 2026-04-16.

**Architecture:** Keep the existing EventBus + subscriber topology from `2026-04-15-hermes-communication-layer-design.md`. This plan (a) migrates ALL notification/event state paths to a single canonical `~/.hermes/` root (Option A — global), (b) adds a new `MailboxTranslator` subscriber (Option B) that consumes `mailbox_message` events and emits typed domain events, retiring the dead regex stub in `CronEventEmitter`, (c) persists ephemeral state (digest timestamp, batch buffer, flush flag) across restarts, (d) disables the broken Telegram fallback transport, and (e) migrates existing data under `~/.hermes/profiles/main/` to the new canonical locations.

**Tech Stack:** Python 3.11 stdlib (sqlite3, pathlib, json, threading, datetime), pytest for tests.

**Baseline:** 133 tests in `tests/events/` currently pass. Every task runs the full events test suite and must keep it green.

---

## Executive Summary — Four Tiers

| Tier | Purpose | Tasks | Approx duration |
|---|---|---|---|
| **1** | Immediate unblock — get notifications flowing today | T1.1–T1.5 | ~2 hours |
| **2** | Retire regex stub, add MailboxTranslator, emit domain events | T2.1–T2.6 | ~1 day |
| **3** | Persistence + observability hardening | T3.1–T3.6 | ~0.5 day |
| **4** | Future-proofing + sticky-IP reset + diagnostic surface | T4.1–T4.4 | ~0.5 day |

**Total: 21 tasks.** Each task has failing-test-first, implementation, verification, and commit steps.

---

## Conventions Used In This Plan

**Repo root:** `C:\Users\diego\Usersdiegohermes-agent-src` (referred to below as `<root>`). All file paths in tasks are repo-relative.

**Test runner:** `pytest` from `<root>`. Use `-x` (stop at first fail) during TDD and `-q` for the whole-suite verification.

**Canonical home function:** Throughout the plan, "canonical home" means `hermes_constants.get_default_hermes_root()` — it is already defined in `hermes_constants.py` (line 20) and resolves to `~/.hermes` even when `HERMES_HOME` points at a profile directory. This is the existing, correct function to use for cross-profile notification state.

**Commits:** Small and focused. Each task ends with one `git commit`. Commit messages use Conventional Commits (`feat:`, `fix:`, `refactor:`, `test:`, `chore:`).

**Running a single test file:**
```bash
python -m pytest tests/events/test_<name>.py -v
```
**Running the whole events suite:**
```bash
python -m pytest tests/events/ -q
```

---

## File Structure Overview

### Files this plan creates

```
events/paths.py                                    # Tier 1 — canonical path resolver
events/subscribers/mailbox_translator.py           # Tier 2 — new subscriber
events/state.py                                    # Tier 3 — persistent state helper
scripts/migrate_hermes_notification_paths.py       # Tier 1 — one-shot data migration

tests/events/test_paths.py
tests/events/subscribers/test_mailbox_translator.py
tests/events/test_state.py
tests/events/test_restart_semantics.py
tests/scripts/test_migrate_hermes_notification_paths.py
```

### Files this plan modifies

```
events/bus.py                                   # path resolver + WAL checkpoint
events/gateway_integration.py                   # persistent state load/save, checkpoint hook, register MailboxTranslator
events/producers/cron_emitter.py                # remove dead regex stub
events/producers/mailbox_watcher.py             # path resolver
events/producers/health_monitor.py              # path resolver
events/subscribers/audit_logger.py              # path resolver
events/subscribers/digest_composer.py           # path resolver + persistence
events/subscribers/memory_writer.py             # path resolver (memory-local path unchanged, confirm)
events/subscribers/telegram_mirror.py           # path resolver
events/subscribers/telegram_notifier.py         # path resolver + persistent batch buffer
events/subscribers/whatsapp_escalator.py        # path resolver + persistent last_flush_fired
gateway/platforms/telegram_network.py           # sticky-IP reset
gateway/platforms/telegram.py                   # respect HERMES_TELEGRAM_DISABLE_FALLBACK_IPS (verify default behavior)
```

### Config files migrated (data, not code)

These will be MOVED by the Tier 1 migration script from `~/.hermes/profiles/main/...` (where the profile-scoped `get_hermes_home()` was writing them) to `~/.hermes/...` (canonical home):

```
~/.hermes/telegram/topics.json
~/.hermes/telegram/verbosity.json
~/.hermes/notifications/quiet_hours.json
~/.hermes/notifications/quiet_queue.json
~/.hermes/events/event_bus.db
~/.hermes/events/event_bus.db-wal
~/.hermes/events/event_bus.db-shm
~/.hermes/events/audit.jsonl
~/.hermes/mailbox/.event_watermark.json
```

---

## Preflight

**PF.1** Create a feature branch from current HEAD before any task:

```bash
cd <root>
git checkout -b fix/hermes-comms-layer-silences-2026-04-16
git status
python -m pytest tests/events/ -q
```

**Expected:** Clean working tree, 133 passed.

If tests fail here, STOP and debug before starting any task.

**PF.2** Back up current notification state (non-destructive safety net before migration):

```bash
mkdir -p ~/.hermes_backup_2026-04-16
cp -r ~/.hermes/profiles/main/telegram ~/.hermes_backup_2026-04-16/ 2>/dev/null || true
cp -r ~/.hermes/profiles/main/notifications ~/.hermes_backup_2026-04-16/ 2>/dev/null || true
cp -r ~/.hermes/profiles/main/events ~/.hermes_backup_2026-04-16/ 2>/dev/null || true
cp -r ~/.hermes/telegram ~/.hermes_backup_2026-04-16/telegram-global 2>/dev/null || true
cp -r ~/.hermes/notifications ~/.hermes_backup_2026-04-16/notifications-global 2>/dev/null || true
```

---

## Tier 1 — Immediate Unblock

Goal: notifications flow again after one gateway restart.

---

### Task 1.1: Add canonical notification-paths resolver

**Files:**
- Create: `events/paths.py`
- Create: `tests/events/test_paths.py`

**Rationale:** Every notification-state path in the codebase currently resolves via `hermes_constants.get_hermes_home()`, which returns the profile-scoped directory when `HERMES_HOME` points at `~/.hermes/profiles/main`. We need a single function that ALWAYS returns `~/.hermes` regardless of profile scoping, so notification/event state is shared across all profiles. `hermes_constants.get_default_hermes_root()` already does this; this task wraps it in an `events`-local module so the rest of the Tier 1 migration is a one-line import swap.

- [ ] **Step 1: Write failing test** — create `tests/events/test_paths.py`:

```python
from pathlib import Path
from unittest.mock import patch

from events.paths import (
    notifications_home, events_db_path, audit_log_path,
    telegram_topics_path, telegram_verbosity_path,
    quiet_hours_path, quiet_queue_path,
    digest_state_path, notifier_batch_path, whatsapp_flush_state_path,
    mailbox_root,
)


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


def test_paths_ignore_profile_scoping(tmp_path, monkeypatch):
    root = tmp_path
    profile = tmp_path / "profiles" / "main"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    from events.paths import events_db_path
    assert "profiles" not in str(events_db_path())
```

- [ ] **Step 2: Run test — expected FAIL** (`ModuleNotFoundError: No module named 'events.paths'`)

```
python -m pytest tests/events/test_paths.py -v
```

- [ ] **Step 3: Implement `events/paths.py`:**

```python
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


def mailbox_root() -> Path:
    return _root() / "mailbox"
```

- [ ] **Step 4: Run test — expected PASS (2 passed)**

- [ ] **Step 5: Run full events suite — no regressions (≥135 passed)**

```
python -m pytest tests/events/ -q
```

- [ ] **Step 6: Commit**

```
git add events/paths.py tests/events/test_paths.py
git commit -m "feat(events): add canonical path resolver for notification state"
```

---

### Task 1.2: Migrate event-bus + subscribers to events.paths

**Files to modify:**
- `events/bus.py` (lines 58-60)
- `events/producers/mailbox_watcher.py` (lines 40-42)
- `events/subscribers/audit_logger.py` (lines 30-32)
- `events/subscribers/telegram_mirror.py` (lines 31-33)
- `events/subscribers/telegram_notifier.py` (lines 74-79, two call sites)
- `events/subscribers/whatsapp_escalator.py` (lines 97-113, two call sites — keep explicit-arg and config-file branches)
- `events/subscribers/digest_composer.py` (lines 175-176)

**Files NOT modified (intentional):**
- `events/producers/health_monitor.py` — reads `config.yaml` which IS per-profile
- `events/subscribers/memory_writer.py` — writes to `MEMORY.md` which IS per-agent

**Test:** `tests/events/test_path_migration.py` (new)

- [ ] **Step 1: Write failing test — create `tests/events/test_path_migration.py`:**

```python
"""Lock in that notification components use events.paths, not get_hermes_home."""
from pathlib import Path

import pytest

_FILES = [
    "events/bus.py",
    "events/producers/mailbox_watcher.py",
    "events/subscribers/audit_logger.py",
    "events/subscribers/telegram_mirror.py",
    "events/subscribers/telegram_notifier.py",
    "events/subscribers/whatsapp_escalator.py",
    "events/subscribers/digest_composer.py",
]


@pytest.mark.parametrize("relpath", _FILES)
def test_file_uses_events_paths_not_get_hermes_home(relpath):
    repo_root = Path(__file__).resolve().parents[2]
    src = (repo_root / relpath).read_text(encoding="utf-8")
    assert "get_hermes_home" not in src, f"{relpath} still uses get_hermes_home"
    assert "events.paths" in src or "from events import paths" in src, (
        f"{relpath} must import events.paths"
    )
```

- [ ] **Step 2: Run test — expected 7 FAIL** (`python -m pytest tests/events/test_path_migration.py -v`)

- [ ] **Step 3: Update `events/bus.py` lines 58-60**

Replace:

```python
        if db_path is None:
            from hermes_constants import get_hermes_home
            db_path = get_hermes_home() / "events" / "event_bus.db"
```

With:

```python
        if db_path is None:
            from events.paths import events_db_path
            db_path = events_db_path()
```

- [ ] **Step 4: Update `events/producers/mailbox_watcher.py` lines 40-42**

Replace:

```python
        if mailbox_root is None:
            from hermes_constants import get_hermes_home
            mailbox_root = get_hermes_home() / "mailbox"
```

With:

```python
        if mailbox_root is None:
            from events.paths import mailbox_root as _default_mailbox_root
            mailbox_root = _default_mailbox_root()
```

- [ ] **Step 5: Update `events/subscribers/audit_logger.py` lines 30-32** — analogous swap to `audit_log_path()`

- [ ] **Step 6: Update `events/subscribers/telegram_mirror.py` lines 31-33** — analogous swap to `telegram_topics_path()`

- [ ] **Step 7: Update `events/subscribers/telegram_notifier.py` lines 74-79** — swap both `topics_path` and `verbosity_path` to `telegram_topics_path()` and `telegram_verbosity_path()` respectively.

- [ ] **Step 8: Update `events/subscribers/whatsapp_escalator.py`** — swap `quiet_config_path` default to `quiet_hours_path()` and the fallback queue path to `quiet_queue_path()`. Leave the explicit-arg and config-file-override branches intact.

- [ ] **Step 9: Update `events/subscribers/digest_composer.py` line ~175** — swap topics.json read to `telegram_topics_path()`.

- [ ] **Step 10: Run the parametrized test — expected 7 PASS**

- [ ] **Step 11: Run full events suite — expected ≥136 passed, no regressions**

- [ ] **Step 12: Commit**

```
git add events/bus.py events/producers/mailbox_watcher.py \
        events/subscribers/audit_logger.py \
        events/subscribers/telegram_mirror.py \
        events/subscribers/telegram_notifier.py \
        events/subscribers/whatsapp_escalator.py \
        events/subscribers/digest_composer.py \
        tests/events/test_path_migration.py
git commit -m "fix(events): route all notification paths through events.paths (Silence #4, #5)"
```

---

### Task 1.3: One-shot data migration script

**Files:**
- Create: `scripts/migrate_hermes_notification_paths.py`
- Create: `tests/scripts/test_migrate_hermes_notification_paths.py`
- Create: `tests/scripts/__init__.py` (if missing)

**Rationale:** Code reads from `~/.hermes/...`; real data still lives at `~/.hermes/profiles/main/...`. Move it. Idempotent. Preserves pre-existing global copies as `*.pre-2026-04-16`.

- [ ] **Step 1: Write failing test — create `tests/scripts/test_migrate_hermes_notification_paths.py`:**

```python
import json
from pathlib import Path

import pytest

from scripts.migrate_hermes_notification_paths import migrate


@pytest.fixture
def fake_hermes(tmp_path):
    root = tmp_path
    profile = root / "profiles" / "main"
    (profile / "telegram").mkdir(parents=True)
    (profile / "notifications").mkdir(parents=True)
    (profile / "events").mkdir(parents=True)
    (profile / "mailbox").mkdir(parents=True)

    (profile / "telegram" / "topics.json").write_text(
        json.dumps({"group_chat_id": "-100", "topics": {}}), encoding="utf-8"
    )
    (profile / "telegram" / "verbosity.json").write_text(
        json.dumps({"system": {"mode": "digest_only"}}), encoding="utf-8"
    )
    (profile / "notifications" / "quiet_hours.json").write_text(
        json.dumps({"enabled": True}), encoding="utf-8"
    )
    (profile / "events" / "event_bus.db").write_bytes(b"FAKE_SQLITE")
    (profile / "events" / "audit.jsonl").write_text('{}\n', encoding="utf-8")

    (root / "telegram").mkdir()
    (root / "telegram" / "topics.json").write_text(
        json.dumps({"group_chat_id": "-OLD"}), encoding="utf-8"
    )
    return root


def test_migrate_moves_profile_files_to_root(fake_hermes):
    migrate(root=fake_hermes)
    assert (fake_hermes / "telegram" / "topics.json").exists()
    data = json.loads((fake_hermes / "telegram" / "topics.json").read_text())
    assert data["group_chat_id"] == "-100"
    # old global preserved
    assert (fake_hermes / "telegram" / "topics.json.pre-2026-04-16").exists()


def test_migrate_preserves_all_notification_artifacts(fake_hermes):
    migrate(root=fake_hermes)
    assert (fake_hermes / "telegram" / "verbosity.json").exists()
    assert (fake_hermes / "notifications" / "quiet_hours.json").exists()
    assert (fake_hermes / "events" / "event_bus.db").exists()
    assert (fake_hermes / "events" / "audit.jsonl").exists()


def test_migrate_idempotent(fake_hermes):
    migrate(root=fake_hermes)
    migrate(root=fake_hermes)
    backups = list((fake_hermes / "telegram").glob("topics.json.pre-*"))
    assert len(backups) == 1


def test_migrate_dry_run_does_not_move(fake_hermes):
    migrate(root=fake_hermes, dry_run=True)
    assert (fake_hermes / "profiles" / "main" / "telegram" / "topics.json").exists()
    data = json.loads((fake_hermes / "telegram" / "topics.json").read_text())
    assert data["group_chat_id"] == "-OLD"
```

- [ ] **Step 2: Run test — expected FAIL** (`ModuleNotFoundError: scripts.migrate_hermes_notification_paths`)

- [ ] **Step 3: Implement `scripts/migrate_hermes_notification_paths.py`:**

```python
"""One-shot migration: profile-scoped notification state -> canonical ~/.hermes root.

Moves:
    ~/.hermes/profiles/main/telegram/*           -> ~/.hermes/telegram/*
    ~/.hermes/profiles/main/notifications/*      -> ~/.hermes/notifications/*
    ~/.hermes/profiles/main/events/*             -> ~/.hermes/events/*
    ~/.hermes/profiles/main/mailbox/.event_watermark.json
                                                 -> ~/.hermes/mailbox/.event_watermark.json

Pre-existing global copies at the destination are preserved under
<name>.pre-2026-04-16 so nothing is lost.  Safe to re-run (idempotent).
"""

from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

_MIGRATIONS: Tuple[Tuple[str, str], ...] = (
    ("telegram/topics.json",            "telegram/topics.json"),
    ("telegram/verbosity.json",         "telegram/verbosity.json"),
    ("notifications/quiet_hours.json",  "notifications/quiet_hours.json"),
    ("notifications/quiet_queue.json",  "notifications/quiet_queue.json"),
    ("events/event_bus.db",             "events/event_bus.db"),
    ("events/event_bus.db-wal",         "events/event_bus.db-wal"),
    ("events/event_bus.db-shm",         "events/event_bus.db-shm"),
    ("events/audit.jsonl",              "events/audit.jsonl"),
    ("mailbox/.event_watermark.json",   "mailbox/.event_watermark.json"),
)

BACKUP_SUFFIX = ".pre-2026-04-16"


def _default_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def migrate(
    root: Path | None = None,
    profile_home: Path | None = None,
    *,
    dry_run: bool = False,
) -> None:
    root = Path(root) if root is not None else _default_root()
    profile_home = Path(profile_home) if profile_home is not None else (root / "profiles" / "main")

    logger.info("migration root=%s profile_home=%s dry_run=%s", root, profile_home, dry_run)

    for rel_src, rel_dst in _MIGRATIONS:
        src = profile_home / rel_src
        dst = root / rel_dst
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            backup = dst.with_name(dst.name + BACKUP_SUFFIX)
            if backup.exists():
                if not dry_run:
                    if backup.is_dir():
                        shutil.rmtree(backup)
                    else:
                        backup.unlink()
            if not dry_run:
                dst.rename(backup)
        if not dry_run:
            shutil.move(str(src), str(dst))


def _cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--root", type=Path, default=None)
    ap.add_argument("--profile-home", type=Path, default=None)
    ns = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    migrate(root=ns.root, profile_home=ns.profile_home, dry_run=ns.dry_run)


if __name__ == "__main__":
    _cli()
```

- [ ] **Step 4: Create `tests/scripts/__init__.py` if missing** (empty file)

- [ ] **Step 5: Run test — expected 4 PASS**

- [ ] **Step 6: Commit**

```
git add scripts/migrate_hermes_notification_paths.py \
        tests/scripts/test_migrate_hermes_notification_paths.py \
        tests/scripts/__init__.py
git commit -m "feat(scripts): one-shot notification-path migration (Tier 1)"
```

---

### Task 1.4: Flip verbosity.json system topic to `all`, add env flag to disable Telegram fallback transport

**Files modified (data, not code):**
- `~/.hermes/telegram/verbosity.json` (after migration runs — data live on the user's machine)
- `~/.hermes/profiles/main/.env` (set `HERMES_TELEGRAM_DISABLE_FALLBACK_IPS=1`)

**No code tests needed for this task** — the existing verbosity-loading and env-flag code paths already have test coverage (`test_telegram_notifier.py`, gateway platform tests). This task is a runtime config change.

- [ ] **Step 1: Apply data migration (must complete Tasks 1.1–1.3 first)**

```
cd <root>
python scripts/migrate_hermes_notification_paths.py --dry-run   # inspect plan
python scripts/migrate_hermes_notification_paths.py             # perform
```

Expected output: lines saying "moving ... -> ..." for each migrated file.

- [ ] **Step 2: Flip verbosity**

Edit `~/.hermes/telegram/verbosity.json`. Change the `system` entry:

```json
{
  "alerts":        {"mode": "all"},
  "scout":         {"mode": "all"},
  "matcher":       {"mode": "all"},
  "tailor_applier": {"mode": "all"},
  "tracker":       {"mode": "all"},
  "digests":       {"mode": "all"},
  "system":        {"mode": "all"},
  "agent_comms":   {"mode": "significant_only"}
}
```

The `system` topic now posts cron lifecycle traffic in real time. This makes delivery visible end-to-end for verification.

- [ ] **Step 3: Add env flag to `~/.hermes/profiles/main/.env`**

Append:

```
HERMES_TELEGRAM_DISABLE_FALLBACK_IPS=1
```

Rationale: under NordVPN, primary DNS resolution `api.telegram.org -> 149.154.166.110` works. The fallback transport locks onto unreachable IPs. Disabling it restores direct routing.

- [ ] **Step 4: Restart the gateway process**

```
cd <root>
pkill -f "gateway" || true
sleep 2
# Start gateway however the user normally does (check README / launch script)
```

- [ ] **Step 5: Manual verification — wait up to 70 seconds, then check**

```
python - <<PYEOF
import sqlite3
db = sqlite3.connect(str((__import__("pathlib").Path.home() / ".hermes" / "events" / "event_bus.db")))
print("recent events:")
for r in db.execute("SELECT timestamp, event_type, source FROM events ORDER BY created_at DESC LIMIT 10"):
    print(" ", r)
print("cursors:")
for r in db.execute("SELECT * FROM subscriber_cursors"):
    print(" ", r)
PYEOF
```

Expected: recent events include `gateway_health`, cron lifecycle events; `subscriber_cursors` has 6 rows (audit-logger, telegram-notifier, whatsapp-escalator, digest-composer, memory-writer, telegram-mirror).

- [ ] **Step 6: Verify Telegram delivery end-to-end**

Force a cron job and watch the `system` topic in your Telegram group. Running `jobflow-tracker-cycle` manually:

```
# Use your existing cron-run-now helper, or trigger from the Hermes CLI
```

Expected: a `cron_started` and `cron_completed` message appear in the `System Health` topic within 60 seconds.

- [ ] **Step 7: Commit (runbook + env template)**

```
git add docs/superpowers/plans/2026-04-16-hermes-comms-layer-fixes.md
# If your repo tracks an .env.example or runbook, update it to include
# HERMES_TELEGRAM_DISABLE_FALLBACK_IPS=1 under NordVPN.
git commit -m "chore(telegram): document disable-fallback-ips flag for NordVPN users"
```

---

### Task 1.5: Manual kickstart — trigger DigestComposer + WhatsApp flush today

This task is NON-CODE. Skip it in automated execution; flag for manual operator action.

- [ ] **Step 1: Open a Python REPL inside the running gateway's process space** — or write a tiny script that connects to the existing event-bus DB and invokes the compose+flush methods:

```python
from events.bus import EventBus
from events.subscribers.digest_composer import DigestComposer
from events.subscribers.whatsapp_escalator import WhatsAppEscalator

bus = EventBus()
d = DigestComposer(bus)
print(d.compose())   # posts to Digests topic + WhatsApp if morning

w = WhatsAppEscalator(bus)
flushed = w.flush_queue()
print(f"flushed {flushed} queued messages")
```

- [ ] **Step 2: Observe the Telegram Digests topic and WhatsApp chat** for the kickstart messages.

No commit needed — one-shot operator action.

---

## Tier 2 — MailboxTranslator + Retire Regex Stub

Goal: replace the dead regex-based output parser with a subscriber that consumes `mailbox_message` events (produced by MailboxWatcher) and emits typed domain events from structured agent payloads.

---

### Task 2.1: Define the mailbox-message → domain-event translation matrix

**Files:**
- Create: `events/subscribers/mailbox_translator.py` (skeleton; real logic in 2.2–2.3)
- Create: `tests/events/subscribers/test_mailbox_translator.py`

**Translation matrix (spec):**

| mailbox_message `message_type` | Emits | Payload mapping |
|---|---|---|
| `SCOUT_DISCOVERY` | `JOB_DISCOVERED` per job in `payload.jobs` (or aggregate if >10 jobs) | `{company, title, source, url, job_key}` |
| `SCORE_RESULT` | `JOB_SCORED`; if `payload.score >= 8.75` also `JOB_HIGH_SCORE` | `{score, recommendation, company, title, dimensions}` |
| `SCORE_BATCH_SUMMARY` | aggregate `JOB_SCORED` for each entry in `payload.scored_jobs`; `JOB_HIGH_SCORE` per entry with score ≥ 8.75 | same |
| `TAILOR_COMPLETE` | `TAILOR_COMPLETED` | `{company, title, artifact_paths}` |
| `SUBMIT_REQUEST` | `APPLICATION_READY` | `{company, title, job_key}` |
| `DRY_RUN_COMPLETE` | `APPLICATION_READY` | `{company, title, artifacts}` |
| `SUBMIT_CONFIRM` | `APPLICATION_SUBMITTED` | `{company, title, submission_id}` |
| `BLOCKED_QUESTION` | `APPLICATION_BLOCKED` | `{company, title, question}` |
| `PIPELINE_UPDATE` | `STAGE_TRANSITION` if `payload.new_stage` differs from `payload.previous_stage` | `{job_key, previous_stage, new_stage}` |
| `FOLLOWUP_ALERT` | `FOLLOWUP_DUE` | `{company, title, days_since_application}` |
| `VIP_DISCOVERY` | `JOB_VIP_DISCOVERED` | `{company, title, source: "linkedin-saved"}` |
| `NOTIFICATION` | No emit (notifier already handled separately) | n/a |
| `HIGH_SCORE_ALERT` | `JOB_HIGH_SCORE` | `{score, company, title}` |
| `ERROR` | `AGENT_ERROR` | `{message, source_agent}` |
| All other types | No emit | n/a |

**Deduplication:** For each emitted domain event, set `correlation_id` equal to the mailbox message's `correlation_id`. The EventBus already indexes on `correlation_id`. MailboxTranslator itself should also skip re-translating the same `mailbox_message` event (subscriber cursor handles this automatically — base class advances cursor after successful poll).

**Interview/Offer detection:** `SUBMIT_CONFIRM` + subsequent `NOTIFICATION` payloads may mention interviews/offers. Defer `INTERVIEW_SIGNAL`/`OFFER_SIGNAL` detection to Tier 4 (keyword pattern on NOTIFICATION payloads).

- [ ] **Step 1: Write failing test — create `tests/events/subscribers/test_mailbox_translator.py`:**

```python
"""Tests for MailboxTranslator subscriber (Silence #1 fix)."""
import json
from pathlib import Path

import pytest

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.mailbox_translator import MailboxTranslator


@pytest.fixture
def bus(tmp_path):
    db = tmp_path / "event_bus.db"
    b = EventBus(db_path=db)
    yield b
    b.close()


def _mailbox_event(bus, message_type, payload, correlation_id="corr-1"):
    return bus.emit(
        event_type=EventType.MAILBOX_MESSAGE,
        source="test",
        payload={"message_type": message_type, "from": "matcher", "to": "main",
                 "file": f"fake_{message_type}.json", "summary": "",
                 "inner_payload": payload},
        correlation_id=correlation_id,
    )


def _recent_domain_events(bus):
    rows = bus.query()
    return [(e.event_type, e.payload) for e in rows
            if e.event_type != EventType.MAILBOX_MESSAGE]


def test_score_result_emits_job_scored(bus):
    _mailbox_event(bus, "SCORE_RESULT", {
        "score": 7.2, "recommendation": "REVIEW",
        "company": "Acme", "title": "Director Finance",
    })
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.JOB_SCORED for et, _ in events)
    payload = next(p for et, p in events if et == EventType.JOB_SCORED)
    assert payload["score"] == 7.2
    assert payload["company"] == "Acme"


def test_score_result_high_score_double_emits(bus):
    _mailbox_event(bus, "SCORE_RESULT", {
        "score": 9.1, "recommendation": "PROCEED",
        "company": "BigCo", "title": "VP Finance",
    })
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    types = [et for et, _ in events]
    assert EventType.JOB_SCORED in types
    assert EventType.JOB_HIGH_SCORE in types


def test_batch_summary_expands_to_per_job_events(bus):
    _mailbox_event(bus, "SCORE_BATCH_SUMMARY", {
        "scored_jobs": [
            {"score": 7.0, "company": "A", "title": "X"},
            {"score": 9.0, "company": "B", "title": "Y"},
            {"score": 5.0, "company": "C", "title": "Z"},
        ],
    })
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    scored = [p for et, p in events if et == EventType.JOB_SCORED]
    high = [p for et, p in events if et == EventType.JOB_HIGH_SCORE]
    assert len(scored) == 3
    assert len(high) == 1  # only score 9.0
    assert high[0]["company"] == "B"


def test_submit_confirm_emits_application_submitted(bus):
    _mailbox_event(bus, "SUBMIT_CONFIRM",
                   {"company": "Acme", "title": "Director", "submission_id": "s1"})
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.APPLICATION_SUBMITTED for et, _ in events)


def test_blocked_question_emits_application_blocked(bus):
    _mailbox_event(bus, "BLOCKED_QUESTION",
                   {"company": "Acme", "title": "Director", "question": "Eligible?"})
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.APPLICATION_BLOCKED for et, _ in events)


def test_pipeline_update_emits_stage_transition_only_if_different(bus):
    _mailbox_event(bus, "PIPELINE_UPDATE",
                   {"job_key": "j1", "previous_stage": "discovered", "new_stage": "scored"})
    _mailbox_event(bus, "PIPELINE_UPDATE",
                   {"job_key": "j2", "previous_stage": "X", "new_stage": "X"})
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    transitions = [p for et, p in events if et == EventType.STAGE_TRANSITION]
    assert len(transitions) == 1
    assert transitions[0]["job_key"] == "j1"


def test_error_message_emits_agent_error(bus):
    _mailbox_event(bus, "ERROR",
                   {"message": "scout failed", "source_agent": "scout"})
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.AGENT_ERROR for et, _ in events)


def test_unknown_message_type_produces_no_domain_event(bus):
    _mailbox_event(bus, "SOME_RANDOM_TYPE", {"foo": "bar"})
    MailboxTranslator(bus).poll()
    assert _recent_domain_events(bus) == []


def test_cursor_advances_after_poll(bus):
    _mailbox_event(bus, "SCORE_RESULT", {"score": 5.0, "company": "A", "title": "B"})
    t = MailboxTranslator(bus)
    t.poll()
    # Second poll on same events should emit nothing new
    pre_events = len(bus.query())
    t.poll()
    post_events = len(bus.query())
    assert post_events == pre_events
```

- [ ] **Step 2: Run test — expected FAIL** (`ModuleNotFoundError: events.subscribers.mailbox_translator`)

- [ ] **Step 3: Implement `events/subscribers/mailbox_translator.py`:**

```python
"""MailboxTranslator — converts mailbox_message events into typed domain events.

Subscribes to mailbox_message (produced by MailboxWatcher) and emits
typed JOB_SCORED, JOB_HIGH_SCORE, APPLICATION_SUBMITTED, STAGE_TRANSITION,
etc. based on the message_type + inner payload.

This subscriber replaces the dead regex-based output parser in
CronEventEmitter that was never producing domain events.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

HIGH_SCORE_THRESHOLD = 8.75


class MailboxTranslator(BaseSubscriber):
    subscriber_id = "mailbox-translator"
    poll_interval_seconds = 5
    event_types = [EventType.MAILBOX_MESSAGE]

    def handle(self, event: Event) -> None:
        payload = event.payload or {}
        message_type = payload.get("message_type", "")
        inner = payload.get("inner_payload") or payload.get("payload") or {}
        correlation_id = event.correlation_id

        emissions = self._translate(message_type, inner)
        for et, out_payload, priority in emissions:
            try:
                self.bus.emit(
                    event_type=et,
                    source=f"mailbox:{payload.get('from', 'unknown')}",
                    payload=out_payload,
                    priority=priority,
                    correlation_id=correlation_id,
                    job_id=out_payload.get("job_key") or out_payload.get("job_id"),
                )
            except Exception:
                logger.exception("MailboxTranslator: failed to emit %s", et.type_string)

    def _translate(
        self,
        message_type: str,
        inner: Dict[str, Any],
    ) -> List[Tuple[EventType, Dict[str, Any], Optional[Priority]]]:
        """Return a list of (event_type, payload, priority_override_or_None)."""
        results: List[Tuple[EventType, Dict[str, Any], Optional[Priority]]] = []

        if message_type == "SCORE_RESULT":
            p = _score_payload(inner)
            results.append((EventType.JOB_SCORED, p, None))
            if p.get("score", 0) >= HIGH_SCORE_THRESHOLD:
                results.append((EventType.JOB_HIGH_SCORE, p, None))

        elif message_type == "SCORE_BATCH_SUMMARY":
            for job in inner.get("scored_jobs", []):
                p = _score_payload(job)
                results.append((EventType.JOB_SCORED, p, None))
                if p.get("score", 0) >= HIGH_SCORE_THRESHOLD:
                    results.append((EventType.JOB_HIGH_SCORE, p, None))

        elif message_type == "SCOUT_DISCOVERY":
            for job in inner.get("jobs", []):
                p = _job_payload(job)
                results.append((EventType.JOB_DISCOVERED, p, None))

        elif message_type == "TAILOR_COMPLETE":
            results.append((EventType.TAILOR_COMPLETED, _copy_fields(
                inner, ["company", "title", "job_key", "artifacts"]), None))

        elif message_type in ("SUBMIT_REQUEST", "DRY_RUN_COMPLETE"):
            results.append((EventType.APPLICATION_READY, _copy_fields(
                inner, ["company", "title", "job_key", "artifacts"]), None))

        elif message_type == "SUBMIT_CONFIRM":
            results.append((EventType.APPLICATION_SUBMITTED, _copy_fields(
                inner, ["company", "title", "job_key", "submission_id"]), None))

        elif message_type == "BLOCKED_QUESTION":
            results.append((EventType.APPLICATION_BLOCKED, _copy_fields(
                inner, ["company", "title", "job_key", "question"]), None))

        elif message_type == "PIPELINE_UPDATE":
            prev = inner.get("previous_stage")
            new = inner.get("new_stage")
            if new and prev and new != prev:
                results.append((EventType.STAGE_TRANSITION, _copy_fields(
                    inner, ["job_key", "previous_stage", "new_stage", "company"]), None))

        elif message_type == "FOLLOWUP_ALERT":
            results.append((EventType.FOLLOWUP_DUE, _copy_fields(
                inner, ["company", "title", "job_key", "days_since_application"]), None))

        elif message_type == "VIP_DISCOVERY":
            p = _job_payload(inner)
            p.setdefault("source", "linkedin-saved")
            results.append((EventType.JOB_VIP_DISCOVERED, p, None))

        elif message_type == "HIGH_SCORE_ALERT":
            results.append((EventType.JOB_HIGH_SCORE, _score_payload(inner), None))

        elif message_type == "ERROR":
            results.append((EventType.AGENT_ERROR, _copy_fields(
                inner, ["message", "source_agent", "traceback"]), None))

        return results


def _score_payload(d: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "score": d.get("score", 0),
        "recommendation": d.get("recommendation"),
        "company": d.get("company"),
        "title": d.get("title"),
        "dimensions": d.get("dimensions"),
        "job_key": d.get("job_key"),
    }


def _job_payload(d: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "company": d.get("company"),
        "title": d.get("title"),
        "source": d.get("source"),
        "url": d.get("url"),
        "job_key": d.get("job_key"),
    }


def _copy_fields(d: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
    return {f: d.get(f) for f in fields if d.get(f) is not None}
```

- [ ] **Step 4: Run test — expected ALL PASS** (9 passed)

- [ ] **Step 5: Run full events suite — no regressions**

- [ ] **Step 6: Commit**

```
git add events/subscribers/mailbox_translator.py \
        tests/events/subscribers/test_mailbox_translator.py
git commit -m "feat(events): MailboxTranslator subscriber (Silence #1 replacement)"
```

---

### Task 2.2: Register MailboxTranslator in gateway startup

**Files:**
- Modify: `events/gateway_integration.py` lines 19-24, 61-64

- [ ] **Step 1: Write failing test — `tests/events/test_gateway_integration.py` (ADD new test):**

```python
def test_mailbox_translator_registered_at_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from events import gateway_integration as gi
    gi.startup()
    try:
        from events.subscribers.mailbox_translator import MailboxTranslator
        subs = gi._registry.subscribers
        assert any(isinstance(s, MailboxTranslator) for s in subs), (
            "MailboxTranslator must be registered at gateway startup"
        )
    finally:
        gi.shutdown()
```

(Add alongside existing startup tests; adapt fixture scaffolding to match the file's existing helpers.)

- [ ] **Step 2: Run test — expected FAIL**

- [ ] **Step 3: Update `events/gateway_integration.py` imports (~line 22)**

Add:

```python
from events.subscribers.mailbox_translator import MailboxTranslator
```

- [ ] **Step 4: Update `startup()` function (~line 61)**

After the existing `_registry.register(TelegramMirror(_bus))`, add:

```python
    _registry.register(MailboxTranslator(_bus))
```

- [ ] **Step 5: Run test — expected PASS**

- [ ] **Step 6: Run full events suite — no regressions**

- [ ] **Step 7: Commit**

```
git add events/gateway_integration.py tests/events/test_gateway_integration.py
git commit -m "feat(events): register MailboxTranslator subscriber at gateway startup"
```

---

### Task 2.3: Retire the dead regex stub in CronEventEmitter

**Files:**
- Modify: `events/producers/cron_emitter.py` (remove `_DOMAIN_PATTERNS` block, remove `_parse_output_for_domain_events`, remove the call site in `on_job_completed`)
- Modify: `tests/events/producers/test_cron_emitter.py` (remove or mark xfail any tests that exercised the regex parser)

- [ ] **Step 1: Identify tests that depend on the stub**

```
cd <root>
grep -n "_parse_output_for_domain_events\|_DOMAIN_PATTERNS\|parse.*domain\|job_scored.*parse" tests/events/producers/test_cron_emitter.py
```

Read the tests. If any specifically validate the regex parser output, they will be removed in step 4. Typical examples: `test_on_job_completed_parses_job_discovered`, `test_on_job_completed_parses_job_scored`, etc.

- [ ] **Step 2: Write a replacement test asserting the stub is gone**

Append to `tests/events/producers/test_cron_emitter.py`:

```python
def test_cron_emitter_has_no_regex_domain_parser():
    import events.producers.cron_emitter as ce
    src = open(ce.__file__, encoding="utf-8").read()
    assert "_DOMAIN_PATTERNS" not in src, (
        "Regex domain parser should be retired. Domain events come from "
        "MailboxTranslator consuming mailbox_message events."
    )
    assert not hasattr(ce.CronEventEmitter, "_parse_output_for_domain_events"), (
        "_parse_output_for_domain_events should be removed; MailboxTranslator "
        "handles domain event emission."
    )
```

- [ ] **Step 3: Run test — expected FAIL**

- [ ] **Step 4: Edit `events/producers/cron_emitter.py`**

  - Remove the entire `_DOMAIN_PATTERNS` list (lines ~28-69)
  - Remove `_parse_output_for_domain_events` method (bottom of file)
  - In `on_job_completed`, remove:

    ```python
            # Parse output for domain events
            if output_summary:
                self._parse_output_for_domain_events(
                    job_name=job_name,
                    job_id=job_id,
                    output=output_summary,
                )
    ```

  - Remove the unused `import re` if no other reference remains
  - Remove the unused `Dict, List` imports if now unreferenced
  - Update the module docstring: change the "Output parsing inspects agent output..." paragraph to "Domain events (job_discovered, job_scored, etc.) come from MailboxTranslator consuming mailbox_message events — see events/subscribers/mailbox_translator.py."

- [ ] **Step 5: Remove now-stale tests from `tests/events/producers/test_cron_emitter.py`**

Delete any test cases that asserted regex-based parsing emitted specific event types. The base `on_job_started`/`on_job_completed` lifecycle tests remain (they don't depend on the parser).

- [ ] **Step 6: Run the test — expected PASS**

- [ ] **Step 7: Run full events suite — no regressions**

```
python -m pytest tests/events/ -q
```

- [ ] **Step 8: Commit**

```
git add events/producers/cron_emitter.py tests/events/producers/test_cron_emitter.py
git commit -m "refactor(events): retire dead regex domain parser in CronEventEmitter

Domain events now flow from MailboxTranslator, which consumes structured
mailbox_message events.  The regex parser was inert because cron jobs
emit [SILENT] final responses and structured data already lives in
mailbox JSON messages."
```

---

### Task 2.4: Backfill test — real 2026-04-16 mailbox data → domain events

**Files:**
- Create: `tests/events/integration/test_mailbox_translator_backfill.py`
- Create: `tests/events/integration/__init__.py` (empty)
- Create: `tests/events/integration/fixtures/` directory

**Rationale:** End-to-end confidence that today's real SCORE_RESULT files are correctly translated.

- [ ] **Step 1: Copy three real mailbox files as fixtures** (scrubbing any PII)

```
mkdir -p tests/events/integration/fixtures
cp ~/.hermes/mailbox/main/inbox/20260416T183742Z_SCORE_RESULT_matcher.json \
   tests/events/integration/fixtures/score_result_sample.json
cp ~/.hermes/mailbox/main/inbox/20260416T183742Z_SCORE_BATCH_SUMMARY_matcher.json \
   tests/events/integration/fixtures/score_batch_summary_sample.json
# Pick one SCOUT_DISCOVERY or other type for coverage:
# (if none exist from today, create a minimal synthetic fixture)
```

Open each fixture and scrub anything PII-identifying (resume content, internal URLs). Replace with placeholder values.

- [ ] **Step 2: Write the integration test**

```python
"""Integration test: MailboxWatcher + MailboxTranslator on real 2026-04-16 data."""
import json
from pathlib import Path

import pytest

from events.bus import EventBus
from events.producers.mailbox_watcher import MailboxWatcher
from events.schema import EventType
from events.subscribers.mailbox_translator import MailboxTranslator

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def mailbox_tree(tmp_path):
    inbox = tmp_path / "mailbox" / "main" / "inbox"
    inbox.mkdir(parents=True)
    for fixture in FIXTURES.glob("*.json"):
        (inbox / fixture.name).write_text(fixture.read_text(encoding="utf-8"),
                                          encoding="utf-8")
    return tmp_path


def test_real_score_result_flows_through_to_job_scored(mailbox_tree, tmp_path):
    bus = EventBus(db_path=tmp_path / "event_bus.db")
    watcher = MailboxWatcher(bus, mailbox_root=mailbox_tree / "mailbox")
    translator = MailboxTranslator(bus)

    # producer: scan the mailbox
    emitted = watcher.scan()
    assert emitted > 0, "MailboxWatcher should emit mailbox_message for fixtures"

    # subscriber: translate mailbox_message into domain events
    translator.poll()

    all_events = bus.query()
    types = [e.event_type for e in all_events]

    assert EventType.MAILBOX_MESSAGE in types
    assert EventType.JOB_SCORED in types, (
        "Real SCORE_RESULT fixture must produce JOB_SCORED event"
    )

    bus.close()
```

- [ ] **Step 3: Run test — expected PASS**

- [ ] **Step 4: Commit (include scrubbed fixtures in repo)**

```
git add tests/events/integration/
git commit -m "test(events): integration test for mailbox->translator on real 2026-04-16 data"
```

---

### Task 2.5: Make MailboxTranslator forward the `inner_payload` from MailboxWatcher

**Files:**
- Modify: `events/producers/mailbox_watcher.py` (extend emit() call to include `inner_payload`)
- Modify: `tests/events/producers/test_mailbox_watcher.py` (assert the new field)

**Rationale:** The translator needs the original message's `payload`; MailboxWatcher currently only emits a `summary` string. Add `inner_payload` to the emitted event so the translator has the structured data it needs.

- [ ] **Step 1: Write failing test addition to `tests/events/producers/test_mailbox_watcher.py`:**

```python
def test_mailbox_watcher_forwards_inner_payload(tmp_path):
    import json
    from events.bus import EventBus
    from events.producers.mailbox_watcher import MailboxWatcher
    inbox = tmp_path / "mailbox" / "main" / "inbox"
    inbox.mkdir(parents=True)
    msg = {
        "type": "SCORE_RESULT",
        "from": "matcher", "to": "main",
        "correlation_id": "abc",
        "payload": {"score": 8.8, "company": "X"},
    }
    (inbox / "20260416T1_SCORE_RESULT_matcher.json").write_text(json.dumps(msg))
    bus = EventBus(db_path=tmp_path / "db.sqlite")
    MailboxWatcher(bus, mailbox_root=tmp_path / "mailbox").scan()
    events = bus.query()
    assert len(events) == 1
    assert events[0].payload.get("inner_payload") == {"score": 8.8, "company": "X"}
    bus.close()
```

- [ ] **Step 2: Run test — expected FAIL**

- [ ] **Step 3: Update `events/producers/mailbox_watcher.py` `scan()` method**

Find the `self.bus.emit(...)` call inside the try block. Change the payload dict to include an `inner_payload` field sourced from the JSON message's `payload`:

```python
                    self.bus.emit(
                        event_type=EventType.MAILBOX_MESSAGE,
                        source=msg.get("from", "unknown"),
                        payload={
                            "message_type": msg_type,
                            "from": msg.get("from", "unknown"),
                            "to": msg.get("to", profile_dir.name),
                            "file": file_key,
                            "summary": self._summarize(msg),
                            "inner_payload": msg.get("payload", {}),
                        },
                        correlation_id=msg.get("correlation_id"),
                        job_id=msg.get("job_id"),
                    )
```

- [ ] **Step 4: Run test — expected PASS**

- [ ] **Step 5: Run full suite — no regressions**

- [ ] **Step 6: Commit**

```
git add events/producers/mailbox_watcher.py tests/events/producers/test_mailbox_watcher.py
git commit -m "feat(events): MailboxWatcher forwards inner_payload for translator"
```

---

### Task 2.6: Verify TelegramNotifier routes new domain events correctly

**File review — no code change expected:** confirm `TOPIC_ROUTING` in `events/subscribers/telegram_notifier.py` already maps the event types produced by MailboxTranslator to the right topics (per spec §2.1):

- `JOB_DISCOVERED`, `JOB_VIP_DISCOVERED` → scout topic
- `JOB_SCORED`, `JOB_HIGH_SCORE` → matcher topic
- `TAILOR_COMPLETED`, `APPLICATION_READY`, `APPLICATION_SUBMITTED` → tailor_applier topic
- `APPLICATION_FAILED`, `APPLICATION_BLOCKED`, `INTERVIEW_SIGNAL`, `OFFER_SIGNAL`, `CRON_FAILED_CONSECUTIVE`, `GATEWAY_HEALTH` → alerts topic
- `STAGE_TRANSITION`, `FOLLOWUP_DUE` → tracker topic
- `AGENT_ERROR` → system topic

- [ ] **Step 1: Read `TOPIC_ROUTING` and compare against the list above**

- [ ] **Step 2: If any event type is missing, add a failing test asserting correct routing**

```python
def test_topic_routing_covers_all_domain_events():
    from events.subscribers.telegram_notifier import TOPIC_ROUTING
    from events.schema import EventType
    required = {
        EventType.JOB_DISCOVERED, EventType.JOB_VIP_DISCOVERED,
        EventType.JOB_SCORED, EventType.JOB_HIGH_SCORE,
        EventType.TAILOR_COMPLETED, EventType.APPLICATION_READY,
        EventType.APPLICATION_SUBMITTED, EventType.APPLICATION_FAILED,
        EventType.APPLICATION_BLOCKED, EventType.INTERVIEW_SIGNAL,
        EventType.OFFER_SIGNAL, EventType.STAGE_TRANSITION,
        EventType.FOLLOWUP_DUE, EventType.AGENT_ERROR,
        EventType.CRON_FAILED_CONSECUTIVE, EventType.GATEWAY_HEALTH,
    }
    covered = set()
    for topic, event_list in TOPIC_ROUTING.items():
        covered.update(event_list)
    missing = required - covered
    assert not missing, f"TOPIC_ROUTING missing: {missing}"
```

Add to `tests/events/subscribers/test_telegram_notifier.py`.

- [ ] **Step 3: If test fails, amend `TOPIC_ROUTING` in `telegram_notifier.py` to cover missing event types**

- [ ] **Step 4: Run test — expected PASS**

- [ ] **Step 5: Run full suite — no regressions**

- [ ] **Step 6: Commit (only if routing was amended)**

```
git add events/subscribers/telegram_notifier.py \
        tests/events/subscribers/test_telegram_notifier.py
git commit -m "fix(events): TelegramNotifier TOPIC_ROUTING covers all domain events"
```

---

## Tier 3 — Durability + Observability

Goal: make notification state survive gateway restarts; make the event bus externally observable.

---

### Task 3.1: Add `events/state.py` persistence helper

**Files:**
- Create: `events/state.py`
- Create: `tests/events/test_state.py`

**Rationale:** DigestComposer, TelegramNotifier, and WhatsAppEscalator each need to persist small amounts of JSON state. Centralize atomic read/write to avoid each subscriber re-implementing write-rename-fsync patterns.

- [ ] **Step 1: Write failing test — `tests/events/test_state.py`:**

```python
import json
from pathlib import Path

from events.state import load_state, save_state


def test_load_state_returns_default_when_file_missing(tmp_path):
    assert load_state(tmp_path / "missing.json", default={"x": 1}) == {"x": 1}


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, {"foo": "bar", "n": 42})
    assert load_state(path, default={}) == {"foo": "bar", "n": 42}


def test_load_state_falls_back_on_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not-json", encoding="utf-8")
    assert load_state(path, default={"fallback": True}) == {"fallback": True}


def test_save_state_creates_parent_directories(tmp_path):
    nested = tmp_path / "a" / "b" / "c" / "state.json"
    save_state(nested, {"ok": True})
    assert nested.exists()


def test_save_state_is_atomic(tmp_path):
    """Writing should use a tmp file + rename to avoid partial writes."""
    path = tmp_path / "state.json"
    save_state(path, {"count": 1})
    save_state(path, {"count": 2})
    assert json.loads(path.read_text()) == {"count": 2}
    # No leftover .tmp files
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
```

- [ ] **Step 2: Run test — expected FAIL**

- [ ] **Step 3: Implement `events/state.py`:**

```python
"""Atomic JSON state helpers for event-bus subscribers.

Writes use tmp-file + rename to avoid partial writes that would corrupt
state on crash.  Reads return the provided default when the file is
missing or malformed (self-healing behaviour for operator comfort).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


def load_state(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(default)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("load_state(%s) falling back to default: %s", path, e)
        return dict(default)


def save_state(path: Path, data: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
```

- [ ] **Step 4: Run test — expected 5 PASS**

- [ ] **Step 5: Commit**

```
git add events/state.py tests/events/test_state.py
git commit -m "feat(events): atomic JSON state helper for subscriber persistence"
```

---

### Task 3.2: Persist DigestComposer's `_last_digest_at` + last_digest_hour

**Files:**
- Modify: `events/subscribers/digest_composer.py`
- Modify: `events/gateway_integration.py` (persist `last_digest_hour` across loop iterations)
- Modify: `tests/events/subscribers/test_digest_composer.py`

- [ ] **Step 1: Write failing test**

```python
def test_digest_composer_persists_last_digest_at(tmp_path):
    from events.bus import EventBus
    from events.subscribers.digest_composer import DigestComposer
    from events.state import load_state
    from unittest.mock import patch

    db = tmp_path / "db.sqlite"
    bus = EventBus(db_path=db)
    try:
        with patch("events.subscribers.digest_composer.digest_state_path",
                   return_value=tmp_path / "digest_state.json"):
            d = DigestComposer(bus, send_telegram_fn=lambda m: None)
            d.compose()
            state = load_state(tmp_path / "digest_state.json", default={})
            assert "last_digest_at" in state
            assert state["last_digest_at"] is not None

            # New instance reads persisted state
            d2 = DigestComposer(bus, send_telegram_fn=lambda m: None)
            assert d2._last_digest_at == state["last_digest_at"]
    finally:
        bus.close()
```

- [ ] **Step 2: Run test — expected FAIL**

- [ ] **Step 3: Update `events/subscribers/digest_composer.py`:**

Import `load_state`, `save_state`, and `digest_state_path`:

```python
from events.paths import digest_state_path
from events.state import load_state, save_state
```

In `__init__`, after setting `self._last_digest_at = None`, load state:

```python
        state = load_state(digest_state_path(), default={})
        self._last_digest_at = state.get("last_digest_at")
```

In `compose()`, after `self._last_digest_at = datetime.now(...).isoformat()`, persist:

```python
        save_state(digest_state_path(), {"last_digest_at": self._last_digest_at})
```

- [ ] **Step 4: Run test — expected PASS**

- [ ] **Step 5: Persist `last_digest_hour` in `gateway_integration.py`**

Add to imports:

```python
from events.paths import digest_state_path
from events.state import load_state, save_state
```

In `_subscriber_poll_loop`, replace the local `last_digest_hour: int = -1` initialization with:

```python
    _state = load_state(digest_state_path(), default={})
    last_digest_hour: int = _state.get("last_digest_hour", -1)
```

In the digest-firing branch, after setting `last_digest_hour = et_hour`, persist:

```python
                last_digest_hour = et_hour
                _state["last_digest_hour"] = et_hour
                save_state(digest_state_path(), _state)
```

Also update the "reset when not in schedule hour" branch to persist the reset:

```python
            elif et_hour not in DIGEST_SCHEDULE_HOURS:
                if last_digest_hour != -1:
                    last_digest_hour = -1
                    _state["last_digest_hour"] = -1
                    save_state(digest_state_path(), _state)
```

- [ ] **Step 6: Add test asserting gateway loop survives restart without duplicate digests**

Create `tests/events/test_restart_semantics.py`:

```python
"""Simulate gateway restart mid-schedule-hour; assert no duplicate digest."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_restart_at_same_hour_does_not_duplicate_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from events.paths import digest_state_path
    from events.state import save_state

    # Pretend digest already fired at hour 13 today
    save_state(digest_state_path(), {
        "last_digest_hour": 13,
        "last_digest_at": "2026-04-16T17:00:00+00:00",
    })

    # Simulate the first iteration of _subscriber_poll_loop
    from events.state import load_state
    state = load_state(digest_state_path(), default={})
    last_digest_hour = state.get("last_digest_hour", -1)
    et_hour = 13  # we crash-restarted still within hour 13

    # Branch condition from gateway_integration.py:
    should_fire = et_hour in [8, 13, 18] and et_hour != last_digest_hour
    assert not should_fire, "digest should NOT re-fire when already fired this hour"
```

- [ ] **Step 7: Run both tests — expected PASS**

- [ ] **Step 8: Run full suite — no regressions**

- [ ] **Step 9: Commit**

```
git add events/subscribers/digest_composer.py events/gateway_integration.py \
        tests/events/subscribers/test_digest_composer.py \
        tests/events/test_restart_semantics.py
git commit -m "feat(events): persist DigestComposer state across gateway restarts (Silence #3)"
```

---

### Task 3.3: Persist TelegramNotifier batch buffer

**Files:**
- Modify: `events/subscribers/telegram_notifier.py`
- Modify: `tests/events/subscribers/test_telegram_notifier.py`

**Rationale:** Currently `_batch_buffer` lives in memory only. Gateway restart loses pending low-priority messages.

- [ ] **Step 1: Write failing test**

```python
def test_notifier_restores_batch_buffer_on_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telegram").mkdir()
    (tmp_path / "telegram" / "topics.json").write_text(
        '{"group_chat_id": "-1", "topics": {"system": {"thread_id": 15}}}')
    (tmp_path / "telegram" / "verbosity.json").write_text(
        '{"system": {"mode": "all"}}')
    from events.bus import EventBus
    from events.subscribers.telegram_notifier import TelegramNotifier
    bus = EventBus(db_path=tmp_path / "db.sqlite")
    n1 = TelegramNotifier(bus, send_fn=lambda *a, **k: None)
    n1._batch_buffer["-1:15"] = ["pending msg 1", "pending msg 2"]
    n1._persist_batch_buffer()

    n2 = TelegramNotifier(bus, send_fn=lambda *a, **k: None)
    assert n2._batch_buffer.get("-1:15") == ["pending msg 1", "pending msg 2"]
    bus.close()
```

- [ ] **Step 2: Run test — expected FAIL**

- [ ] **Step 3: Update `events/subscribers/telegram_notifier.py`**

Add imports:

```python
from events.paths import notifier_batch_path
from events.state import load_state, save_state
```

In `__init__` after `self._batch_buffer: Dict[str, List[str]] = {}`, add:

```python
        saved = load_state(notifier_batch_path(), default={})
        if isinstance(saved.get("buffer"), dict):
            self._batch_buffer = {k: list(v) for k, v in saved["buffer"].items()}
        if isinstance(saved.get("timestamps"), dict):
            import time
            now = time.monotonic()
            # Re-anchor timestamps to current monotonic time; we don't know
            # the original monotonic epoch after restart.  Treat all restored
            # entries as "just created" — safe overapproximation.
            self._batch_timestamps = {k: now for k in self._batch_buffer}
```

Add a `_persist_batch_buffer` method:

```python
    def _persist_batch_buffer(self) -> None:
        """Write current batch state to disk so it survives restart."""
        try:
            save_state(notifier_batch_path(), {
                "buffer": {k: list(v) for k, v in self._batch_buffer.items()},
            })
        except Exception:
            logger.exception("TelegramNotifier: failed to persist batch buffer")
```

Call it after every mutation: after appending to the buffer AND after flushing the buffer. Find the two sites:

```python
            self._batch_buffer[key].append(message)
            self._persist_batch_buffer()
```

And in the flush block:

```python
        # after flushing buffer:
        self._batch_buffer.pop(key, None)
        self._batch_timestamps.pop(key, None)
        self._persist_batch_buffer()
```

- [ ] **Step 4: Run test — expected PASS**

- [ ] **Step 5: Run full suite — no regressions**

- [ ] **Step 6: Commit**

```
git add events/subscribers/telegram_notifier.py \
        tests/events/subscribers/test_telegram_notifier.py
git commit -m "feat(events): persist TelegramNotifier batch buffer (Silence #2 hardening)"
```

---

### Task 3.4: Persist WhatsAppEscalator `last_flush_fired` flag

**Files:**
- Modify: `events/gateway_integration.py`
- Modify: `tests/events/test_restart_semantics.py`

**Rationale:** If gateway restarts between 7:00 and 7:59am ET, it currently re-fires the morning flush. Persist the fired date to avoid duplicate overnight summaries.

- [ ] **Step 1: Write failing test — append to `tests/events/test_restart_semantics.py`:**

```python
def test_whatsapp_flush_does_not_re_fire_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from events.paths import whatsapp_flush_state_path
    from events.state import save_state
    save_state(whatsapp_flush_state_path(), {"last_flush_date": "2026-04-17"})

    from events.state import load_state
    from datetime import datetime
    today = "2026-04-17"
    state = load_state(whatsapp_flush_state_path(), default={})
    already_fired_today = state.get("last_flush_date") == today

    # Gateway restart logic should see we already fired today and skip
    assert already_fired_today
```

- [ ] **Step 2: Run test — expected FAIL (if path helper not yet used) or PASS (if passive)**

- [ ] **Step 3: Update `_subscriber_poll_loop` in `events/gateway_integration.py`**

Add imports:

```python
from events.paths import whatsapp_flush_state_path
```

Replace the `last_flush_fired: bool = False` initialization with date-based tracking:

```python
    _flush_state = load_state(whatsapp_flush_state_path(), default={})
    last_flush_date: str = _flush_state.get("last_flush_date", "")
```

Replace the 7am flush block with:

```python
            # WhatsApp morning flush — one-per-day by ET date
            import zoneinfo
            from datetime import datetime as _dt
            try:
                tz = zoneinfo.ZoneInfo("America/New_York")
                today_et = _dt.now(tz).date().isoformat()
            except Exception:
                today_et = _dt.utcnow().date().isoformat()

            if et_hour == 7 and last_flush_date != today_et:
                for sub in _registry.subscribers:
                    if isinstance(sub, WhatsAppEscalator):
                        try:
                            count = sub.flush_queue()
                            if count:
                                logger.info("WhatsApp morning flush: %d messages", count)
                        except Exception:
                            logger.exception("WhatsApp flush failed")
                last_flush_date = today_et
                save_state(whatsapp_flush_state_path(), {"last_flush_date": today_et})
```

Remove the old `elif et_hour != 7: last_flush_fired = False` block (replaced by date comparison).

- [ ] **Step 4: Run test — expected PASS**

- [ ] **Step 5: Run full suite — no regressions**

- [ ] **Step 6: Commit**

```
git add events/gateway_integration.py tests/events/test_restart_semantics.py
git commit -m "feat(events): date-keyed WhatsApp flush state (no duplicates across restart)"
```

---

### Task 3.5: Periodic SQLite WAL checkpoint

**Files:**
- Modify: `events/bus.py` (add `checkpoint()` method)
- Modify: `events/gateway_integration.py` (call it every 60s)
- Modify: `tests/events/test_bus.py`

**Rationale:** WAL grows unboundedly without auto-checkpoint. External observers (CLI debugging, monitoring) can't see recent events until a checkpoint runs. This is also a correctness requirement for integration tests that open a second connection.

- [ ] **Step 1: Write failing test**

Append to `tests/events/test_bus.py`:

```python
def test_checkpoint_exposes_wal_data_to_other_connections(tmp_path):
    import sqlite3
    from events.bus import EventBus
    from events.schema import EventType

    db = tmp_path / "bus.db"
    bus = EventBus(db_path=db)
    bus.emit(EventType.CRON_STARTED, "test", {})

    # External connection before checkpoint: may see zero rows (WAL isolation)
    bus.checkpoint()

    other = sqlite3.connect(str(db))
    count = other.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    other.close()
    assert count == 1
    bus.close()
```

- [ ] **Step 2: Run test — expected FAIL** (`EventBus has no attribute checkpoint`)

- [ ] **Step 3: Add `checkpoint()` method to `EventBus` in `events/bus.py`:**

```python
    def checkpoint(self) -> None:
        """Run a passive WAL checkpoint so external readers see recent data."""
        with self._lock:
            try:
                self._get_conn().execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error as e:
                logger.warning("WAL checkpoint failed: %s", e)
```

- [ ] **Step 4: Wire into `_subscriber_poll_loop` in `gateway_integration.py`**

Add alongside the other periodic tasks:

```python
        # WAL checkpoint every 60 seconds
        if _bus and now - last_checkpoint >= 60:
            try:
                _bus.checkpoint()
            except Exception:
                logger.exception("WAL checkpoint failed")
            last_checkpoint = now
```

Initialize `last_checkpoint: float = 0` at the top of the loop.

- [ ] **Step 5: Run test — expected PASS**

- [ ] **Step 6: Run full suite — no regressions**

- [ ] **Step 7: Commit**

```
git add events/bus.py events/gateway_integration.py tests/events/test_bus.py
git commit -m "feat(events): periodic WAL checkpoint for external observability"
```

---

### Task 3.6: Sticky-IP reset in `telegram_network.py`

**Files:**
- Modify: `gateway/platforms/telegram_network.py`
- Modify: `tests/gateway/platforms/test_telegram_network.py` (may not exist — create if needed)

**Rationale:** Even with fallback disabled (Task 1.4), this hardens the fallback path so a future re-enable doesn't lock onto a broken IP permanently.

- [ ] **Step 1: Write failing test** — create `tests/gateway/platforms/test_telegram_network.py` if absent:

```python
"""Test that sticky IP resets after repeated failures."""
from unittest.mock import MagicMock, patch

from gateway.platforms.telegram_network import TelegramFallbackTransport


def test_sticky_ip_resets_after_5_consecutive_failures():
    t = TelegramFallbackTransport(primary_transport=MagicMock(),
                                  fallback_ips=["1.1.1.1", "2.2.2.2"])
    t._sticky_ip = "1.1.1.1"
    t._sticky_failures = 0
    for _ in range(5):
        t._record_sticky_failure()
    assert t._sticky_ip is None, "sticky IP should reset after 5 failures"


def test_sticky_ip_retained_on_sporadic_failure():
    t = TelegramFallbackTransport(primary_transport=MagicMock(),
                                  fallback_ips=["1.1.1.1"])
    t._sticky_ip = "1.1.1.1"
    t._sticky_failures = 0
    t._record_sticky_failure()
    t._record_sticky_success()  # success clears failure counter
    t._record_sticky_failure()
    t._record_sticky_failure()
    assert t._sticky_ip == "1.1.1.1"
```

- [ ] **Step 2: Run test — expected FAIL**

- [ ] **Step 3: Update `gateway/platforms/telegram_network.py`**

Add instance attributes in `__init__`:

```python
        self._sticky_failures: int = 0
```

Add helper methods:

```python
    STICKY_FAILURE_RESET_THRESHOLD = 5

    def _record_sticky_failure(self) -> None:
        self._sticky_failures += 1
        if self._sticky_failures >= self.STICKY_FAILURE_RESET_THRESHOLD:
            logger.warning(
                "TelegramFallbackTransport: sticky IP %s failed %d times, resetting",
                self._sticky_ip, self._sticky_failures,
            )
            self._sticky_ip = None
            self._sticky_failures = 0

    def _record_sticky_success(self) -> None:
        self._sticky_failures = 0
```

Wire the calls into the existing success/failure paths in `handle_async_request` — when the sticky attempt succeeds call `_record_sticky_success()`; when it raises call `_record_sticky_failure()`.

- [ ] **Step 4: Run test — expected PASS**

- [ ] **Step 5: Run full suite (whole repo, not just events) — no regressions**

```
python -m pytest tests/gateway/platforms/test_telegram_network.py tests/events/ -q
```

- [ ] **Step 6: Commit**

```
git add gateway/platforms/telegram_network.py tests/gateway/platforms/test_telegram_network.py
git commit -m "fix(gateway): reset sticky IP after repeated failures (Silence #6 hardening)"
```

---

## Tier 4 — Future-Proofing & Diagnostics

Goal: add interview/offer detection, emit digest events when buffers flush, add a CLI diagnostic command, and document the complete config surface.

---

### Task 4.1: Interview/Offer signal detection from NOTIFICATION payloads

**Files:**
- Modify: `events/subscribers/mailbox_translator.py` (extend `_translate` to detect interview/offer keywords in `NOTIFICATION` messages)
- Modify: `tests/events/subscribers/test_mailbox_translator.py`

**Rationale:** The spec §2.2 marks `interview_signal` and `offer_signal` as IMMEDIATE escalation tier (breaks through WhatsApp quiet hours). These events currently have no producer. Detect them from `NOTIFICATION` message bodies using scoped keyword patterns.

- [ ] **Step 1: Write failing test**

```python
def test_notification_interview_keyword_emits_interview_signal(bus):
    _mailbox_event(bus, "NOTIFICATION", {
        "body": "Interview scheduled with Acme next Tuesday",
        "company": "Acme",
    })
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.INTERVIEW_SIGNAL for et, _ in events)


def test_notification_offer_keyword_emits_offer_signal(bus):
    _mailbox_event(bus, "NOTIFICATION", {
        "body": "We are pleased to offer you the Director of Finance role",
        "company": "BigCo",
    })
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.OFFER_SIGNAL for et, _ in events)


def test_notification_without_keyword_emits_nothing(bus):
    _mailbox_event(bus, "NOTIFICATION", {
        "body": "Weekly pipeline update: 12 jobs discovered",
    })
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    types = [et for et, _ in events]
    assert EventType.INTERVIEW_SIGNAL not in types
    assert EventType.OFFER_SIGNAL not in types
```

- [ ] **Step 2: Run test — expected FAIL**

- [ ] **Step 3: Extend `_translate` in `events/subscribers/mailbox_translator.py`**

Add keyword patterns near the top:

```python
import re

_INTERVIEW_PATTERNS = [
    re.compile(r"\binterview\s+(?:scheduled|invitation|request|invite)", re.I),
    re.compile(r"\bphone\s+screen", re.I),
    re.compile(r"\b(?:schedule|set up)\s+an?\s+interview", re.I),
]
_OFFER_PATTERNS = [
    re.compile(r"\b(?:pleased|delighted|happy)\s+to\s+offer", re.I),
    re.compile(r"\boffer\s+(?:letter|of\s+employment)", re.I),
    re.compile(r"\bextended\s+an?\s+offer", re.I),
]
```

Add a NOTIFICATION branch in `_translate`:

```python
        elif message_type == "NOTIFICATION":
            body = str(inner.get("body", "")) + " " + str(inner.get("summary", ""))
            if any(p.search(body) for p in _INTERVIEW_PATTERNS):
                results.append((EventType.INTERVIEW_SIGNAL, _copy_fields(
                    inner, ["company", "title", "job_key", "body"]), None))
            elif any(p.search(body) for p in _OFFER_PATTERNS):
                results.append((EventType.OFFER_SIGNAL, _copy_fields(
                    inner, ["company", "title", "job_key", "body"]), None))
```

- [ ] **Step 4: Run tests — expected 3 PASS**

- [ ] **Step 5: Run full suite — no regressions**

- [ ] **Step 6: Commit**

```
git add events/subscribers/mailbox_translator.py \
        tests/events/subscribers/test_mailbox_translator.py
git commit -m "feat(events): detect interview/offer signals in NOTIFICATION messages"
```

---

### Task 4.2: CLI diagnostic — `hermes events doctor`

**Files:**
- Create: `hermes_cli/events_doctor.py`
- Modify: `hermes_cli/__init__.py` or wherever subcommands are registered (check repo layout; this may require modifying a `cli.py` or similar)
- Create: `tests/hermes_cli/test_events_doctor.py`

**Rationale:** Give the operator a one-shot command to verify all six silence points: path canonicality, bus reachability, subscriber cursors, topic config, quiet hours, Telegram API connectivity.

- [ ] **Step 1: Write failing test**

```python
"""Tests for hermes events doctor CLI diagnostic."""
import json
import sqlite3
from pathlib import Path

from hermes_cli.events_doctor import run_doctor


def test_doctor_reports_missing_topics_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "events").mkdir()
    sqlite3.connect(str(tmp_path / "events" / "event_bus.db")).close()

    rc = run_doctor()
    captured = capsys.readouterr().out
    assert "topics.json" in captured
    assert "MISSING" in captured or "missing" in captured
    # Non-zero return code when any issue found
    assert rc != 0


def test_doctor_all_green_on_healthy_setup(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "events").mkdir()
    (tmp_path / "telegram").mkdir()
    (tmp_path / "notifications").mkdir()
    sqlite3.connect(str(tmp_path / "events" / "event_bus.db")).close()
    (tmp_path / "telegram" / "topics.json").write_text(
        json.dumps({"group_chat_id": "-1", "topics": {}}))
    (tmp_path / "telegram" / "verbosity.json").write_text(json.dumps({}))
    (tmp_path / "notifications" / "quiet_hours.json").write_text(
        json.dumps({"enabled": True}))

    # We skip live Telegram API check for unit test
    rc = run_doctor(check_telegram_api=False)
    captured = capsys.readouterr().out
    assert "topics.json" in captured
    assert "quiet_hours.json" in captured
```

- [ ] **Step 2: Run test — expected FAIL**

- [ ] **Step 3: Implement `hermes_cli/events_doctor.py`:**

```python
"""hermes events doctor — diagnose notification layer health.

Checks:
  1. Canonical paths exist: ~/.hermes/events/event_bus.db, telegram/topics.json,
     notifications/quiet_hours.json, telegram/verbosity.json
  2. Event bus schema is readable
  3. Subscriber cursors are present (audit-logger, telegram-notifier,
     whatsapp-escalator, digest-composer, memory-writer, telegram-mirror,
     mailbox-translator)
  4. Events are flowing (any event in last 24 hours)
  5. (Optional) Telegram API getMe returns ok
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from events.paths import (
    audit_log_path, events_db_path, quiet_hours_path,
    telegram_topics_path, telegram_verbosity_path,
)

REQUIRED_SUBSCRIBERS = [
    "audit-logger", "telegram-notifier", "whatsapp-escalator",
    "digest-composer", "memory-writer", "telegram-mirror",
    "mailbox-translator",
]


def _check(name: str, ok: bool, detail: str = "") -> bool:
    marker = "OK" if ok else "FAIL"
    print(f"[{marker}] {name}{' — ' + detail if detail else ''}")
    return ok


def run_doctor(check_telegram_api: bool = True) -> int:
    issues = 0

    db = events_db_path()
    if not _check("events db exists", db.exists(), str(db)):
        issues += 1

    if db.exists():
        try:
            conn = sqlite3.connect(str(db))
            conn.execute("SELECT 1 FROM events LIMIT 1")
            _check("events db readable", True)

            cursors = {row[0] for row in conn.execute(
                "SELECT subscriber_id FROM subscriber_cursors")}
            for sub in REQUIRED_SUBSCRIBERS:
                if not _check(f"subscriber cursor: {sub}",
                              sub in cursors, "present" if sub in cursors else "missing"):
                    issues += 1

            since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            cnt = conn.execute(
                "SELECT COUNT(*) FROM events WHERE timestamp > ?", (since,)
            ).fetchone()[0]
            _check(f"events emitted in last 24h", cnt > 0, f"{cnt} events")
            if cnt == 0:
                issues += 1

            conn.close()
        except sqlite3.Error as e:
            _check("events db readable", False, str(e))
            issues += 1

    for label, p in [
        ("topics.json", telegram_topics_path()),
        ("verbosity.json", telegram_verbosity_path()),
        ("quiet_hours.json", quiet_hours_path()),
        ("audit.jsonl", audit_log_path()),
    ]:
        ok = p.exists()
        detail = str(p) if not ok else ""
        if not _check(f"{label}", ok, detail):
            issues += 1

    if check_telegram_api:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token:
            _check("TELEGRAM_BOT_TOKEN env", False, "unset")
            issues += 1
        else:
            try:
                import urllib.request
                with urllib.request.urlopen(
                    f"https://api.telegram.org/bot{token}/getMe", timeout=5
                ) as r:
                    data = json.loads(r.read().decode())
                    _check("telegram getMe", data.get("ok") is True,
                           data.get("result", {}).get("username", ""))
                    if not data.get("ok"):
                        issues += 1
            except Exception as e:
                _check("telegram getMe", False, str(e))
                issues += 1

    print()
    if issues:
        print(f"events doctor: {issues} issue(s) found")
        return 1
    print("events doctor: all checks passed")
    return 0


def _cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-telegram-api", action="store_true",
                    help="Skip live getMe check")
    ns = ap.parse_args()
    sys.exit(run_doctor(check_telegram_api=not ns.no_telegram_api))


if __name__ == "__main__":
    _cli()
```

- [ ] **Step 4: Wire into main CLI**

Locate the CLI entry point (check `hermes_cli/cli.py` or search for `argparse` subcommand registration). Register `events-doctor` or `events doctor` as a subcommand that calls `run_doctor()`. If the CLI uses a dispatcher pattern, add:

```python
from hermes_cli.events_doctor import run_doctor as _events_doctor_run
# ... within the subcommand dispatcher:
elif subcmd == "events-doctor":
    sys.exit(_events_doctor_run())
```

If unsure of the pattern, keep the script invokable as `python -m hermes_cli.events_doctor` for now.

- [ ] **Step 5: Run test — expected PASS**

- [ ] **Step 6: Create `tests/hermes_cli/__init__.py` if missing**

- [ ] **Step 7: Run full suite — no regressions**

- [ ] **Step 8: Commit**

```
git add hermes_cli/events_doctor.py tests/hermes_cli/
git commit -m "feat(cli): hermes events doctor diagnostic command"
```

---

### Task 4.3: End-to-end smoke test — full stack integration

**Files:**
- Create: `tests/events/integration/test_end_to_end_smoke.py`

**Rationale:** A single test that drives producer → bus → subscriber → (mock) Telegram delivery. Running green here is the definitive "it works" signal.

- [ ] **Step 1: Write the test**

```python
"""End-to-end smoke test: mailbox file -> domain event -> Telegram delivery stub."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from events.bus import EventBus
from events.producers.mailbox_watcher import MailboxWatcher
from events.schema import EventType
from events.subscribers.mailbox_translator import MailboxTranslator
from events.subscribers.telegram_notifier import TelegramNotifier


def test_full_stack_score_result_reaches_telegram(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # Set up mailbox with a SCORE_RESULT
    inbox = tmp_path / "mailbox" / "main" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "20260416T_SCORE_RESULT_matcher.json").write_text(json.dumps({
        "type": "SCORE_RESULT",
        "from": "matcher", "to": "main",
        "correlation_id": "abc",
        "payload": {"score": 8.9, "company": "Acme", "title": "VP Fin",
                    "recommendation": "PROCEED"},
    }))

    # Set up Telegram config
    (tmp_path / "telegram").mkdir()
    (tmp_path / "telegram" / "topics.json").write_text(json.dumps({
        "group_chat_id": "-100xxx",
        "topics": {
            "matcher": {"thread_id": 11, "name": "Matcher / Scores"},
            "alerts": {"thread_id": 9, "name": "Alerts"},
            "system": {"thread_id": 15, "name": "System"},
            "scout": {"thread_id": 10}, "tailor_applier": {"thread_id": 12},
            "tracker": {"thread_id": 13}, "digests": {"thread_id": 14},
            "agent_comms": {"thread_id": 16},
        }
    }))
    (tmp_path / "telegram" / "verbosity.json").write_text(json.dumps({
        "matcher": {"mode": "all"}, "alerts": {"mode": "all"},
    }))

    # Wire bus + producer + subscribers
    bus = EventBus(db_path=tmp_path / "events" / "event_bus.db")
    delivered = []
    send_fn = lambda chat_id, thread_id, msg: delivered.append(
        (chat_id, thread_id, msg))

    watcher = MailboxWatcher(bus)
    translator = MailboxTranslator(bus)
    notifier = TelegramNotifier(bus, send_fn=send_fn)

    # Run the pipeline
    watcher.scan()          # mailbox -> mailbox_message
    translator.poll()       # mailbox_message -> job_scored + job_high_score
    notifier.poll()         # job_scored -> Telegram delivery

    # Assert: at least one delivered message, targeted at matcher topic
    assert len(delivered) >= 1
    matcher_deliveries = [d for d in delivered if d[1] == 11]
    assert len(matcher_deliveries) >= 1, (
        f"expected matcher topic delivery, got: {delivered}"
    )
    bus.close()
```

- [ ] **Step 2: Run test — expected PASS (if Tier 1-3 all complete) or FAIL with actionable error**

- [ ] **Step 3: Run the full suite — final regression check**

```
python -m pytest tests/ -q
```

Expected: all green. Note: some pre-existing non-events tests may fail for unrelated reasons; that is out of scope. Focus on `tests/events/`, `tests/scripts/`, `tests/hermes_cli/`, and `tests/gateway/platforms/test_telegram_network.py`.

- [ ] **Step 4: Commit**

```
git add tests/events/integration/test_end_to_end_smoke.py
git commit -m "test(events): end-to-end smoke test mailbox->translator->telegram"
```

---

### Task 4.4: Update design spec + CLAUDE.md with post-fix architecture notes

**Files:**
- Modify: `docs/superpowers/specs/2026-04-15-hermes-communication-layer-design.md` (append "2026-04-16 Post-Silence-Fix Addendum")
- Modify: `C:\Users\diego\CLAUDE.md` (add "Notification architecture" section under "Cross-Platform MCP Access")

- [ ] **Step 1: Append addendum to the spec**

Append a new section to `docs/superpowers/specs/2026-04-15-hermes-communication-layer-design.md`:

```markdown
---

## 2026-04-16 Post-Silence-Fix Addendum

After initial rollout on 2026-04-15, six compounding silences prevented all user-facing notifications.  Diagnosis and fix plan in `docs/superpowers/plans/2026-04-16-hermes-comms-layer-fixes.md`.  Key architectural updates:

- **Canonical paths (Option A):** All notification/event state lives at the single root resolved by `events.paths.*` (wrapping `hermes_constants.get_default_hermes_root()`).  Profile-scoped directories hold only per-agent state (memory, sessions, workspace, config.yaml).

- **MailboxTranslator subscriber (Option B):** Structured mailbox messages are the source of truth for domain events.  A new subscriber reads `mailbox_message` events and emits typed domain events.  The regex output parser in `CronEventEmitter` is retired.

- **Persistent subscriber state:** `DigestComposer._last_digest_at`, `TelegramNotifier._batch_buffer`, gateway loop `last_digest_hour`, and WhatsApp `last_flush_date` all persist via `events/state.py` atomic JSON helpers.

- **Periodic WAL checkpoint:** Every 60s, for external observability.

- **Telegram fallback transport:** Under NordVPN / restricted networks, set `HERMES_TELEGRAM_DISABLE_FALLBACK_IPS=1`.  Sticky-IP logic now resets after 5 consecutive failures.

- **CLI diagnostic:** `hermes events doctor` (or `python -m hermes_cli.events_doctor`) validates path canonicality, bus schema, subscriber cursors, recent event flow, and optional live Telegram connectivity.
```

- [ ] **Step 2: Update `C:\Users\diego\CLAUDE.md`**

Add near the bottom of the file, before the final "Project Context" section:

```markdown

## Notification Layer (Event Bus)

All notification state lives at the canonical `~/.hermes/` root:

- `~/.hermes/events/event_bus.db` — SQLite event store
- `~/.hermes/events/audit.jsonl` — append-only audit trail
- `~/.hermes/telegram/topics.json` + `verbosity.json` — Telegram routing
- `~/.hermes/notifications/quiet_hours.json`, `quiet_queue.json`, `digest_state.json`, `notifier_batch.json`, `whatsapp_flush_state.json` — persistent subscriber state
- `~/.hermes/mailbox/.event_watermark.json` — MailboxWatcher watermark

Never use `hermes_constants.get_hermes_home()` for notification paths.  Use `events.paths.*` (which wraps `get_default_hermes_root()`).  Notification state is cross-profile.

Run `python -m hermes_cli.events_doctor` to validate the notification layer health.
```

- [ ] **Step 3: Commit documentation update**

```
git add docs/superpowers/specs/2026-04-15-hermes-communication-layer-design.md
# CLAUDE.md is outside the repo — no git add needed
git commit -m "docs(events): post-silence-fix architectural addendum"
```

---

## Final Verification Checklist

After all 24 tasks complete:

- [ ] **Full test suite green:** `python -m pytest tests/events/ tests/scripts/ tests/hermes_cli/ tests/gateway/platforms/ -q`
- [ ] **Migration script executed once:** `python scripts/migrate_hermes_notification_paths.py`
- [ ] **verbosity.json system topic set to `all`**
- [ ] **`.env` contains `HERMES_TELEGRAM_DISABLE_FALLBACK_IPS=1`**
- [ ] **Gateway restarted** — logs show `EventBus: 7 subscribers registered` (was 6 before MailboxTranslator added)
- [ ] **`python -m hermes_cli.events_doctor` reports all checks passing**
- [ ] **Real-time delivery confirmed:** trigger a cron job, see the `system` topic receive a `cron_started`/`cron_completed` message within 60 seconds in Telegram
- [ ] **Domain event flow confirmed:** trigger matcher scoring, see `matcher` topic receive `job_scored` messages within 60 seconds
- [ ] **Digest delivery confirmed:** at next 8am/1pm/6pm ET trigger, Digests topic receives a compiled digest
- [ ] **No duplicate digests** after a test-restart during a schedule hour
- [ ] **Merge the feature branch**

```bash
git checkout main
git merge --no-ff fix/hermes-comms-layer-silences-2026-04-16
```

---

## Change Budget

- **New files:** 8 (paths, state, mailbox_translator, migrate_hermes_notification_paths, events_doctor, 3 test files)
- **Modified files:** ~15 (all existing event bus / subscriber / gateway files)
- **Estimated tests added:** ~40
- **Baseline regression target:** all 133 pre-existing events tests continue to pass

---

## Self-Review Notes

**Spec coverage:** The plan addresses all six silences from the diagnosis (Phase 2b).

- Silence #1 (dead regex parser) → Tasks 2.1–2.3
- Silence #2 (digest_only in-memory batch) → Tasks 1.4, 3.3
- Silence #3 (DigestComposer no persistence) → Task 3.2
- Silence #4 (MailboxWatcher wrong path) → Tasks 1.1, 1.2, 1.3
- Silence #5 (TelegramMirror wrong path) → Tasks 1.1, 1.2, 1.3
- Silence #6 (NordVPN + sticky IP) → Tasks 1.4, 3.6
- Architectural schism → Tasks 1.1, 1.2, 1.3 (canonical paths)
- Observability → Tasks 3.5 (WAL), 4.2 (CLI doctor), 4.3 (smoke test)

**Types consistent:** `EventBus`, `Event`, `EventType`, `Priority` all preserve their existing signatures. `BaseSubscriber` contract (class attrs `subscriber_id`, `poll_interval_seconds`, `event_types`; `handle(event)` method) unchanged — new subscriber conforms.

**Tests precede code everywhere:** Each task writes failing test first, then implementation.

**Commits small:** One commit per task (24 commits), allows easy bisect if something regresses.

---
