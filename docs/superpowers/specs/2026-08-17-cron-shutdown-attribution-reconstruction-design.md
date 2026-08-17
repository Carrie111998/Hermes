# Cron shutdown attribution: reconstruct from the bus at startup

**Date:** 2026-08-17
**Component:** `events/subscribers/cron_stale_monitor.py`, `events/bus.py`
**Status:** design approved

## The gap

`CronStaleMonitor` reports a cron run that a gateway shutdown cut short as
`CRON_STALE` with `scope="gateway_stopped"`, `Priority.NORMAL`. It learns which
runs were in flight from the `GATEWAY_STOPPED` payload's
`inflight_cron_correlation_ids` — a list of `cron_started` event ids stamped by
`gateway/run.py`'s `_stop_impl_body` via `cron/inflight.py`.

That snapshot is taken EARLY in teardown and must stay there: a teardown
force-killed past `gateway/status.py`'s `_TASKKILL_TIMEOUT_S` (30) would
otherwise emit no `GATEWAY_STOPPED` at all. Because it is early, a listed run
can still FINISH during teardown, so "in flight when the stop began" is a
weaker claim than "killed by the stop". `2dc4bdf27c` fixed the resulting false
reports by STAGING the report in `_resolve_gateway_stopped` and flushing it in
`shutdown()`; `2a4ece2c07` then corrected `age_seconds` to be measured at
staging time rather than at the flush.

**The flush only happens on a graceful teardown.** If the process is
force-killed — `taskkill /F` past `_TASKKILL_TIMEOUT_S`, or the shutdown
watchdog's `exit_code=1` (leash = `agent.restart_drain_timeout` + 60s) —
neither `_drain_subscribers_for_shutdown()` nor `SubscriberRegistry.
shutdown_all()` runs, and the staged reports die with the process. Nothing is
recorded at all, for runs that genuinely WERE killed. `2dc4bdf27c` accepted
this deliberately ("a wrong record is worse than a missing one at
Priority.NORMAL") and named this work as the real fix.

Scale: the 2026-08-12 census found six shutdowns started that day and three
completed — roughly half of shutdowns on this box, not an edge case.

### Why the successor cannot do it today

`CronStaleMonitor._started_event_ids` (correlation_id -> job_id) is per-process
in-memory state, built only from `CRON_STARTED` events as they are handled.
`BaseSubscriber` seeds its cursor with `INSERT OR IGNORE`
(`events/subscribers/base.py`), so a restart PRESERVES the cursor and never
replays the `CRON_STARTED` rows that built that map. Verified in production
2026-08-17 04:12:03Z: the successor handled the predecessor's
`GATEWAY_STOPPED` and emitted nothing, because every correlation id missed an
empty map.

## Design

Every input is durable in `~/.hermes/events/event_bus.db`, so this is strictly
a query problem. The new gateway reconstructs the attribution from the bus at
startup.

Evaluated after the fact, this path has complete information and needs no
deferral and no race — it is strictly better than the in-process path, not just
a fallback.

### Entry point

`CronStaleMonitor.startup()` — a free hook today (the class has no override).
`SubscriberRegistry.startup_all()` calls it synchronously during
`events/gateway_integration.py`'s `startup()`, before the subscriber poll
thread starts. The whole pass is wrapped so a failure logs and never blocks
gateway boot.

### Algorithm

1. **Horizon.** `ATTRIBUTION_HORIZON_SECONDS = 86_400` (24h). Examine only
   `GATEWAY_STOPPED` events with `timestamp >= now - horizon`, via
   `bus.query(event_type=GATEWAY_STOPPED, since=...)`. That seeks the
   `idx_events_type_status_ts` prefix and returns a handful of rows — the live
   bus holds 28 `gateway_stopped` rows all-time.
2. **Report what the bound excluded.** A second count over
   `[now - HORIZON_REPORT_WINDOW (7d), now - horizon)` logs how many shutdowns
   were skipped for being older than the horizon, rather than truncating
   silently.
3. **Dedupe off the bus — no new persistent state.**
   `bus.query(event_type=CRON_STALE, since=...)` over the same horizon builds
   the set of `(gateway_stopped_event_id, cron_started_event_id)` pairs already
   reported. Both keys are already in the `CRON_STALE` payload, so the check is
   a bus query rather than new state, and a record emitted by the predecessor's
   graceful flush is seen here. A `CRON_STALE` is always emitted after the
   `GATEWAY_STOPPED` it attributes, so the same horizon covers both.

   The horizon plus this dedupe set make the pass idempotent: no watermark is
   needed, and re-running it on every boot costs only the queries.
4. **Resolve each correlation id.** `cron_started` by `event_id` — a primary-key
   seek. Missing (retention-evicted, or an id from a foreign bus) → log at debug
   and skip.
5. **Decide the outcome.** One query per id: the FIRST event carrying that
   `job_id` with type in `{cron_started, cron_completed, cron_failed}` in the
   rowid window `(cron_started_rowid, head]`, where `head` is a
   `bus.head_rowid()` snapshot taken at the start of the pass.
   - a terminal event → the run landed (possibly during teardown) → no report;
   - a NEWER `cron_started` → this run never terminated and the re-run is not
     its completion → report;
   - nothing → report.

   The "first event wins" rule is what keeps a later re-run of the same job from
   being mistaken for the killed run's completion — which matters when a boot
   between the shutdown and this pass already re-ran the job.
6. **Emit.** `CRON_STALE`, `Priority.NORMAL`, `scope="gateway_stopped"`, with
   the existing payload keys: `job_id`, `job_name`, `scope`, `exit_reason`,
   `age_seconds`, `gateway_stopped_event_id`, `cron_started_event_id`.
   `age_seconds = gateway_stopped.timestamp - cron_started.timestamp`, clamped
   at zero — "how far into the run did the shutdown land", never measured
   against now (`2a4ece2c07`).

### The cursor is not rewound

This is a targeted `query()` / rowid read. `subscribe()` is never called with a
moved cursor, no handler re-fires, and the ADR-0018 scanner-flood mitigation
that the `INSERT OR IGNORE` seed exists to protect is untouched. This is the
difference from `AuditLogger`, which needs `SEED_CURSOR_AT_CONSTRUCTION = False`
because it genuinely consumes from 0; `CronStaleMonitor` keeps the default.

### The in-process staging stays, as a fast path

`_resolve_gateway_stopped` / `_flush_pending_shutdown` / `shutdown()` are kept
unchanged. They cover one case the successor cannot: a graceful `hermes stop`
that is never followed by a restart, where no successor ever boots to
reconstruct anything. On a graceful stop followed by a restart, the two paths
compose to exactly one record because the successor's dedupe sees the
predecessor's row. Nothing from `2dc4bdf27c` or `2a4ece2c07` is deleted, and
their tests keep passing.

### Two additions to `EventBus`

- `event_with_rowid(event_id) -> Optional[Tuple[int, Event]]` — primary-key
  seek returning the row's rowid alongside the event, so step 5 has a window
  floor.
- `first_event_for_job(job_id, event_types, after_rowid, through_rowid) ->
  Optional[Event]` — earliest event of the given types carrying that payload
  `job_id` inside the rowid window.

`first_event_for_job` reads `json_extract(payload, '$.job_id')` and NOT the
`events.job_id` COLUMN, because no producer populates that column: measured
2026-08-17 on the live bus, 0 of 25,449 `cron_started` rows and 0 of 24,964
`cron_completed` rows set it. The rowid bounds make the scan an INTEGER PRIMARY
KEY seek; a full day of traffic (~5,865 events) measures ~22 ms.

## Error handling

- The whole pass is wrapped: any exception is logged and boot continues.
  `startup_all()` already catches per subscriber; the internal guard exists so a
  failure on one shutdown record does not discard the ones already emitted.
- An unparseable `cron_started` timestamp, a non-list
  `inflight_cron_correlation_ids`, and an unresolvable correlation id each skip
  that entry rather than aborting the pass.
- A failed `bus.emit` is logged per record, matching `_flush_pending_shutdown`.
- One summary log line reports shutdowns examined, records emitted, entries
  already reported, runs found finished, ids unresolvable, and the count
  excluded by the horizon.

## Testing

TDD, RED first. New class `TestStartupShutdownReconstruction` in
`tests/events/subscribers/test_cron_stale_monitor.py`, all on a `tmp_path`
bus — synthetic events are NEVER injected into the live
`~/.hermes/events/event_bus.db`.

1. A successor with empty in-memory state emits the record for a killed run —
   the gap itself.
2. No double-report when the predecessor already emitted the record.
3. A run whose `cron_completed` landed after the `GATEWAY_STOPPED` (during
   teardown) is not reported.
4. `age_seconds` is `gateway_stopped - cron_started`, not measured against now.
5. A `GATEWAY_STOPPED` older than the horizon is not examined, and the excluded
   count is logged.
6. An unresolvable correlation id is silent.
7. Only ids the payload lists are attributed; an unrelated open run is
   untouched.
8. A later `cron_started` for the same job does not count as the run finishing.
9. The subscriber cursor is unchanged by `startup()`, and no handler fires.
10. A raising bus does not break `startup()`.

Existing `TestGatewayStoppedResolution` and `TestShutdownAttributionTiming`
must stay green; `tests/events/test_shutdown_drain.py` covers the composition
of the graceful path with the new one.

## Verification horizon

This path runs in the SUCCESSOR, so it is exercised by the first restart that
boots the new code — unlike the drain (`bc07363000`) and the deferral, it does
not need two restarts. But it can only produce a record if a hard-killed
teardown actually occurs, which is not something to force. Verified by test;
production confirmation is opportunistic.
