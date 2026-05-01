# Cron Aborted vs Cron Failed — wallclock-timeout trade-off

**Date:** 2026-04-30
**Status:** decided — wallclock-timeout stays on `cron_failed` path
**Companion plan:** [`docs/superpowers/plans/2026-04-30-cron-aborted-on-shutdown.md`](../plans/2026-04-30-cron-aborted-on-shutdown.md)

## Context

Guard #1 (shipped 2026-04-30) introduced `EventType.CRON_ABORTED` plus `cron/scheduler.py::flush_inflight_aborts(reason)` to drain still-tracked in-flight crons on gateway shutdown so every `cron_started` in `audit.jsonl` has a paired terminal event.

The helper accepts two `reason` values:

* `"gateway_shutdown"` — wired into `events/gateway_integration.py::shutdown()`.
* `"wallclock_timeout"` — accepted by the helper but with no production caller.

This addendum captures the decision NOT to wire the second reason into the wallclock-timeout branch at `cron/scheduler.py:1546-1573`, and the reasoning that future contributors should follow when revisiting it.

## Two options considered

### Option 1 — replace `cron_failed` with `cron_aborted` for wallclock

`_process_job` would, on detecting a wallclock-shaped failure, call `flush_inflight_aborts("wallclock_timeout")` (or a single-job variant) and skip the existing `on_job_completed(success=False)`.

**Pros:**

* Stronger semantic distinction: only `cron_completed` would be a "normal terminal," everything else (failed, aborted) carries a categorical reason.
* Audit-log grep symmetry: `did_not_finish = cron_failed ∪ cron_aborted`.

**Cons (the disqualifying ones):**

1. `cron_aborted` deliberately skips `FailureClusterDetector.record()` (see `events/producers/cron_emitter.py::on_job_aborted` and the rationale in [`2026-04-30-cron-aborted-on-shutdown.md`](../plans/2026-04-30-cron-aborted-on-shutdown.md): "gateway-fault, not agent-fault"). Wallclock-timeout IS agent-attributable — the agent took longer than `HERMES_CRON_HARD_TIMEOUT` and the scheduler terminated it. Three consecutive wallclock kills from the same source SHOULD trip `agent_failure_cluster` so the operator gets paged. Re-routing wallclock to `cron_aborted` would silence that signal.
2. The `consecutive_errors` increment lives on the `cron_failed` path (`mark_job_run` + `on_job_completed`'s consecutive-threshold branch). Re-routing wallclock to `cron_aborted` would skip the `cron_failed_consecutive` (Priority CRITICAL) escalation and the corresponding `WhatsAppEscalator` URGENT-tier message.
3. `digest_composer` counts only `CRON_FAILED` / `CRON_FAILED_CONSECUTIVE` / `AGENT_ERROR` toward the daily-digest "errors" group. Wallclock failures would fall out of the digest entirely.
4. `control_center.storage.ACTIVITY_EVENT_TYPES` and `control_center.ws.py` filter the dashboard activity feed and the WS push set to a hard-coded list that includes `cron_failed` but not `cron_aborted`. Adding `cron_aborted` to those lists is a separate non-trivial decision (it changes which alerts the dashboard escalates), and Option 1 cannot be considered "minimal" without it.

### Option 2 — emit `cron_aborted` ALONGSIDE `cron_failed` for wallclock

`_process_job` would call BOTH `on_job_completed(success=False)` AND a single-job `cron_aborted` emission (e.g. `flush_inflight_aborts_for_job(job_id, "wallclock_timeout")`).

**Pros:**

* Preserves all existing observability signals (cluster detection, consecutive escalation, WhatsApp URGENT, digest, dashboard).
* Adds the `cron_aborted` audit-log row for tooling that wants the symmetry.

**Cons:**

1. **Notification double-fire.** `TelegramNotifier::TOPIC_ROUTING` routes both `cron_failed` and `cron_aborted` to the `watchdog_alerts` topic. One wallclock incident → two Telegram messages for the same job.
2. Three events per fire (`cron_started` + `cron_aborted` + `cron_failed`) inflates `audit.jsonl` and increases EventBus poll load on every subscriber, for no functional gain over a single `cron_failed` row that already carries the same diagnostic content (`error` payload includes "wall-clock" classifier, `duration` field, etc.).
3. `CronStaleMonitor` clears `_open_jobs[job_id]` on whichever terminal lands first. Harmless, but creates cross-event ordering nondeterminism.

## Decision

**Neither option ships.** Wallclock-timeout failures continue to flow through `_process_job` → `on_job_completed(success=False)` → `EventType.CRON_FAILED`. The `flush_inflight_aborts` helper retains `"wallclock_timeout"` as an accepted `reason` value (forward-compat) but no production caller invokes it for that case.

The "every `cron_started` should be paired with a same-class terminal event" symmetry is **already satisfied today** — `cron_failed` IS a terminal event. The pairing requirement that motivated `cron_aborted` (gateway shutdown leaving truly orphaned starts) does not apply to wallclock-timeout, where the scheduler synchronously emits a terminal event before returning.

The operator signals built into the `cron_failed` path (cluster detection, consecutive escalation, WhatsApp URGENT, digest counting, dashboard activity feed) are exactly the right behavior for repeated wedges. Re-classifying those failures as a "non-agent-fault" abort would weaken on-call observability for a categorical purity that no consumer in the codebase currently needs.

### `cron_aborted` reason vocabulary, going forward

| Reason | Semantics | Status |
|---|---|---|
| `gateway_shutdown` | Gateway is exiting with futures still in flight; agents had no chance to finish. | Wired (Guard #1, 2026-04-30) |
| `wallclock_timeout` | Scheduler killed an agent that exceeded `HERMES_CRON_HARD_TIMEOUT`. | **Reserved as forward-compat; intentionally NOT wired.** Wallclock failures are agent-attributable and live on the `cron_failed` path. |

If a future scenario surfaces a third reason that IS genuinely external to the agent (e.g. host-level OOM that the watchdog catches before SIGKILL, a SIGTERM that bypasses the gateway shutdown handler, host-machine pause/resume), revisit this trade-off — but only if the new scenario fits the gateway-fault definition. Don't reuse `cron_aborted` for additional agent-attributable failure modes.

## Tests pinning the decision

* `tests/cron/test_scheduler.py::TestWallclockTimeoutEmitsCronFailedNotAborted::test_wallclock_failure_emits_cron_failed_via_on_job_completed` — drives `tick()` with a wallclock-shaped failure tuple from `run_job` and asserts `on_job_completed(success=False)` is called and `on_job_aborted` is NOT.
* `tests/events/test_cron_emitter.py::TestWallclockTimeoutFlowsThroughCronFailed::test_wallclock_error_emits_cron_failed_and_feeds_cluster_detector` — calls `on_job_completed(success=False, error=<wallclock-text>)` directly and asserts `CRON_FAILED` is emitted (not `CRON_ABORTED`) and `FailureClusterDetector.record()` is invoked.

A future contributor proposing Option 1 or Option 2 must update both tests AND this addendum to revoke or amend the decision.

## Out of scope for this trade-off

* `classify_failure_type` regex for wallclock errors. The current pattern `\b(timeout|timed[\s_-]out)\b` does not word-bound through `TimeoutError` (no boundary between `t` and `E`), so wallclock kills currently classify as `unknown` rather than `timeout`. They still cluster as same-type when 3 hit in a row, so `agent_failure_cluster` does fire. Tightening the classifier is a separate observability ticket if the operator wants finer-grained timeout vs. unknown attribution.
* `CronStaleMonitor` consumption of `CRON_ABORTED` (already wired by Guard #1).
* Any change to the `gateway_shutdown` wiring path (out of scope).
