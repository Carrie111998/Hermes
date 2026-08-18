---
title: "HERMES-MULTI-AGENT-ORCHESTRATION-001 ticket design"
status: IMPLEMENTATION_REVIEW_PASS
date: 2026-08-18
type: ticket-design
ticket: HERMES-MULTI-AGENT-ORCHESTRATION-001
target_repo: hermes-agent
base: 222396ffcf912037655f0ac8db914ea222d1002a
base_format: full 40-hex SHA-1; verified equal to origin/main
---

# HERMES-MULTI-AGENT-ORCHESTRATION-001

## Objective

Provide a narrow, auditable multi-agent worker path:

1. make the official Grok and Antigravity (`agy`) worker skills available;
2. add a quiet, metadata-only Telegram queue summary on a user systemd timer;
3. use the existing Kanban dashboard and terminal-state notifier without
   creating a second dispatcher, web UI, or notification path; and
4. record versioned worker token usage through the existing completion metadata
   handoff.

Kanban remains the sole source of task state. Telegram remains the human entry
and notification surface. Codex, Claude, Grok, and AGY remain optional worker
tools; none becomes a dispatcher target or a new core model provider.

## Current-source findings

- `hermes kanban create --skill` already persists repeatable worker skills.
- `hermes kanban complete --metadata` already stores arbitrary structured data
  on the closing `task_runs` row and exposes it to downstream workers.
- Telegram `/kanban create` already auto-subscribes the originating source to
  terminal events; no Telegram adapter change is needed.
- `hermes kanban stats --json` and `diagnostics --json` already provide the
  queue and distress inputs needed by a summary process.
- The built-in dashboard already renders task state, run history, and raw run
  metadata. It does not currently provide token rollups.
- `origin/main` contains official optional `grok` and `antigravity-cli`
  skills, but no repo-local `kanban-worker` skill. The worker guidance must be
  added under `optional-skills/devops/kanban-worker/` so the existing official
  optional-skill repair/sync path can install it at user scope.
- `kanban_db.py`, dispatcher lifecycle code, and the Telegram adapter are not
  required for this ticket.

## Proposed implementation

### 1. Official worker skills

Do not duplicate the Grok or AGY skill contents into the bundled `skills/`
tree. Keep their existing official optional sources and use the existing
official-optional repair path to install exact copies into the active user
skills tree. The runtime setup gate will verify `hermes skills list` and
byte-level source parity in a temporary/user-scoped home.

Add `optional-skills/devops/kanban-worker/SKILL.md` with the worker handoff
contract. It will require workers to:

- complete through `kanban_complete` / `hermes kanban complete`;
- put human-readable evidence in `summary` and machine-readable facts in
  `metadata`;
- report token usage only when the worker or external CLI actually provides it;
- never invent, estimate, or copy token counts from prompts; and
- validate the required integer/range/string fields locally before emitting a
  `token_usage` object; if validation fails, omit the object and complete with
  a non-sensitive validation note rather than emitting malformed metadata; and
- keep credentials, prompt bodies, message bodies, and absolute sensitive
  paths out of completion metadata.

The Grok OAuth login and the AGY authentication/smoke test remain user-owned
runtime gates. The optional `xai-provider` plugin is explicitly out of scope.

### 2. Versioned token metadata

The worker skill will document this optional shape:

```json
{
  "token_usage": {
    "schema": "hermes.worker.v1",
    "provider": "grok",
    "model": "grok-build-0.1",
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "total_tokens": 0,
    "estimated_cost_usd": null,
    "source": "worker_reported"
  }
}
```

The token object is optional. If present, `schema`, `provider`, `model`,
`input_tokens`, `output_tokens`, `total_tokens`, and `source` are required;
cache fields and `estimated_cost_usd` are optional. Token fields must be
non-negative integers no larger than `10^12` per run. `estimated_cost_usd`,
when present, must be a finite non-negative number no larger than `10^9` per
run. The cost is included in a rollup only when it passes that validation.
`provider` and `model` strings must be non-empty, valid UTF-8, at most 128
Unicode characters and at most 512 UTF-8 bytes after Unicode NFKC
normalization, and contain no control characters (including `\\u0000`, CR, and
LF). If any required field fails validation, the entire `token_usage` record is
omitted; no partial numeric fields are accepted. An invalid provider/model
causes that usage record to be excluded from the rollup and increments a
derived `invalid_token_usage_count`; it does not block the rest of the summary
and the invalid string is never emitted.
Valid `estimated_cost_usd` values are summed in USD separately for each
provider/model pair under `token_rollup.by_provider_model`; no currency
conversion is attempted. An invalid or absent cost is omitted from the cost
sum, while valid token counts from the same record remain eligible.
The derived `invalid_token_usage_count` is included in the outbound summary's
`token_rollup` object so operators can see excluded records without seeing the
invalid values themselves. Before fingerprinting, each valid USD sum is
quantized to six decimal places using `Decimal` and represented as an integer
`estimated_cost_usd_micros`; the fingerprint never serializes a platform-
dependent float.
The consumer does not require `total_tokens == input_tokens + output_tokens` because
providers differ in cache/accounting semantics. Missing fields remain missing;
unknown schemas and malformed values are retained in the run metadata but are
excluded from rollups. No code changes are made to the dispatcher or database
schema.

For `hermes.worker.v1`, `source` is a closed enum with the sole value
`worker_reported`; a different provenance requires a future schema version.
For the derived anomaly check, the expected count is
`input_tokens + output_tokens + cache_read_tokens + cache_write_tokens`.
Missing optional cache fields are treated as zero only for this calculation;
they remain absent in stored metadata and are not synthesized into the
rollup. If `total_tokens` differs from that expected count by more than 10% or
1024 tokens (whichever is larger), the run is retained but the current summary
snapshot increments its aggregate `token_rollup.accounting_anomaly_count` once
for that run. The counter is recomputed per summary invocation and is not
cumulative or persisted. This derived counter is not written back to
`task_runs.metadata`, so it requires no schema or dispatcher change and is
never silently presented as an exact cost measurement.

An anomaly-marked record with otherwise valid provider/model and numeric fields
still contributes its token counts to `token_rollup` totals and is counted in
`accounting_anomaly_count`; a record rejected for schema, provider/model, or
numeric validation does not contribute counts.

### 3. Quiet Telegram summary

Add a small read-only summary runner under `scripts/` and user-systemd unit
templates under `scripts/systemd/`.

The runner will:

1. read the current board through existing `hermes kanban stats --json` and
   `diagnostics --json` surfaces, plus read-only `task_runs.metadata` for the
   token rollup;
2. apply an independent allowlist at the read boundary, retaining only queue
   counts, opaque task IDs, diagnostic severity/rule code, and validated token
   totals by provider/model. Raw task rows and raw run metadata are never
   copied into the summary, even if a worker violated the skill guidance;
   token history is bounded to non-archived tasks on the current board with
   `ended_at` in the most recent 90 days and at most 10,000 runs, ordered newest
   first. The payload includes `token_rollup.window_days`,
   `token_rollup.max_runs`, and `token_rollup.truncated` so a bounded result is
   never mistaken for an all-history total;
3. build a stable `hermes.kanban.summary.v1` payload, excluding timestamps from
   its comparison fingerprint, and canonicalize the fingerprint input as UTF-8
   JSON with sorted keys, compact separators, and no environment-dependent
   formatting;
4. send through the existing `hermes send --to <configured Telegram target>`
   only when the fingerprint changes; and
5. atomically update a profile-safe state file only after a successful send.

The first run sends one summary. An unchanged run is silent. A failed send does
not advance the state file, so the next timer invocation retries. The runner
makes at most one `hermes send` attempt per timer invocation, has no internal
retry loop, and exits without sending when the target is absent or malformed.
The runner derives an instance key as the first 32 lowercase hex characters of
`sha256(board + "\\0" + target)`. This gives a 128-bit key for the deliberately
low-cardinality set of configured summary instances; state and lock files are
scoped by this key, so different boards or targets do not intentionally
collide, and the raw target is never put in a filename or state file. The
runner takes a single-instance lock under the
private state directory; if
another invocation holds it, the later invocation exits without sending. The
on-DGX lock primitive is POSIX advisory `fcntl.flock(LOCK_EX | LOCK_NB)` on a
dedicated lock file; it is released on normal exit and process termination by
the OS. Platform-specific unit tests may skip the POSIX lock assertion when
the primitive is unavailable.
The systemd timer provides the only retry cadence (ten minutes), so a bad
configuration cannot create an unbounded burst of duplicate notifications.
With the installed ten-minute timer and no service restart policy, scheduled
failure attempts are bounded to at most six per hour; manual invocations are
operator actions outside this retry contract.
State files contain exactly this bounded shape and no other fields:

```json
{
  "schema": "hermes.kanban.summary.state.v1",
  "fingerprint": "<64 lowercase hex characters>",
  "last_success_at": 0
}
```

The serialized state is capped at 512 UTF-8 bytes, is created and rewritten
with mode `0600` (and a private parent directory where the runner owns it),
and never contains task titles, prompt text, message bodies, credentials, or
tokens. State writes use a same-directory temporary file, flush, and atomic
rename (`os.replace`); a failed write leaves the prior valid state intact.
`last_success_at` is an integer UTC Unix epoch in seconds.
If the state path is absent, the runner treats the invocation as first-run.
If the path exists but is unreadable, oversized, malformed, or fails the exact
schema/fingerprint validation, the runner fails closed: it does not send, does
not overwrite the state, and emits only a non-sensitive reason code. Recovery
requires an operator to repair or remove the state file; there is no internal
retry loop.
State persistence happens after send success, so a crash or write failure in
that narrow window may produce one duplicate on the next run. This is an
explicit at-least-once policy; exactly-once delivery is not claimed because
`hermes send` has no receiver-side idempotency contract.

The timer is ten minutes (`OnUnitActiveSec=10min`) with a bounded oneshot
service. Templates are inert until a user installs, configures, enables, and
starts them. The target and board are non-secret `config.yaml`/unit settings;
no new user-facing non-secret `HERMES_*` environment variable is introduced.

### 4. Existing dashboard and event notifications

Do not add a custom dashboard page. Verification will use the existing
authenticated `hermes dashboard` and confirm that a completed run's
`token_usage` metadata is visible in Run history. A token analytics page is a
separate follow-up only if this ticket's real metadata proves the built-in
surface insufficient.
The dashboard's existing raw run-metadata view is pre-existing behavior and
this ticket adds no new at-rest redaction control; the worker contract and
summary read-boundary allowlist remain the applicable new safeguards.
This accepted residual risk is explicit: a non-compliant worker could still
place sensitive data in the pre-existing dashboard-visible run metadata, which
is outside this ticket's new enforcement surface.
Record this exposure as a follow-up review trigger if worker-reported metadata
contains any discovered credential, prompt/message body, or other sensitive
content (immediate trigger), or if invalid token usage exceeds 10% of scanned
runs for three consecutive summaries; this ticket does not permanently waive
that review.

Do not add a second completion notifier. The existing Telegram `/kanban create`
auto-subscription and `hermes kanban notify-subscribe` remain the sole
terminal-event notification mechanisms.

## Explicit non-goals

- no dispatcher, `kanban_db.py`, task lifecycle, or schema changes;
- no Telegram adapter or polling changes;
- no new model-provider integration or `xai-provider` enablement;
- no custom web/dashboard UI;
- no automatic Grok OAuth, AGY login, or credential changes;
- no Telegram send, timer enablement, DGX deployment, or service restart as
  part of repository implementation tests;
- no investigation of the separately reported fake `<system-reminder>`
  injection in this ticket.

## Acceptance gates

1. **Identity/source gate:** repository, remote, branch/HEAD, GitHub `main`,
   handover, roadmap, and clean isolated worktree match the ticket base.
2. **Design review gate:** exactly one authenticated Claude reviewer and one
   authenticated AGY reviewer inspect the same metadata-only design packet and
   return `PASS`, `REVISE`, or `BLOCKED`. `PASS/PASS` closes the gate. Any
   `REVISE` requires integrating the concrete findings, producing one new
   packet, and rerunning both reviewers on that same packet, for at most three
   correction rounds. If the third round is not `PASS/PASS`, the gate is
   `BLOCKED` and escalates to human direction. If either reviewer returns
   `BLOCKED`, the gate is `BLOCKED` and escalates to human direction. An
   unresolved disagreement after reconciliation is also `BLOCKED`; no third
   automated reviewer is substituted.
3. **Worker skill gate:** optional skill install/repair is exact and discoverable;
   Grok/AGY source parity is proven with SHA-256 hashes of the complete skill
   directory (relative file names plus bytes), and the worker metadata contract
   validator/consumer logic is tested without exposing credentials,
   prompt/message bodies, or local absolute paths. This does not technically
   enforce runtime worker adherence to the skill text; that remains a
   user-owned operational responsibility.
4. **Summary unit gate:** temporary-home tests prove JSON parsing, metadata-only
   formatting, valid token aggregation, required-field/type/range validation,
   cost validation, malformed-schema exclusion, read-boundary allowlisting,
   provider/model string bounds, invalid-usage exclusion/counting, anomaly
   counting and anomalous-valid-record inclusion, bounded-window/truncation
   behavior, per-provider/model USD cost sums, exact state shape/size, `0600`
   fixed micro-USD cost canonicalization, board/target instance-key isolation,
   state permissions, single-instance lock behavior including graceful skip
   when the lock is held by another invocation,
   canonical deterministic fingerprinting, missing-state first-send behavior,
   corrupt-state fail-closed behavior (including malformed JSON and a
   state file over 512 UTF-8 bytes, asserting no send and unchanged prior
   state), silent
   unchanged behavior, zero-valid-runs empty-rollup behavior, atomic state
   update, missing-target fail-closed behavior,
   one-attempt send failure behavior, and retry-after-send-failure behavior.
   Gate 4 consumes Gate 3's parity result and does not duplicate ownership of
   the source-parity check.
5. **Dashboard gate:** existing dashboard/API tests and a bounded authenticated
   manual check show raw completion metadata in run history; no custom page is
   introduced.
6. **Implementation review gate:** Claude and AGY review the identical final
   correction set under the same PASS/REVISE/BLOCKED reconciliation rule;
   unresolved findings block merge.
7. **CI/commit/push/merge gate:** latest-head required CI passes before merge.
8. **Runtime setup gate:** after explicit authorization, user-owned Grok login,
   AGY smoke, optional-skill installation, and timer configuration are verified
   separately from repository CI.
9. **DGX/Telegram gate:** only after explicit deployment authorization, verify
   hostname/user, release identity, service health, timer state, summary send
   audit, and direct Telegram user-visible receipt as separate evidence. A
   running service, empty polling, or `hermes send` process alone does not close
   user-visible delivery.

## Metadata-only review packet

Reviewers receive only:

- ticket id and objective;
- the current `origin/main` base SHA;
- the existing-capability findings above;
- the proposed file classes and invariants;
- the token schema and aggregation rules;
- the summary dedupe and failure semantics;
- the explicit non-goals and acceptance gates; and
- result vocabulary `PASS`, `REVISE`, `BLOCKED`.

They do not receive source text, task titles, prompt/message bodies, generated
evidence, credentials, tokens, or absolute sensitive paths.

## Review record

Design review PASS. Final metadata-only packet SHA-256:
`FF86753932BCCA40E21CB294C342B075B154CD72888B8D9D1BED343E3ECCC59A`.
One authenticated Claude reviewer and one authenticated AGY reviewer each
reviewed that exact packet and returned `PASS` with `CORRECTIONS: NONE`.
This plan still does not authorize Telegram state mutation, timer enablement,
DGX restart/deployment, or user-owned OAuth.

## Implementation record

Implementation is complete in the isolated worktree, pending the separate
implementation-review gate. The correction set is limited to:

- `hermes_cli/kanban_summary.py`: read-only stats/diagnostic projection,
  bounded task-run token rollup, complete-record validation, anomaly/cost
  accounting, canonical fingerprinting, private atomic state, per-instance
  lock, and one-attempt `hermes send` integration;
- `hermes_cli/config.py`: an empty, non-secret
  `kanban.summary.telegram_target` default so an unconfigured timer fails
  closed and a configured timer has a documented config path;
- `scripts/hermes-kanban-summary.py` and the two inert user-systemd templates;
- `scripts/install_kanban_summary.py`: explicit unit rendering with all
  placeholders filled, without enabling or starting the timer;
- `optional-skills/devops/kanban-worker/SKILL.md`: worker completion and
  `hermes.worker.v1` metadata contract guidance;
- `tests/test_kanban_summary.py`: metadata allowlisting, validation,
  aggregation, dedupe, failed-send, corrupt/oversized-state, and instance
  isolation coverage plus config-target coverage.

No dispatcher, Kanban DB schema, Telegram adapter, dashboard UI, provider
integration, credential, timer-enable, DGX, or Telegram-state files were
changed. Focused verification on Windows is `18 passed`; `git diff --check`:
`PASS`. The default
Windows pytest temporary root was inaccessible, so the
run used a temporary directory inside this isolated worktree, which was
removed after the run. The implementation review must still verify the
runtime command path, POSIX lock behavior, and the final correction set.

Review correction rounds added cost and micro-USD aggregation, anomalous-valid
record inclusion, provider/model length bounds, lock-held skip,
invalid-target fail-closed behavior, retry-after-send-failure coverage,
config/unit wiring, a unit renderer, bounded-window/truncation coverage,
canonical fingerprint coverage, malformed JSON/zero-valid/cost/schema tests.
No Telegram, DGX, or service state was changed by repository tests.

Implementation review PASS. One authenticated Claude reviewer and one
authenticated AGY reviewer inspected the same final correction set. Both
returned `PASS` with `CORRECTIONS: NONE`; AGY used an explicit worktree
add-directory boundary so its file reads were independently addressable. This
closes implementation review only. CI, commit, push, merge, runtime setup,
DGX deployment, timer enablement, and Telegram user-visible delivery remain
separate gates.

Latest implementation-review correction set: `hermes_cli/config.py`,
`scripts/install_kanban_summary.py`, updated systemd templates, and the added
Gate-4 tests. Focused verification is `18 passed`, `py_compile` PASS, and
`git diff --check` PASS. The latest authenticated Claude and AGY reviews both
returned `PASS` with `CORRECTIONS: NONE` on this exact set.
