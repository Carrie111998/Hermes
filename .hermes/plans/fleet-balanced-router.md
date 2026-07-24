---
title: "feat: Fleet-balanced task routing behind Hermes"
status: proposed
date: 2026-07-24
type: feature
target_repo: hermes-agent
---

# Fleet-balanced task router

## Outcome

Add an opt-in `hermes fleet` CLI workflow, accompanied by a Hermes skill, that
selects one qualified subscription-backed worker for a **new** task, pins that
task to the selected lane, and returns the worker's result through Hermes.
Hermes remains the only user-facing agent. The router is an edge capability:
it adds no model tool, does not mutate the system prompt during a conversation,
does not change `delegate_task`, and does not silently turn API keys into a
fleet.

The rollout order is fixed:

1. ChatGPT/Codex
2. Claude Code
3. Grok
4. Antigravity
5. Kimi, later

Landing an adapter does not make its lane eligible. A lane is selectable only
when it passes every gate below with current, attributable evidence. In
particular, an API key, an unknown credential source, an unknown overage
setting, or stale capacity data makes the lane ineligible.

## Why this fits Hermes

- The Footprint Ladder points to **CLI command + skill**. `hermes fleet run`
  gives the router structured inputs and outputs; the skill teaches Hermes when
  and how to call it with the existing terminal tool.
- There is no new core model-tool schema on every API call.
- `tools/delegate_tool.py` remains unchanged. Its current contract pins all
  subagents globally through `delegation.provider` / `delegation.model` and
  explicitly does not support per-call model selection, so it is not the
  correct fleet boundary.
- Native-provider adapters use Hermes' existing provider/auth/client resolution.
  External-CLI adapters launch a documented executable as a child process.
  They share a result protocol but are not presented as the same transport.
- Fleet state is isolated in `~/.hermes/fleet/state.db`; it does not add
  fleet-specific tables to `hermes_state.py`.
- Configuration is behavioral and therefore belongs under `fleet:` in
  `config.yaml`, never in new `HERMES_*` environment variables. Credentials
  continue to be owned by existing Hermes auth stores or the external CLI.

## User contract

The normal interaction remains:

```text
User -> Hermes: Fix the failing checkout tests using the fleet.
Hermes -> hermes fleet run --cwd ... --prompt-file ... --json
Fleet worker -> structured completion
Hermes -> User: verified result, lane provenance, and any caveats
```

The CLI surface is:

```text
hermes fleet status [--json]
hermes fleet doctor [--lane LANE] [--json]
hermes fleet plan --task-file PATH [--cwd PATH] [--json]
hermes fleet run --task-file PATH [--cwd PATH] [--task-id UUID] [--json]
hermes fleet continue TASK_ID --task-file PATH [--json]
hermes fleet audit [--task-id UUID] [--reason CODE] [--jsonl]
hermes fleet release TASK_ID [--outcome completed|failed|cancelled]
```

`plan` is read-only and never acquires capacity. `run` creates a UUID when one
is not supplied, selects exactly once, atomically pins and reserves, then runs
the pinned adapter. `continue` and `run --task-id <existing>` resolve the pin;
they never call selection again. `release` is idempotent and releases only the
matching live lease owner.

The skill must:

- invoke fleet routing only when the user explicitly asks for fleet routing or
  has enabled it as their default;
- submit a self-contained task file and explicit working directory;
- retain the returned `task_id` for follow-up turns in the same Hermes
  conversation;
- report the selected lane, adapter kind, capacity provenance, captured time,
  freshness, and confidence;
- verify claimed file/external side effects before relaying them as facts;
- never describe `planned`, `pinned`, or `started` as `completed`.

## V1 scope and lane truth

### Adapter kinds

`native_provider`
: A fresh headless Hermes child uses the existing provider resolver and Hermes
  wire adapter. It is eligible only when existing auth inspection proves an
  allow-listed subscription/OAuth credential source. It must not fall through
  to an API-key provider, OpenRouter, a custom endpoint, or a fallback chain.

`external_cli`
: Hermes launches a named, version-qualified executable with an argv list
  (`shell=False`), a bounded environment, an explicit CWD, and a
  machine-readable/non-interactive mode. Authentication and usage remain owned
  by that CLI. Hermes does not claim it is a native provider and does not scrape
  an interactive screen as an auth or completion oracle.

### Ordered delivery

| Order | Lane | V1 adapter truth | Initial state |
|---:|---|---|---|
| 1 | `chatgpt_codex` | Prefer Hermes `openai-codex` OAuth as `native_provider`; an external `codex` adapter is separate and must pass the same contract | implement first |
| 2 | `claude_code` | `external_cli`; do not substitute an Anthropic API key or claim API usage is Claude Code subscription usage | implement second |
| 3 | `grok` | Hermes `xai-oauth` as `native_provider`; never silently use `XAI_API_KEY` | implement third |
| 4 | `antigravity` | `external_cli` only after a documented non-interactive command, auth-status oracle, usage oracle, and exit contract exist | deferred/ineligible |
| 5 | `kimi` | later; existing `kimi-coding` API-key support is not evidence of a non-metered coding plan | deferred/ineligible |

Antigravity and Kimi do not get optimistic stubs that return eligible. Unknown
adapter support produces `ADAPTER_UNIMPLEMENTED`.

### Model and execution policy

Each implemented adapter owns a reviewed `LaneProfile`:

- `ordered_models`: strongest qualified model first;
- `supported_efforts`: ordered weakest to strongest;
- `fast_mode_supported`;
- static capability claims and the qualification command that verifies them;
- credential kinds allowed for subscription-only use;
- capacity-source schema and maximum sample age;
- argv builder and completion parser.

At qualification time the router chooses the first available model in
`ordered_models`, chooses `supported_efforts[-2]`, and forces fast/priority mode
off. A lane with fewer than two distinct supported effort values is ineligible
with `EFFORT_POLICY_UNSATISFIED`; the router does not reinterpret “second
highest” as “highest.” Model names are adapter data backed by a qualification
test, not a cross-provider marketing comparison. A model containing a “fast”
variant or a request that would enable fast/priority service is rejected.

For a native-provider child:

- pass the exact provider and model;
- disable provider fallback for that child;
- pass the resolved second-highest reasoning effort;
- pass no fast-mode override;
- reject resolution if the actual provider/model/credential kind differs from
  the pin.

For an external CLI:

- construct argv without a shell;
- pass the strongest qualified model, second-highest effort, and explicit
  fast-off flag when the CLI exposes those controls;
- if any required control cannot be expressed and verified from process
  metadata/output, make the adapter ineligible rather than assuming a default.

## Eligibility pipeline

Evaluation is pure and ordered. Every lane produces one `LaneEvaluation` even
when it fails early so `status`, `plan`, and audit output remain truthful.

1. **Eligibility:** lane enabled, implementation present, platform supported,
   executable/provider resolvable. Failure: `LANE_DISABLED`,
   `ADAPTER_UNIMPLEMENTED`, `PLATFORM_UNSUPPORTED`, or `ADAPTER_NOT_FOUND`.
2. **Auth:** exact credential source is inspectable and allow-listed as
   subscription/OAuth; no metered API-key fallback; overage/pay-as-you-go is
   explicitly known off. Failure: `AUTH_MISSING`, `AUTH_KIND_FORBIDDEN`,
   `AUTH_SOURCE_UNKNOWN`, or `OVERAGE_STATUS_UNKNOWN_OR_ON`.
3. **Qualification:** version is in the supported range, non-billing auth
   status succeeds, model/effort/fast controls are supported, and the last
   qualification sample is current. A model inference request is never an
   auth probe. Failure: `QUALIFICATION_FAILED` or `QUALIFICATION_STALE`.
4. **Capability:** task requirements are a subset of the lane's qualified
   capability set (for example workspace read/write, shell, vision, or maximum
   context class). Failure: `CAPABILITY_MISMATCH`.
5. **Occupancy:** active, unexpired leases are below the lane's configured
   concurrency limit. Failure: `OCCUPANCY_FULL`.
6. **Reserve:** after subtracting active reservations, the candidate can
   reserve the task's configured percentage-point budget without crossing its
   protected reserve floor. Failure: `RESERVE_FLOOR`.
7. **Freshness:** capacity has numeric used/remaining values in `[0, 100]`, a
   named source, capture timestamp, expiry policy, and sufficient confidence.
   Failure: `CAPACITY_MISSING`, `CAPACITY_INVALID`, `CAPACITY_STALE`, or
   `CAPACITY_CONFIDENCE_LOW`.
8. **Cooldown:** lane cooldown has expired. Failure: `LANE_COOLDOWN`.

Selection considers only evaluations with all eight gates `MET`. No-data is
not zero usage; it is ineligible. If no lane remains, return
`NO_ELIGIBLE_LANE` with every lane's reason codes and do not start any child.

## Capacity provenance

Every lane snapshot is normalized to:

```python
CapacitySnapshot(
    lane_id: str,
    used_pct: Decimal,
    remaining_pct: Decimal,
    reserved_pct: Decimal,
    effective_remaining_pct: Decimal,
    source_kind: Literal["native_read_only", "external_cli", "bridge_file"],
    source_id: str,
    captured_at: datetime,
    read_at: datetime,
    expires_at: datetime,
    freshness: Literal["fresh", "stale", "invalid"],
    confidence: Literal["high", "medium", "low"],
    schema_version: str,
    overage_disabled: bool | None,
)
```

`remaining_pct` must agree with the source's total/used fields within a defined
decimal tolerance. Selection uses `effective_remaining_pct = remaining_pct -
active_reserved_pct`, quantized to `0.001`; floats, NaN, infinity, negative
values, and values above 100 are invalid.

`C:/HermesBridge/usage-weekly.json` is an optional `bridge_file` source:

- open it read-only, never create, truncate, rename, lock, normalize, or update
  it;
- accept only an explicitly versioned schema mapping a provider lane to
  remaining/used percentage, capture time, and overage-disabled evidence;
- ignore unknown fields, but reject missing required fields;
- record path, SHA-256, schema version, capture time, read time, freshness, and
  confidence in the audit event;
- absence, sharing violation, malformed JSON, invalid values, stale content, or
  an unknown provider entry affects only that lane and never crashes routing;
- bridge data is evidence of capacity, not evidence of authentication,
  qualification, or capability.

The bridge path is a normal config value with that Windows path as the default;
it is not a dependency and is never watched continuously in v1.

## Deterministic selection

Let `P` be the first eligible lane in the fixed order and let `B` be the
largest `effective_remaining_pct` among eligible lanes.

1. If `B - P.effective_remaining_pct < 20.000`, choose `P`.
2. If the difference is at least `20.000`, form the switch set containing the
   eligible lanes whose effective remaining capacity equals `B` exactly after
   `0.001` quantization.
3. Choose from the switch set by persisted round-robin cursor over the fixed
   lane order. Advance the cursor only in the same transaction that commits the
   winning pin, lease, reservation, and audit decision.

Thus `60.000` versus `79.999` stays on the priority lane, while `60.000`
versus `80.000` switches. A dry-run `plan` computes the same candidate but
does not advance rotation. Repeated evaluation of identical state returns the
same decision; committed tied selections rotate deterministically.

The selector never runs for an existing task pin. A pinned lane becoming stale,
full, cooled down, unauthenticated, or unavailable causes a reason-coded
`PINNED_LANE_UNAVAILABLE` result. It does not migrate the task. Explicit
operator recovery closes the old task and starts a new task ID, preserving both
audit chains.

## Atomic state, leases, reserves, and cooldowns

Use a dedicated SQLite database at `get_hermes_home() / "fleet" / "state.db"`.
SQLite is already a runtime dependency through the standard library and gives
Windows-safe cross-process transactions without a new package.

Schema:

```text
tasks(
  task_id PK, lane_id, adapter_kind, provider_id, model_id, effort,
  fast_mode, cwd_fingerprint, status, created_at, updated_at, terminal_at
)
leases(
  task_id PK/FK, lane_id, owner_uuid, generation, reserved_pct,
  acquired_at, heartbeat_at, expires_at, released_at
)
lane_state(
  lane_id PK, rotation_selected_at, cooldown_until, cooldown_reason,
  qualification_json, qualification_expires_at, updated_at
)
rotation(
  policy_id PK, next_lane_index, generation, updated_at
)
audit_events(
  event_id INTEGER PK AUTOINCREMENT, event_uuid UNIQUE, task_id, lane_id,
  at, event_type, reason_code, decision_json
)
```

`BEGIN IMMEDIATE` wraps stale-lease reap, occupancy/reserve recomputation,
selection, rotation, task pin, lease insert, and `ROUTE_SELECTED` audit insert.
The transaction either commits all of them or none. `owner_uuid + generation`
makes heartbeat and release identity-checked and idempotent; a stale owner
cannot release a newer lease. Reservations are sums of unexpired,
identity-valid lease rows, not mutable counters. A lease expires only after a
bounded TTL and missed heartbeats; reaping writes `LEASE_EXPIRED`.

Provider rate-limit, exhaustion, or retry-after output sets a lane cooldown in
a transaction with `COOLDOWN_SET`. Authentication/billing-policy failures do
not get blind retries; they make the lane ineligible until a new `doctor`
qualification succeeds. A running pinned task is never moved because of a
cooldown.

`audit_events` is the authoritative, append-only, reason-coded audit log and is
queryable through `hermes fleet audit --jsonl`. Standard Hermes logging may
mirror summaries, but a logger failure cannot erase the transactional event.
Audit payloads contain hashes/identifiers and normalized metadata, never
prompts, child output, access tokens, CLI auth files, or environment values.

## Exact implementation files

New production files:

- `hermes_cli/fleet/__init__.py` — public package surface and schema version.
- `hermes_cli/fleet/types.py` — frozen task, lane, capacity, evaluation,
  decision, lease, and result types plus reason-code enum.
- `hermes_cli/fleet/config.py` — parse and validate the `fleet:` config subtree;
  no credentials and no environment-variable config.
- `hermes_cli/fleet/capacity.py` — `CapacitySource` protocol, normalization,
  freshness/confidence rules, and read-only HermesBridge adapter.
- `hermes_cli/fleet/policy.py` — pure gate evaluation and deterministic
  20-point/rotation selection.
- `hermes_cli/fleet/state.py` — schema migration, `BEGIN IMMEDIATE`
  transactions, pins, leases, reservations, cooldowns, cursor, and audit.
- `hermes_cli/fleet/adapters/base.py` — adapter protocol, qualification/result
  contracts, safe child environment, timeout/cancellation behavior.
- `hermes_cli/fleet/adapters/native_provider.py` — exact-provider Hermes child
  with fallback disabled and credential-kind verification.
- `hermes_cli/fleet/adapters/external_cli.py` — argv-only subprocess adapter and
  bounded environment; no shell and no interactive screen scraping.
- `hermes_cli/fleet/profiles.py` — ordered ChatGPT/Codex, Claude Code, and Grok
  lane profiles; deferred lanes are reported unsupported, not eligible.
- `hermes_cli/fleet/service.py` — orchestration: inspect, select/pin, execute,
  heartbeat, classify completion, cooldown, and release.
- `hermes_cli/subcommands/fleet.py` — argparse tree and human/JSON rendering.
- `skills/autonomous-ai-agents/fleet-balanced-router/SKILL.md` — Hermes-facing
  workflow, task-ID continuity, verification, and truthful reporting rules.
- `docs/user-guide/features/fleet-balanced-router.md` — configuration,
  security/billing guarantees, operations, audit, and limitations.

Minimal existing-file edits:

- `hermes_cli/main.py` — lazily register `build_fleet_parser(...)` following
  existing subcommand modules.
- `hermes_cli/config.py` — add documented `fleet:` behavioral defaults only.

Tests:

- `tests/hermes_cli/fleet/test_capacity.py`
- `tests/hermes_cli/fleet/test_policy.py`
- `tests/hermes_cli/fleet/test_state.py`
- `tests/hermes_cli/fleet/test_adapters.py`
- `tests/hermes_cli/fleet/test_service.py`
- `tests/hermes_cli/fleet/test_cli.py`
- `tests/hermes_cli/fleet/test_e2e.py`

Do not edit `model_tools.py`, `toolsets.py`, `tools/delegate_tool.py`,
`run_agent.py`, `hermes_state.py`, provider auth files, or dependency manifests
unless a later implementation proves a real generic bug independently of this
feature. Do not add an integration hook speculatively.

## Strict TDD order

Each numbered slice is a separate RED -> GREEN -> REFACTOR cycle. Do not write
the production file named by a slice before its RED test fails for the expected
missing behavior.

1. **RED:** reason codes, immutable data contracts, decimal validation, and
   bridge normalization tests. **GREEN:** `types.py` and `capacity.py`.
2. **RED:** all eight gate tests, no-eligible result, 19.999/20.000/20.001
   boundaries, exact ties, cursor behavior, and dry-run non-mutation.
   **GREEN:** pure `policy.py`.
3. **RED:** clean database creation, concurrent pin acquisition, lease
   ownership/generation, reserve sums, TTL reap, cooldown, audit atomicity,
   rollback on injected failure, and cursor advancement in the winning
   transaction. **GREEN:** `state.py`.
4. **RED:** config defaults and rejection of credentials, unknown lane keys,
   invalid percentages/TTLs, and unsupported deferred adapters.
   **GREEN:** `config.py` and `profiles.py`.
5. **RED:** native exact-provider/auth-kind/fallback-off enforcement and
   external argv/environment/result contracts. Include explicit API-key,
   unknown-auth, overage-on, missing fast-off, malformed output, timeout,
   cancellation, and executable substitution attacks. **GREEN:** adapters.
6. **RED:** service tests proving selection only on first task use, continuation
   pinning, pinned-lane fail-closed behavior, heartbeat/release, cooldown
   classification, and no execution when any gate fails. **GREEN:** `service.py`.
7. **RED:** CLI JSON schema, exit codes, help, dry-run, audit filtering, and
   human output provenance. **GREEN:** subcommand wiring in
   `subcommands/fleet.py` and `main.py`.
8. **RED:** a clean-`HERMES_HOME` E2E using fake native and external adapters
   through the real parser, real SQLite store, real read-only bridge file, and
   multiple processes. **GREEN:** only integration corrections.
9. **RED:** skill static checks require task-ID continuity, provenance,
   verification, and billing fail-closed language. **GREEN:** skill and docs.
10. **REFACTOR:** remove duplication without changing behavior, then run the
    focused suite, broader CLI/auth/provider regressions, and adversarial suite.

No test may call a real model, auth endpoint, provider usage endpoint, or
external coding CLI. Fakes must exercise the real process/SQLite/file
boundaries, not monkeypatch away the policy or transaction.

## Test commands

Canonical repository runner (from the repository root):

```bash
# RED/GREEN per slice
bash scripts/run_tests.sh tests/hermes_cli/fleet/test_capacity.py -q
bash scripts/run_tests.sh tests/hermes_cli/fleet/test_policy.py -q
bash scripts/run_tests.sh tests/hermes_cli/fleet/test_state.py -q
bash scripts/run_tests.sh tests/hermes_cli/fleet/test_adapters.py -q
bash scripts/run_tests.sh tests/hermes_cli/fleet/test_service.py -q
bash scripts/run_tests.sh tests/hermes_cli/fleet/test_cli.py -q
bash scripts/run_tests.sh tests/hermes_cli/fleet/test_e2e.py -q

# Complete feature
bash scripts/run_tests.sh tests/hermes_cli/fleet -q

# Existing surfaces touched or depended on
bash scripts/run_tests.sh tests/hermes_cli/test_argparse_flag_propagation.py -q
bash scripts/run_tests.sh tests/hermes_cli -k "auth or provider or config or command" -q
bash scripts/run_tests.sh tests/agent -k "auxiliary_client or credential" -q
```

Clean-state and adversarial verification:

```powershell
# Must start clean and show only intended implementation files afterward.
git status --short

# Plan/status must not create fleet state or mutate the optional bridge input.
$before = if (Test-Path -LiteralPath 'C:\HermesBridge\usage-weekly.json') {
  (Get-FileHash -Algorithm SHA256 -LiteralPath 'C:\HermesBridge\usage-weekly.json').Hash
}
hermes fleet plan --task-file .\tests\fixtures\fleet-task.txt --json
$after = if (Test-Path -LiteralPath 'C:\HermesBridge\usage-weekly.json') {
  (Get-FileHash -Algorithm SHA256 -LiteralPath 'C:\HermesBridge\usage-weekly.json').Hash
}
if ($before -ne $after) { throw 'bridge input changed' }

# No tracked file outside the reviewed implementation/test/docs list.
git diff --check
git status --short
```

The E2E/adversarial test module must additionally prove:

- fresh empty `HERMES_HOME`, empty credential environment, absent bridge file;
- malformed/truncated/oversized JSON, duplicate provider rows, unknown schema,
  stale/future timestamps, NaN/infinity, negative and over-100 percentages;
- bridge file replaced between stat/read, read denied, and contents unchanged;
- all API-key environment variables present and rejected/scrubbed;
- CLI executable path containing spaces/metacharacters cannot inject a shell;
- adapter reports a different provider/model/auth source than requested;
- 32 concurrent selectors against capacity one yield one live lease and no
  double reservation;
- database busy, process crash between begin/commit, stale owner release,
  heartbeat race, lease expiry, and cooldown expiry;
- 19.999 does not switch, 20.000 does switch, and exact top-capacity ties
  rotate in fixed order across committed new tasks;
- an existing task remains pinned while capacity reverses by 100 points;
- no eligible lane causes zero child processes and a complete reason matrix;
- audit events contain no prompt text, token, auth file contents, or inherited
  credential values.

## Acceptance criteria

Every criterion has a binary `MET` oracle. “Looks correct” is not an oracle.

- **AC1 — Hermes remains user-facing.** **MET:** E2E submits through the skill
  contract/`hermes fleet run`, observes one normalized child result returned to
  Hermes, and finds no new core tool schema or direct user-to-worker channel.
- **AC2 — New-task-only selection.** **MET:** selector call count is one for a
  new `task_id` and zero for all continuations of that ID.
- **AC3 — Durable task pin.** **MET:** after reversing all capacity values, a
  continuation resolves the original lane/model/effort/adapter tuple; if that
  lane is unavailable, execution fails `PINNED_LANE_UNAVAILABLE` and no other
  adapter starts.
- **AC4 — Ordered lanes.** **MET:** with equal eligible capacities the first
  new task chooses ChatGPT/Codex; disabling successive lanes yields Claude
  Code then Grok; Antigravity and Kimi return `ADAPTER_UNIMPLEMENTED` until
  their ordered implementation slices land.
- **AC5 — Honest adapter provenance.** **MET:** JSON output and audit identify
  `native_provider` or `external_cli`, exact provider/executable, credential
  kind (never secret), model, and qualification evidence; mismatch tests fail.
- **AC6 — Complete gates.** **MET:** table-driven tests independently fail
  each of eligibility, auth, qualification, capability, occupancy, reserve,
  freshness, and cooldown, and assert no process launch.
- **AC7 — Model policy.** **MET:** fixture capabilities `[m1, m2, m3]` and
  efforts `[low, medium, high, max]` produce `m1`, `high`, and `fast=false`;
  missing second effort or unverifiable fast-off is ineligible.
- **AC8 — Billing/overage fail-closed.** **MET:** API key, unknown credential
  source, provider fallback, custom endpoint, overage true, and overage null
  each produce an auth/billing reason and zero model/CLI invocations.
- **AC9 — Exact 20-point switch.** **MET:** Decimal boundary tests prove
  `19.999` stays on priority, `20.000` switches to maximum capacity, and
  `20.001` switches.
- **AC10 — Deterministic rotation.** **MET:** identical committed tie fixtures
  select lanes in persisted fixed-order round robin; dry-runs neither change
  the cursor nor the next committed result.
- **AC11 — Per-lane evidence.** **MET:** status JSON includes source ID/hash,
  captured/read/expiry timestamps, freshness, confidence, raw remaining,
  reserved, and effective remaining for every lane, including failed lanes.
- **AC12 — Atomic capacity accounting.** **MET:** the 32-process test never
  exceeds concurrency/reserve limits, and injected transaction failure leaves
  no pin, lease, reserve, cursor change, or route-selected audit event.
- **AC13 — Lease safety and cooldowns.** **MET:** stale generation cannot
  heartbeat/release a newer lease; expiry and reason-coded cooldown transitions
  are observable and deterministic under a fake clock.
- **AC14 — Auditability.** **MET:** every decision/denial/lease/cooldown/
  completion has a documented reason code and task correlation ID in the
  transactional audit; secret-canary scan returns no hit.
- **AC15 — Optional bridge is read-only.** **MET:** before/after SHA-256 and
  access-spy assertions are identical, and absence/corruption/staleness degrades
  only the affected capacity source without a write attempt.
- **AC16 — Clean installation.** **MET:** clean-`HERMES_HOME` E2E passes with
  no real credentials/network and creates only `fleet/state.db` after `run`
  (not after `plan`).
- **AC17 — Existing behavior is preserved.** **MET:** focused fleet tests,
  parser/config/auth/provider regressions, `git diff --check`, and the
  repository's required CI suite pass.

## Activation

1. Ship code with `fleet.enabled: false`; `status`, `doctor`, and `plan` remain
   available but `run` returns `FLEET_DISABLED`.
2. Run `hermes fleet doctor --json` with no model inference. Review each lane's
   adapter kind, auth kind, qualification, capacity provenance, overage status,
   freshness, confidence, and all failed gates.
3. Enable one lane at a time in rollout order under `fleet.lanes` and keep the
   global switch off. Re-run doctor and focused E2E with fake execution.
4. Set `fleet.enabled: true` only after at least one lane is fully `MET`.
5. Activate the skill for users who want natural-language routing. Existing
   conversations keep a byte-stable system prompt; the skill is loaded through
   normal skill mechanics and applies on the next task.
6. Start with concurrency one and conservative per-task reservation. Raise
   limits only from observed reason-coded audit evidence.

Example behavioral configuration shape (documentation, not a credential
store):

```yaml
fleet:
  enabled: false
  bridge_usage_file: "C:/HermesBridge/usage-weekly.json"
  switch_delta_pct: 20.0
  minimum_confidence: high
  lease_ttl_seconds: 1800
  lanes:
    chatgpt_codex: {enabled: true, max_concurrency: 1, reserve_floor_pct: 10}
    claude_code: {enabled: false, max_concurrency: 1, reserve_floor_pct: 10}
    grok: {enabled: false, max_concurrency: 1, reserve_floor_pct: 10}
    antigravity: {enabled: false}
    kimi: {enabled: false}
```

`switch_delta_pct` is exposed for transparency but v1 validation requires it
to equal exactly `20.0`; making the policy tunable is a later decision.

## Rollback

1. Set `fleet.enabled: false`. This blocks new tasks but does not kill a running
   child or rewrite existing pins.
2. Let live leases drain, or explicitly cancel/release named task IDs. Record
   `TASK_CANCELLED`/`LEASE_RELEASED`; do not delete state.
3. Disable/remove the fleet skill. Hermes continues with its existing provider
   and normal delegation unchanged.
4. Revert the fleet implementation commit if needed. Keep
   `~/.hermes/fleet/state.db` as recoverable audit evidence; the feature has no
   migrations in the main session database.
5. Re-enable only after `doctor`, the focused suite, and the previously failing
   adversarial oracle are `MET`.

Rollback never edits auth stores, provider config, the bridge usage file, or a
worker's external-CLI configuration.

## Risks and mitigations

- **Subscription usage is not a universal API.** Mitigation: source-specific,
  versioned read-only evidence; stale/unknown means ineligible.
- **OAuth does not always prove “no overage.”** Mitigation: require explicit
  overage-disabled evidence independently of credential kind.
- **Model branding changes.** Mitigation: ordered, qualified adapter profiles;
  do not guess from name strings or silently downgrade.
- **A CLI can change flags/output.** Mitigation: version qualification and
  machine-readable completion contract; mismatch cools the lane and fails the
  pinned task.
- **Parallel Hermes processes can overbook.** Mitigation: SQLite
  `BEGIN IMMEDIATE`, computed reservations, owner generations, and
  multi-process adversarial tests.
- **Capacity percentage may not predict task cost.** Mitigation: explicit
  conservative reservation classes and protected reserve floors; do not call
  them exact token forecasts.
- **A worker may claim a side effect it did not perform.** Mitigation: Hermes
  verifies shared-workspace/external evidence before reporting completion.
- **Child environment can leak billable credentials.** Mitigation: allow-list
  environment construction and canary tests; native adapter rejects any
  credential-source mismatch.
- **Pinned lane failure reduces availability.** Mitigation: truthful
  fail-closed behavior and explicit new-task recovery, not invisible migration.
- **Long synchronous workers can outlive the invoking turn.** Mitigation:
  explicit cancellation, heartbeat, lease expiry, and durable task/audit state;
  durable background execution is not claimed in v1.

## Clear V1 limitations

- V1 selects only at task creation. It does not migrate, rebalance, race,
  ensemble, or fall back a running/pinned task.
- ChatGPT/Codex, Claude Code, and Grok land in order; Antigravity remains
  ineligible until a supported external-CLI contract exists. Kimi is later and
  cannot use the existing billable API-key path.
- No lane is eligible from an API key, unknown auth, unknown overage state, or
  stale/missing capacity. This may intentionally produce no route.
- Capacity is weekly percentage evidence plus active reservations, not an
  exact token/cost forecast.
- The bridge file is optional, polled at decision time, and read-only. V1 does
  not run a watcher or repair its contents.
- V1 uses one local SQLite store and coordinates processes sharing the same
  `HERMES_HOME`; it is not a distributed lease service across hosts.
- V1 external CLI execution requires a documented non-interactive,
  machine-readable mode. Interactive TUI scraping is out of scope.
- V1 does not change desktop/TUI/gateway UI. Those surfaces see Hermes'
  narrative result; structured fleet UI can follow after the CLI contract is
  stable.
- V1 does not alter core prompt caching, provider fallback, auth storage,
  credential pools, session routing, or `delegate_task`.
- V1 audit stores metadata and reason codes, not full prompts or worker
  transcripts.

## Definition of done

The implementation is upstream-ready only when every acceptance criterion is
`MET`, every adapter accurately labels its transport/auth/capacity source,
clean-state and adversarial suites pass, the optional bridge input's hash is
unchanged, no new dependency or model tool exists, and `git status --short`
contains only the reviewed fleet implementation, tests, skill, and docs.
