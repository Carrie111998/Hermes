# JobFlow reconciler: move the enabled-job guard from the prompt into code

**Date:** 2026-08-10
**Status:** design, approved
**Scope:** `cron/jobs.py`, `jobflow_dispatch/`, `events/subscribers/jobflow_dispatcher.py`,
`~/.hermes/profiles/main/scripts/jobflow_reconcile.py`, cron job `64711e6d8334` prompt

## The risk

Cron job `jobflow-reconcile` (`64711e6d8334`, schedule `30 0,6,12,18 * * *`) runs the
pre-run script `jobflow_reconcile.py`, which scans the mailbox for actionable messages
no worker holds and prints the stranded activity IDs with counts. An agent then reads
that stdout and is instructed, in natural language, to resolve each activity to exactly
one **enabled** cron job via `activity_policy` aliases, fail closed on zero-or-multiple,
and trigger it with `hermes cron run <job_id>`.

`hermes cron run` reaches `cron/jobs.py::trigger_job`, which calls `update_job` with
`{"enabled": True, "state": "scheduled", "paused_at": None, "paused_reason": None,
"next_run_at": NOW}`. Triggering a disabled job therefore **silently re-enables it**.
This is the exact hazard the event dispatcher was built to avoid; `cron/wake_channel.py`
says so in its module docstring: `trigger_job` "would silently revive a worker an
operator had deliberately disabled."

Today the only thing standing between a mis-resolving agent and a revived worker is a
sentence in a prompt. The guard lives in natural language, not in code, and nothing
flags the failure when it happens.

## What already exists

Four findings from reading the code shaped this design.

1. **The resolution rule is already implemented and tested.**
   `resolve_job_id_for_activity()` in `events/subscribers/jobflow_dispatcher.py` performs
   exactly what the prompt asks the agent to do by hand: alias lookup, `job.get("enabled")`
   filter, and `len(matches) != 1 -> refuse to guess`. The agent is hand-executing tested
   code.

2. **A non-enabling activation path exists but is unreachable from the script.**
   `cron/wake_channel.py` plus `_collect_woken_jobs` in `cron/scheduler.py` drop wakes for
   disabled jobs ("Unlike trigger_job, a wake never re-enables"). That channel is a
   volatile in-process set. The pre-run script runs as a **subprocess** (`_run_job_script`),
   so it cannot reach it. A cross-process non-enabling trigger has to be new code.

3. **Guarding `trigger_job` itself has the wrong blast radius.**
   Its production callers are the CLI `hermes cron run` (`hermes_cli/cron.py`), the
   api_server trigger endpoint, and the web_server trigger endpoint — all
   operator-initiated, where "run this now even though it is paused" is plausibly the
   intent. The LLM `cronjob` tool does not call it at all (it goes through
   `_execute_job_now`). Changing the default there would fix one agent path by altering
   three human paths.

4. **The risk is latent, not live.** All four JobFlow activities map 1:1 to enabled jobs
   (`cron.jobflow.applier` -> `jobflow-applier`, `cron.jobflow.matcher` -> `jobflow-matcher`,
   `cron.jobflow.researcher` -> `jobflow-researcher`, `jobflow.tailor.generate` ->
   `jobflow-tailor`). The only two disabled jobs, `devflow-standup` and `devflow-bridge`,
   are not aliases of any JobFlow activity. This is preventive hardening with no cleanup
   attached, so the clean design is affordable.

## Decision

Take **(a) + (b) as one change**: the wrapper resolves *and* activates, through a new
non-enabling trigger. The agent leaves the actuator path entirely.

Rejected alternatives:

- **Wrapper emits verified job IDs; agent still triggers.** Removes mis-resolution but
  leaves a TOCTOU window of minutes between the scan and the trigger, and still routes the
  action through an enabling command. The guarantee stays probabilistic.
- **Verified IDs plus a `--no-revive` CLI flag.** Closes the TOCTOU window, but the flag
  itself is prompt-enforced. The guard returns to natural language, one level down.
- **Guard inside `trigger_job`.** Broadest blast radius, for the reasons in finding 3.

## Design

### 1. `cron/jobs.py::request_run()` — durable, cross-process, non-enabling

```python
def request_run(job_id: str, *, caller: str, reason: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Schedule an ALREADY-ENABLED job for the next tick. Never enables."""
```

- Resolves by ID or name via `resolve_job_ref`; returns `None` for an unknown reference and
  propagates `AmbiguousJobReference` for an ambiguous name, matching `trigger_job`.
- **`if not job.get("enabled"): log and return None`** — refuses without writing anything.
- Otherwise `update_job(job["id"], {"next_run_at": NOW})` — **exactly one field**.
- Emits `cron_triggered` through the existing `emit_cron_triggered_safe`, so reconciler
  activations remain attributable. This improves on today, where they are recorded as
  `hermes_cli:cron_run` with no reason.
- `caller` is keyword-only and required, raising `ValueError` on an empty value. This is a
  new API with no back-compat burden, so it takes `wake_channel.request_wake`'s stricter
  contract rather than `trigger_job`'s warn-and-continue one.
- Exported from `cron/__init__.py`.

**Why one field is sufficient.** The due scan gates on `job.get("enabled", True)` and never
reads `state`. `pause_job` sets `enabled: False` alongside `state: "paused"`, so the single
`enabled` check also covers paused jobs. Writing only `next_run_at` means `request_run`
cannot alter any operator-visible lifecycle field — an invariant worth asserting directly
in tests.

`trigger_job` is not modified.

### 2. One source of truth for resolution

Move `resolve_job_id_for_activity` out of `events/subscribers/jobflow_dispatcher.py` into
`jobflow_dispatch/activate.py`, re-exported from the dispatcher so its existing import
keeps working.

Two reasons. The dispatcher's docstring already insists that routing table, claim ledger,
and availability predicate have one source of truth because "two sources of truth would
eventually disagree" — resolution now has two consumers and belongs in that set. And the
wrapper should not drag the events subscriber and bus stack into a cron subprocess.

### 3. `jobflow_dispatch/activate.py::activate_pending()`

Takes the `Activation` list from `scan_actionable` and returns a report. Dependencies
(`resolve`, `request_run`) are injectable so the orchestration is unit-testable without a
cron store.

Sequence: dedupe activity IDs (keeping counts for the report) -> resolve each, fail closed
on zero-or-multiple -> dedupe job IDs so **each distinct job is activated at most once per
run** -> call `request_run` per job.

Three non-success outcomes, counted separately because they mean different things:

| Outcome | Meaning |
|---|---|
| `unresolved` | 0 or >1 enabled jobs matched the activity's aliases |
| `refused` | Resolution succeeded but `request_run` returned `None` — the job was disabled **between the scan and the activation**. This is the TOCTOU case, now failing closed in code. |
| `errors` | An exception on one activity, isolated so the remaining activities still activate |

Activations are attributed as `caller="cron:jobflow-reconcile"`, `reason="reconcile"` —
matching the `reason` already used by `scan_actionable` when it builds an `Activation`.

### 4. `render_report()` owns the stdout contract

In the same module, so the "must be the last non-empty line" wake-gate invariant is
guarded by unit tests instead of by careful reading of the script.

- Gate is `{"wakeAgent": false}` **iff** `unresolved == 0 and refused == 0 and errors == 0`.
  A run that activated three jobs cleanly is a silent run.
- Otherwise `{"wakeAgent": true}`, carrying `errors: N` on scan failure as today.
- Message bodies still never reach stdout — activity IDs, job IDs, and counts only.

**Where the record actually lives.** Neither stream survives a silent run, and the design
depends on knowing that:

- on the `wakeAgent: false` branch the scheduler replaces the script's stdout with a fixed
  `silent_doc` (`cron/scheduler.py`, agent path), so the summary line is **not** in the run
  document;
- on exit 0 `_run_job_script` returns stdout only — **stderr is discarded entirely**, which
  is already true of today's `"[jobflow-reconcile] nothing pending"` line.

The durable record of an activation is therefore the **`cron_triggered` event** emitted by
`request_run`, carrying `caller="cron:jobflow-reconcile"`, the reason, and the previous and
new `next_run_at`. The audit-logger subscriber persists it to `~/.hermes/events/audit.jsonl`.
This is a better audit trail than a run doc — queryable, attributed, and independent of
which branch the gate took. It works from the script subprocess because `_get_event_bus()`
constructs an `EventBus` directly against the SQLite store rather than talking to the
gateway; the standalone CLI `hermes cron run` already emits this way today.

The summary line still goes to **stdout** above the gate, because it is what the agent
reads in the non-silent branch. Diagnostics and sanitized exception labels stay on
**stderr**. Neither is treated as the system of record.

### 5. The wrapper becomes thin

`~/.hermes/profiles/main/scripts/jobflow_reconcile.py` (tracked in the `~/.hermes` repo)
reduces to: scan -> `activate_pending` -> `print(render_report(...))`, keeping its existing
scan-failure branch. All logic worth testing lives in `agent-src`.

### 6. Prompt rewrite, via `hermes cron edit`

The agent becomes a diagnostician for a broken activity-to-job mapping, not an actuator:

- explicitly forbidden from running `hermes cron run` or triggering anything;
- asked to report *why* an activity failed to resolve — zero versus multiple matches,
  disabled deliberately or by accident;
- existing prohibitions preserved verbatim: no mailbox reads or writes, no `jobs.json`
  edits, no changes to `HERMES_JOBFLOW_EVENT_DISPATCH`.

The job keeps `no_agent: false`. Because the gate is now false in the healthy case, a clean
reconcile costs no session and no model call; a session is spent only when the mapping is
actually broken.

## Fail-closed semantics

Preserved and doubled. Resolution refuses on zero-or-multiple matches, and `request_run`
independently refuses a disabled job even if resolution somehow returned one. Activating
the wrong worker stays worse than not activating one, because the next reconcile catches
the miss.

## Testing

The load-bearing regression: **`request_run` on a disabled job returns `None` and leaves
`jobs.json` byte-identical.**

`tests/cron/test_jobs.py`:

- enabled job -> `next_run_at` advances, `enabled` and `state` untouched
- disabled job -> returns `None`, store byte-identical
- paused job -> returns `None`
- unknown reference -> `None`; ambiguous name -> `AmbiguousJobReference`
- emits `cron_triggered` carrying the caller; a bus failure is swallowed and the write
  still lands
- **`trigger_job` still sets `enabled: True`** — pinned deliberately, so a later refactor
  cannot silently converge the two functions and quietly remove the operator's revive

`tests/jobflow_dispatch/test_activate.py`:

- resolution fails closed on 0 and on 2 matching enabled jobs
- a disabled job is never a match even when its alias is the only one
- many activations mapping to one job produce exactly one `request_run` call
- TOCTOU refusal is counted as `refused`, not `activated`
- one raising activity does not prevent the others from activating
- report rendering: counts, and the wake gate as the last non-empty line in every branch

## Non-changes

- **Schedule `30 0,6,12,18 * * *` is untouched.** The 30-minute offset is load-bearing: it
  makes the reconciler trail the 6-hourly worker rather than fire alongside it. Aligning
  them recreates the cadence trap where the reconciler reports work as stranded that the
  worker simply had not drained yet. See agent memory
  `jobflow-reconciler-offset-from-tailor-cycle`.
- **`cron/jobs.json` is never hand-edited.** The prompt change goes through
  `hermes cron edit`; hand-edits can leave records with no `id`.
- **The activation ledger stays read-only on this path.** The reconciler claims nothing, so
  it remains the safety net rather than becoming a second claimant.
- **`trigger_job` keeps its current behavior** for the CLI, api_server, and web_server
  operator paths.

## The `HERMES_JOBFLOW_EVENT_DISPATCH` question

Whether the reconciler should stay agent-gated once dispatch flips from `shadow` to `on`
resolves itself under this design: **it needs no prompt change.**

With the actuator in code, the guard and the activation path are identical whether events
are claiming and waking directly or not. `on` only reduces how often anything is pending,
which lowers an already near-zero agent cost — the gate is false in the healthy case
either way. The reconciler stays exactly as designed and simply finds less to do. The
prompt is coupled to the *failure* mode (a broken activity-to-job mapping), which the
dispatch mode does not affect.

## Accepted risks

- **Import cost.** The wrapper now imports `cron.jobs` in a subprocess, parsing a ~130 KB
  `jobs.json`. Four runs per day; worth measuring once, not worth designing around.
- **Cross-process `jobs.json` writes from the script.** This is the status quo, not new —
  the agent's `hermes cron run` does exactly this today, once per job. `_jobs_lock()` is a
  cross-process advisory flock with a 30s timeout and a logged degraded fallback. The new
  design performs strictly fewer writes from strictly fewer processes.
- **Concurrent scheduler activity.** `mark_job_run` and `get_due_and_skipped_jobs` re-read
  under the same lock before writing, so a `next_run_at` written by the script is not
  clobbered by an in-flight tick.
- **A permanently stuck message produces an invisible wake loop.** The gate is
  `{"wakeAgent": false}` whenever every resolved activity activates cleanly, and on that
  branch the scheduler replaces the run's stdout with a fixed `silent_doc` — so a
  successful activation leaves no run document, no delivered message, and no agent
  session. If a message is permanently stuck in an inbox (a malformed packet, or a bug in
  the worker's own drain path), the worker never consumes it, so the same activity is
  still actionable on the next scan. Each reconcile resolves it, calls `request_run`
  successfully, and closes the gate again — the reconciler silently wakes that worker at
  00:30, 06:30, 12:30, and 18:30 forever, burning a worker run every six hours, with the
  only trace being hourly-batched `cron_triggered` TRACE rows in
  `~/.hermes/events/audit.jsonl`. Under the old prompt-driven design, any pending work
  opened the gate, so this same condition produced a visible agent report every cycle
  instead of silence. Closing this in code would require the reconciler to keep per-run
  state across passes (e.g. "this activity resolved cleanly N times in a row with no
  progress") — state this design deliberately keeps the reconciler without, since it is
  meant to be a stateless safety net, not a second claimant with its own ledger. The
  accepted mitigation is operational, not code: watch `audit.jsonl` for the same job_id
  recurring under `caller="cron:jobflow-reconcile"` across consecutive reconcile windows
  (see the plan's Verification checklist).
