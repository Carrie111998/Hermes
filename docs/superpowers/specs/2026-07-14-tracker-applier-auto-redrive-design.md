# Tracker-Intent-Applier: Auto-Re-Drive of Partials + Partial-Backlog Alert — Design Spec

**Date:** 2026-07-14
**Status:** APPROVED (Diego, 2026-07-14, via superpowers:brainstorming). No code written before this session.
**Repo:** `~/.hermes/agent-src` (its own local-only git repo, author = Diego, **NEVER push**).
**Origin:** MemPalace `drawer_jobflow_applier-auto-redrive-design-2026-07-14_919c6adbda6f1155afac3fc3`
(wing=jobflow, room=applier-auto-redrive-design-2026-07-14). Follow-up items 3+4 of the
2026-07-13 partial-pileup work (`jobflow_applier_partial_pileup_drained_2026_07_14`).

---

## Problem

On the 2026-07-13 storm night, 13 `APPROVAL_INTENT` partials piled up in
`~/.hermes/mailbox/tracker/partial/` and **sat ~a day unnoticed**. Two gaps:

1. **No auto-recovery.** A partial (JobOps/:4100 step-4 failure) is left re-drivable — the
   idempotency key is unburned (only burned on a confirmed 2xx at step 6, per the 06a8feb23 fix)
   and `pipeline.json` (step 3) already succeeded — but nothing moves it back to `inbox/`. A human
   had to notice and re-drive by hand.
2. **No observability.** Nothing alerts when `partial/` accumulates, so the backlog was invisible.

Both are now *safe to automate* because jobflow-api :4100 commit `8d7b5f5` added (a) `SET LOCAL
lock_timeout=5s` + `statement_timeout=8s` in `transitionLegacyStage` + `recordManualSubmission`
(kills zombie commits) and (b) an idempotent no-op guard on `transitionLegacyStage` (re-driving an
already-applied intent is a clean no-op — no duplicate events, no re-fired notifications).

---

## HARD GATE (blocks *enabling* re-drive, not building it)

Auto-re-drive must **ship DISABLED** (feature flag `TRACKER_APPLIER_REDRIVE_ENABLED` default `"0"`)
until jobflow-api :4100 is restarted with `8d7b5f5`'s dist LIVE. Enabling requires **both**:

- **(a)** `grep -c "current_business_state === mappedState.businessState"
  ~/.hermes/services/jobflow-platform/services/jobflow-api/dist/modules/jobs/repository.js` == `1`
  (guard compiled into dist), **AND**
- **(b)** the running :4100 node process start time is **after** the dist mtime (process actually
  loaded the guarded code — node does not hot-reload).

**State as of 2026-07-14 (re-verified this session):** grep = `1`, dist mtime `2026-07-14 09:06:42`,
but running :4100 = **PID 30172, started 2026-07-13 17:09:49** — ~16h *before* the recompile. Gate
is **NOT met**; re-drive stays OFF. `:4100` restart is **Diego's call** — never auto-restart.

The **alert (Component 3) is NOT gated** — monitoring must not depend on the fix being live. It goes
live on any gateway restart.

---

## Architecture & invariants (confirmed by reading the code)

- Partial files ARE the original `*_INTENT_*.json` intents; the parser reads `message_id` +
  `idempotency_key` from file **content**, so the filename is free to carry a re-drive marker.
- `scan_inbox()` globs `*_INTENT_*.json`; that glob still matches `foo_INTENT_bar.rd1.json`.
- The applier is **single-threaded by design** (`is_applied`/`mark_applied`/`_move_to` are not
  race-free). Its SOLE driver is the dedicated 1s `_applier_poll_loop` in
  `events/gateway_integration.py` (the 24e65914a own-thread fix). **Any re-drive logic MUST run on
  that same thread** to preserve the single-writer invariant.
- `rename`/`replace`/`_move_to` **preserve mtime**. So a partial's raw file mtime is the intent's
  *creation* time, which would never space exponential-backoff retries. We need an explicit
  "entered-partial" clock via `os.utime(dest, None)` on partial landing.
- The step-3b `PIPELINE_UPDATE` mirror is re-emitted on every re-drive; downstream dedups by
  `idempotency_key`, bounded to ≤ `max_attempts` per stuck job. This matches existing manual
  re-drive behavior and is intentionally NOT gated (gating only the mirror desyncs the legacy vs
  tracker-canonical projections).

---

## Component 1 — `IntentApplier.redrive_partials()` (`intent_applier/applier.py`)

Pure filesystem logic; always acts (unit-testable, no env). Runs on the single-writer applier
thread. Each sweep globs `partial/` for `*_INTENT_*.json`; for each file:

- **Attempt count `N`** parsed from the filename `.rdN` marker: regex `\.rd(\d+)$` on the stem
  (`Path.stem`, i.e. `.json` already stripped); absent ⇒ `N=0`. Original intent stems never end in
  `.rd<digits>`, so the parse is unambiguous.
- **Eligibility:** `now - partial_mtime >= min(base * multiplier**N, max_backoff)`.
  Defaults `base=120s`, `multiplier=2`, `max_backoff=1800s` ⇒ eligible ages `120/240/480/960/1800s`.
- **Cap:** if `N >= max_attempts` (default `5`) ⇒ **SKIP** (leave in `partial/` for the alert —
  Diego's "leave + alert" choice; do **NOT** dead-letter). Partials aren't dead: step-3 already
  succeeded, only the Postgres mirror is pending, key unburned ⇒ stay re-drivable.
- **Re-drive:** rename stripping any old marker and appending `.rd{N+1}`, then move to `inbox/`.
  The next 1s `scan_inbox()` re-runs steps 3/3b/4.
- **Return:** `dict[str, str]` mapping filename → action (`"redriven" | "waiting" | "capped"`),
  for logging/telemetry.

**`_move_to_partial()` helper** = `_move_to` + `os.utime(dest, None)`. Used in **both** partial
branches of `apply_one()` (`except CircuitBreakerOpen` and `except JobOpsClientTransientError`) so
`partial_mtime` = "when it last entered partial/". `_move_to` stays generic (processed/dead-letter
must NOT touch mtime).

**Constructor params** (defaults; overridable by the subscriber from env):
`redrive_base_backoff=120.0`, `redrive_multiplier=2.0`, `redrive_max_backoff=1800.0`,
`max_redrive_attempts=5`.

---

## Component 2 — feature flag (`events/subscribers/tracker_intent_applier.py`)

`TrackerIntentApplierSubscriber` reads env `TRACKER_APPLIER_REDRIVE_ENABLED` (default `"0"`;
truthy = `1`/`true`/`yes`/`on`). Its `redrive_partials()` wrapper only calls
`self._applier.redrive_partials()` when the flag is enabled (else no-op returning `0`).
`IntentApplier.redrive_partials()` itself stays pure/always-acts. **The subscriber-level gate IS the
feature flag.** The subscriber also reads the backoff/attempt env overrides
(`TRACKER_APPLIER_REDRIVE_BASE_SECONDS`, `_MULTIPLIER`, `_MAX_BACKOFF_SECONDS`, `_MAX_ATTEMPTS`) and
passes them to the `IntentApplier` it builds in `startup()`.

Enabling requires Diego to: (1) restart :4100 to load `8d7b5f5`'s dist; (2) set
`TRACKER_APPLIER_REDRIVE_ENABLED=1` in `profiles/main/.env`; (3) restart the gateway (loads new
applier code — editable install — AND the env flag). An unrelated gateway restart keeps it OFF.

---

## Component 3 — `PartialBacklogMonitor` (`events/producers/partial_backlog_monitor.py`, NEW)

Sibling of `ResourcePressureMonitor` (`events/producers/resource_monitor.py`): **read-only** counts
`partial/`, **edge-triggered** with a re-arm cooldown (fire on rising edge, quiet while sustained,
re-ping every cooldown, falling edge re-arms). Injectable `sampler` + `clock` for testability.
**ALWAYS ON** — independent of the re-drive flag. Fires when `count > alert_threshold` (default `3`).

- New `EventType.TRACKER_PARTIAL_BACKLOG = ("tracker_partial_backlog", Priority.HIGH)` in
  `events/schema.py`.
- New `TOPIC_ROUTING` entry `'tracker_partial_backlog': 'jobflow_decisions'` in
  `events/subscribers/telegram_notifier.py` (the human-action lane).
- **Payload:** `{count, threshold, oldest_age_seconds, capped_count, sample_job_ids}` where
  `sample_job_ids` is capped to the first `SAMPLE_CAP` (default 10) partial job IDs (best-effort
  read of top-level `job_id` from file content; falls back to the filename stem on parse failure)
  and `capped_count = len(sample_job_ids)`.
- **Defaults (env-overridable at construction):** `alert_threshold=3`, `re_alert_cooldown_seconds=900`.
- Read-only counting is safe off the applier thread.

---

## Wiring (`events/gateway_integration.py`)

- **redrive:** a 60s monotonic-gated call to `_applier_subscriber.redrive_partials()` **inside**
  `_applier_poll_loop` (the single-writer applier thread), so re-drive and `scan_inbox` never race.
  `last_redrive` starts at `time.monotonic()` (skip the boot tick, mirroring `last_resource_check`).
- **alert:** construct `_partial_backlog_monitor` at `startup()` (like `_resource_monitor`) with a
  `get_partial_backlog_monitor()` accessor, and call `monitor.check()` in the SHARED
  `_subscriber_poll_loop` maintenance block at 60s, right next to `_resource_monitor.check()`
  (~L723). Read-only ⇒ safe off the latency-sensitive 1s applier thread.

---

## Locked design decisions (Diego, via AskUserQuestion)

1. **Attempt tracking:** filename `.rdN` marker (durable across restart, no DB/sidecar).
   *Rejected:* SQLite ledger in `applier_state.db`; in-memory dict.
2. **Post-cap behavior:** leave in `partial/` + alert (stays manually re-drivable, key unburned).
   *Rejected:* dead-letter.
3. **Alert threshold + delivery:** `count > 3`, HIGH → `jobflow_decisions`, edge-triggered like
   `RESOURCE_PRESSURE`. *Rejected:* N>5 same topic; N>10 → jobflow_firehose.

---

## Acceptance criteria

- [ ] `IntentApplier.redrive_partials()` re-drives eligible partials with a bumped `.rd{N+1}`
      marker, leaves not-yet-eligible and capped (`N>=5`) files in place, and is a pure method.
- [ ] `_move_to_partial()` resets mtime on partial landing (both partial branches).
- [ ] Marker parse: no-marker ⇒ 0, `.rd12` ⇒ 12.
- [ ] A re-driven intent re-applies end-to-end (partial → inbox → applied, key burned).
- [ ] Subscriber `redrive_partials()`: flag off ⇒ no-op; flag on ⇒ delegates to the applier.
- [ ] `PartialBacklogMonitor`: rising-edge fires when `count>3`; quiet while sustained; re-ping
      after cooldown; falling edge re-arms; payload shape matches.
- [ ] `EventType.TRACKER_PARTIAL_BACKLOG` exists, `type_string` stable, HIGH, `from_string`
      resolvable; routes to `jobflow_decisions`.
- [ ] Gateway wiring: monitor constructed at startup + getter; redrive 60s-gated on the applier
      thread; `monitor.check()` in the shared maintenance block.
- [ ] The re-drive flag defaults `"0"`; feature ships DISABLED until the :4100 hard gate is met.

## Test command

```
cd ~/.hermes/agent-src && PYTHONPATH=$(pwd) python -m pytest tests/intent_applier/ tests/events/ -q
```

## Constraints

- Do **NOT** auto-restart the gateway or :4100 (report PID + time; Diego restarts).
- Do **NOT** push (agent-src + `~/.hermes` subrepos are local-only). Commit to agent-src only when
  Diego approves; author = Diego; gitleaks pre-commit runs (PS 5.1: `git commit -F msgfile`).
- `partial/` is currently empty (the 07-13 storm's 13 were drained). Test with synthetic files
  under a `tmp_path` mailbox, never the live one.
