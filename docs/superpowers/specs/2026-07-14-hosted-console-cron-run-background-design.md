# Hosted Console `cron run` — immediate background fire

**Date:** 2026-07-14
**Component:** `hermes_cli/console_engine.py` (`_cron_run`), tests in
`tests/hermes_cli/test_console_engine.py`
**Status:** Implemented + tested (TDD). Local commit to agent-src `main`.
**Related:** commit `6a5dba5d1` (align TUI `cron run` with CLI/LLM — execute now
in local REPL), `1416d1ffa` (attribute console `cron run` to
`tui:console_engine`), `a6b9597d5` (bounded console worker pool). Hosted transport:
`hermes_cli/web_server.py` `console_ws` (`_CONSOLE_EXECUTOR_MAX_WORKERS=4`,
`_CONSOLE_COMMAND_TIMEOUT_SECONDS=60.0`).

## Problem

`_cron_run` is context-aware. In `local` context (standalone `hermes console`
REPL) it executes the job immediately via
`tools/cronjob_tools.py:_execute_job_now(caller="tui:console_engine")` — parity
with `hermes cron run` (CLI) and the `cronjob(action='run')` tool.

In `hosted` context (the dashboard web-console served by `console_ws`) it keeps
schedule-for-next-tick via `cron.jobs.trigger_job`, because that surface runs
every command in a bounded 4-worker `ThreadPoolExecutor` under a 60s
`asyncio.wait_for` timeout. A synchronous agent run (>60s is common) would be
mis-reported as timed-out while the un-cancellable worker keeps running, and
would occupy 1 of only 4 shared console threads → starvation.

The gap: a dashboard user who types `cron run <job>` has **no** way to get
immediate execution. They get "it'll fire on the next tick" — and if no gateway
ticker is active, it may not fire at all (the #41037 case that motivated the
local change).

## Goal / Acceptance

Hosted `cron run <job>` fires the job **immediately** without blocking the
console thread or tripping the 60s timeout:

1. Dispatch the **unchanged** `_execute_job_now(job, caller="tui:console_engine")`
   on a dedicated background executor — **never** on the 4-worker console pool.
2. Return a "started, running in the background" acknowledgement immediately.
3. Surface completion via the existing event stream the dashboard already
   consumes: `CRON_TRIGGERED` (emitted by `_execute_job_now` on a won claim) +
   the completion event (`run_one_job` → `emitter.on_job_completed`).
4. Preserve `caller="tui:console_engine"` attribution.
5. Preserve the single-`CRON_TRIGGERED`-emit contract (exactly one on a won
   claim; zero on a lost claim).
6. Do **not** reintroduce a synchronous run on the hosted 4-thread pool.

**Decision (approved 2026-07-14):** hosted `cron run <job>` *replaces*
schedule-for-next-tick with the background fire — "run" means run now on every
surface. Defer-to-tick was only ever a timeout workaround, not a wanted feature.

## Core insight — reuse `_execute_job_now` verbatim

`_execute_job_now` is a complete, self-contained fire:

- Claims at-most-once via `claim_job_for_fire`. On a won claim it emits **exactly
  one** `CRON_TRIGGERED` (via `emit_cron_triggered_safe`, with caller/reason
  attribution) *before* the run, then delegates to `run_one_job`. On a lost claim
  it no-ops and emits nothing.
- `run_one_job` records `last_status` and emits the completion event via
  `emitter.on_job_completed` — the `cron_completed` the dashboard activity feed
  already consumes.

So attribution, single-emit, and completion-surfacing are all **inherited** by
calling the unchanged function. The only new work is *where it runs* and *how to
test it deterministically*.

## Approaches considered

- **A (chosen) — dedicated background pool owned by `console_engine`.**
  `_cron_run` resolves the job on the console thread (fast fail on
  not-found/ambiguous), then `submit(_execute_job_now, …)` to a module-level
  lazy-singleton `ThreadPoolExecutor` and returns the ack. Self-contained, keeps
  the handler a plain synchronous function, and is directly testable in
  `test_console_engine.py` (the test owns the executor via a monkeypatchable
  getter and joins with `shutdown(wait=True)`).
- **B — sentinel `ConsoleResult`; `web_server` owns the pool.** Rejected:
  invasive engine↔transport protocol, and the dispatch would live in
  `web_server`, so it could not be tested via the required `test_console_engine.py`
  path.
- **D — raw `threading.Thread(daemon=True)` per run.** Rejected: unbounded
  agent-run concurrency under click-spam. A bounded pool mirrors the existing
  `_console_executor` precedent and is cleaner to join in tests.

## Design (Approach A)

### `_cron_run` hosted branch

Replace the `trigger_job` block with:

1. `resolve_job_ref(args[0])` on the console thread — cheap file read, so
   `Job not found` / `AmbiguousJobReference` come back **immediately and
   synchronously** (same fast-fail UX as the local branch).
2. Dispatch `_execute_job_now(job, caller="tui:console_engine")` to the
   background pool (`submit` never blocks the caller, even when all workers are
   busy — excess work queues).
3. Return immediately:

   > `Started job: <name> (<id>) — running now in the background; watch the
   > activity feed for the result.`

The **local branch is untouched.**

### Background executor

Module-level lazy singleton in `console_engine.py`, mirroring `web_server`'s
`_console_executor`:

```
_CONSOLE_RUN_EXECUTOR_MAX_WORKERS = 2
_get_console_run_executor() -> ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="hermes-console-cronrun")
# atexit: shutdown(wait=False, cancel_futures=True)
```

`max_workers=2` bounds *concurrent agent runs* from manual clicks (desirable);
it does **not** bound the ack — `submit` queues excess, so the console thread
always returns instantly. A done-callback logs the background outcome
(claimed / success / error) so an outcome is never silently swallowed (it is
also in the event stream; the log line aids debugging).

### Two deliberate decisions

1. **Propagate the caller's context to the background fire
   (`contextvars.copy_context`).** *Revised during implementation* — the original
   plan was to run the fire "like the gateway ticker" with no profile context.
   That is wrong for the hosted multi-profile case: the hosted WS wraps each
   console command in `_profile_scope`, which for a non-default profile sets a
   **context-local** (`ContextVar`) HERMES_HOME override
   (`hermes_constants.set_hermes_home_override`). `cron.jobs` resolves its store
   dynamically through that override (commit `3b91389a1`), and `run_one_job`
   builds the job's secret scope from `_get_hermes_home()`. A
   `ThreadPoolExecutor` worker starts with an **empty** context, so a naive
   `submit(_execute_job_now, …)` would claim/run/record against the WRONG
   profile's store — regressing exactly the per-profile fire correctness
   `3b91389a1` established for `_fire_cron_job_for_profile`. The dispatch
   therefore captures `contextvars.copy_context()` on the console thread and runs
   the fire via `ctx.run(…)`, so it inherits the profile override (and any other
   request context) — behaving as if it ran inline, only off-thread. The job
   reference is still resolved on the console thread (fast-fail), in the same
   context.
2. **Optimistic "started" ack.** If the scheduler wins the claim first,
   `_execute_job_now` no-ops after we have already said "started." The user's
   intent (fire now) is still met by that concurrent tick, the activity feed
   shows the true trigger/completion, and the done-callback logs the skip.
   Splitting the claim onto the console thread to report "skipped" synchronously
   would fracture `_execute_job_now`'s atomic claim→emit→run packaging — not
   worth it.

## Testing

`tests/hermes_cli/test_console_engine.py`, using the existing `_tmp_cron_store`
fixture and stubbing `cron.scheduler.run_one_job` (the real agent boundary).
Hosted-run tests monkeypatch `_get_console_run_executor` to a **test-owned**
`ThreadPoolExecutor`, then join deterministically with `shutdown(wait=True)` —
no sleeps.

- **Rewrite** `test_cron_run_schedules_for_next_tick_in_hosted_context` →
  `test_cron_run_executes_in_background_in_hosted_context`: asserts the "Started"
  ack, that `run_one_job` actually fired, and exactly one `CRON_TRIGGERED` with
  `caller="tui:console_engine"`. (Its premise — schedule-for-next-tick — is the
  behavior being deliberately replaced.)
- **Non-blocking proof:** stub `run_one_job` to block on a `threading.Event`;
  assert `execute()` returns the ack *before* the event is set (if it blocked,
  the test deadlocks → fails), then release + `shutdown(wait=True)`.
- **Fast-fail on the console thread:** `cron run nope` (hosted) → `Job not found`
  immediately, with **no** background dispatch.
- **Optimistic-ack regression pin:** with the claim forced lost, hosted `cron run`
  still returns the "Started" ack and `run_one_job` is never called.
- **Profile propagation:** with a context-local HERMES_HOME override set
  (`set_hermes_home_override` to a distinct profile home) and the job created in
  that profile's store, the *background* thread must resolve that same profile
  home (asserted via `cron.jobs._get_hermes_home()` inside the stubbed
  `run_one_job`). Fails without `copy_context` — the worker falls back to the
  process default and the claim no-ops.
- Local-context tests unchanged.

## Out of scope

- No change to the local branch, `_execute_job_now`, `run_one_job`, or
  `web_server`'s console loop.
- No new user-facing flag/subcommand (the approved decision is to *replace* the
  hosted `cron run` semantics, not add a variant).
- No gateway restart / no push (local commit to agent-src `main`, author Diego).
