# Evidence-Fenced Autonomous Orchestration v1

Status: implementation contract for the opt-in GitHub-native execution mode. Existing Kanban tasks retain their current behavior.

## Purpose

This contract supports planner-authorized software campaigns without turning Hermes Kanban into a second product backlog. It replaces serialized epic workers, broad orientation prompts, heartbeat-defined progress, and overlapping LLM pollers with versioned atomic leaves, bounded concurrency, verified evidence, and restart-safe attempts.

The initial implementation is deliberately smaller than a complete GitHub Project orchestrator. It establishes the execution-kernel invariants that must exist before real unattended Veltro leaves can be piloted.

## Authority and source of truth

| Concern | Authority |
| --- | --- |
| Product goal, priority, pause/cancel, product and security-policy decisions | Planner |
| Intent, dependencies, acceptance criteria, priority, PR linkage, delivery status | GitHub Issues/Projects |
| Code, tests, schemas, ADRs | Git at a pinned commit |
| Decomposition, leaf specifications, dispatch, review routing, reconciliation, successor selection | Orchestrator |
| Attempt, claim, worker, worktree, lease, evidence, retry, blocker | Hermes Kanban runtime |
| Operator visualization and commands | Workflow desktop plugin; projection/control surface only |
| Navigation and compressed history | Hindsight; never canonical |
| One bounded implementation assignment | Worker |

Workers cannot invent or dispatch successors, edit product intent, self-approve, merge, release, or administer the Project. Detailed attempt states remain in Kanban; the human-facing Project remains compact.

## Availability topology

The laptop and Desktop process are outside the Workflow v1 availability
boundary. The durable controller runs on the Hermes remote server inside the
long-lived gateway service. On Linux that gateway is supervised by the existing
Hermes/systemd service lifecycle; a Desktop connection is not a prerequisite for
startup, ticks, recovery, or shutdown handling.

```text
GitHub                          Hermes remote server
canonical product state  --->  supervised Workflow controller
                               runtime database and dispatch saga
                               reconciliation and worker supervision
                               evidence verification and kill switch
                                             ^
                                             |
                                  authenticated typed API
                                             |
                                  laptop / Desktop client
```

The remote controller owns its own persisted epoch, enabled/paused state, last
successful reconciliation time, current saga phase, and last error. Gateway
restart reconstructs that state from the server database and live process/Git
facts before any new dispatch. Controller health is measured from its remote
tick record and supervised gateway process, never from a WebSocket, browser tab,
or Desktop heartbeat.

Closing, suspending, or disconnecting the laptop must not:

- stop a worker or controller tick;
- release, renew, or cancel a claim;
- expire an evidence lease early;
- alter dependency, gate, readiness, or dispatch-saga state;
- project completion or failure to GitHub;
- enable or disable dispatch.

On reconnect, Desktop discards cached mutable state and fetches a fresh,
versioned projection from the remote controller. It must not replay queued
drag/drop, retry, pause, approval, or cancellation requests. Every operator
mutation is a new typed request carrying the expected controller/projection
version; stale versions fail closed and require a fresh read.

The remote kill switch is durable and defaults off. It is checked before
specification materialization, readiness promotion, worktree provisioning,
claim, spawn, retry, and successor dispatch. It remains operable through a
server-local administrative command when Desktop is unavailable. A failed or
stale controller-health probe blocks new dispatch but does not infer that an
existing worker is dead; reconciliation must inspect the fenced run, process,
and worktree identities.

## Desktop control plane and generic Kanban boundary

The official Hermes desktop Kanban plugin is a native presentation layer over
the generic Kanban REST and `kanban_db` surfaces. It deliberately supports local
task creation, editable titles and bodies, dependency editing, auxiliary-model
decomposition, status dragging, reassignment, reclaim, and immediate dispatcher
nudge. Those are appropriate for a general local work queue. They are not the
authority model for Workflow v1.

Workflow v1 therefore uses a separate GitHub-native desktop page, delivered
through the supported Desktop Plugin SDK:

- GitHub Issues/Projects are read using stable repository, issue, Project item,
  field, and pull-request identities. Every material snapshot records the
  source `updatedAt`/version and a canonical content hash.
- Product title, intent, acceptance criteria, priority, dependencies, gates,
  and completion are displayed from GitHub. They are not freely editable local
  Kanban fields.
- Operator commands are closed, typed operations: refresh from GitHub, pause
  local dispatch, cancel a current attempt, approve an already-declared human
  gate, request retry, or open the canonical GitHub object. A product mutation
  goes through an orchestrator-owned GitHub operation with expected-version and
  read-back verification; it is never a local-only card edit.
- Local runtime overlays show leaf version, run/token state, worktree, evidence
  age, blocker class, review SHA, CI SHA, and reconciliation state. Raw claim
  tokens, worker PIDs, and absolute workspace paths are never projected.
- The page does not expose generic auto-decompose, local dependency editing,
  direct `ready`/`running` dragging, generic task creation, or dispatcher nudge
  for evidence-fenced leaves.

Current pre-pilot implementation status:

- the systemd-supervised remote gateway has an independent, singleton Workflow
  watcher which runs even while `kanban.dispatch_in_gateway=false`;
- the watcher is monitor/reconcile-only and persists controller epoch,
  heartbeat, reconciliation result, audited control version, durable dispatch
  switch, and broker readiness in each board database;
- the plugin backend exposes a fresh `/workflow/projection` projection plus CAS-fenced
  `/workflow/controller/pause` and `/workflow/controller/resume` operations;
- Desktop exposes that projection on a separate `/workflow` route. It refetches
  on mount, focus, and a bounded interval, attempts controls once with no
  offline retry/replay, and offers no leaf/card mutation controls;
- a server-local `hermes kanban workflow-status` / `workflow-pause` path keeps
  the emergency control usable without Desktop; and
- remote pause/resume additionally require an authenticated native/interactive
  session, `kanban.workflow.remote_control_enabled=true`, and an exact
  request-derived principal entry in
  `kanban.workflow.remote_control_principals`. Each principal receives an
  explicit operation list such as `[pause]` or `[pause, resume]`; authentication
  alone grants neither operation. Both settings default deny, and durable audit
  rows use the authenticated principal rather than a client-supplied actor; and
- `workflow.broker_ready` (a retained compatibility name for worker-runtime
  readiness) and the durable dispatch switch both default false. Resume is
  rejected until an operator has verified the configured worker runtime and
  explicitly marks it ready. This branch is not installed on the live server
  and does not authorize a pilot merely by changing that flag.

The renderer is not a control-plane boundary. Desktop plugins have full
application authority through `host.request` and their scoped backend; backend
code runs in the gateway. Session authentication protects the operator HTTP
surface. Workflow v1 otherwise uses a trusted, cooperative-worker threat model:
workers can make mistakes or drift, but are not treated as hostile same-UID
processes attempting to bypass Python or open SQLite directly.

Worker containment is therefore operational rather than adversarial: one
bounded prompt contract, one isolated worktree, a restricted normal tool
surface, explicit exclusions, finite budgets, structured results, and
controller-side validation. Each launch also receives a controller-owned
isolated `HOME`/`HERMES_HOME`, an empty GitHub CLI configuration, no profile
`.env`, no GitHub or SSH environment credentials, and only the explicitly
selected inference provider's narrowed credential. A real launcher remains
unready until both its model and provider are configured. Containers, dedicated
worker UIDs, and a non-forgeable security broker are outside this contract.

Evidence-fenced rows are protected runtime records. Generic Kanban REST, CLI,
slash-command, model-tool, dashboard, and desktop-plugin mutation paths must
reject changes to their identity, specification, dependencies, assignment, or
lifecycle state unless the request carries an operation-specific trusted
controller capability. Existing non-evidence tasks keep normal Kanban behavior.

Generic read projections exclude protected task/run details and every sibling
surface that could reveal them: worker logs and process inspection, run tokens
and PIDs, comments and notification subscriptions, active-worker lists,
diagnostics, stats/assignee counts, board-switcher counts, relationship rollups,
and event/WebSocket cursors. Workflow runtime data is available only through the
separate redacted `/workflow/projection` contract.

## Campaign and leaf identity

A GitHub campaign issue may own several internal implementation leaves. Atomic work does not require a GitHub sub-issue for every 10–25 minute action.

A leaf version is identified by canonical fields:

```text
(repository identity, campaign issue identity, leaf spec id, leaf spec version)
```

Hermes stores a canonical `leaf_key` derived by the orchestrator from those fields. `leaf_key` is immutable and globally unique within a board. Retrying the same leaf version creates another run for the same task; changing the leaf specification creates a new version and therefore a new key.

The frozen dispatch identity also contains:

- `spec_hash`: hash of canonical scope, exclusions, dependencies, gates, and acceptance checks;
- `pin_sha`: exact source commit used to build the capsule;
- `capsule_hash`: hash of the targeted worker capsule;
- the immutable `spec_hash` includes the exact canonical dependency id set; each
  run copies that hash, and readiness plus the final transactional claim compare
  the persisted links to the frozen spec;
- `evidence_paths`: conservative repository-relative path ownership set;
- allowed symbols, hazards, acceptance commands, required checks, artifact budget,
  and human-gate policy.

Material changes create a new leaf version and supersede the old attempt. Cosmetic issue edits do not.

## Context capsule

The orchestrator constructs a bounded capsule using targeted search and validates acceptance-relevant facts against `pin_sha`. It contains:

- repository and campaign identity;
- leaf id/version and the three dispatch hashes;
- atomic objective and explicit exclusions;
- exact paths and relevant symbols;
- governing decisions needed by the leaf;
- dependency and base assumptions;
- acceptance commands and expected artifact;
- hazards and human/external gates;
- first-evidence and wall-clock budgets;
- required terminal result schema.

Workers open named files and nearby seams. Neither worker nor orchestrator performs a wholesale documentation crawl unless a concrete unresolved question requires it.

## Runtime state model

Product state and execution state are distinct.

A leaf task moves through the existing Kanban states:

```text
triage/todo -> ready -> running -> review -> done
                         |           |
                         +-> blocked-+
                         +-> archived (superseded/cancelled)
```

A run is one fenced attempt and moves through:

```text
reserved -> running -> done | blocked | released | crashed | timed_out | failed
```

`done` means the leaf artifact completed. It does not imply that the campaign issue, PR, release, deployment, or human gate completed.

## Mechanized invariants

### I1. Atomic leaf uniqueness

At most one task row exists for a non-null `leaf_key`. Concurrent creators of the same leaf version return the same task id. This must be a database uniqueness constraint, not a read-before-write convention.

### I2. Attempt fencing

Each claim creates a monotonic `task_runs.id` and an opaque claim token. Worker mutations require the current run id; lease renewal additionally requires the opaque token. Stale, expired, reclaimed, cancelled, or superseded attempts cannot heartbeat, submit evidence, complete, or block the current run.

The final claim transaction rechecks the durable dispatch switch, broker gate,
healthy controller epoch, fresh controller heartbeat, exact dependency links,
exclusive workspace ownership, and the live pinned HEAD, authorized branch, and
clean-tree coordinate after the global reservation is acquired. Dispatch repeats
the live readiness check after invocation materialization and immediately before
entering the launcher seam. A preflight read cannot race a pause, same-SHA branch
switch, or second workspace claim into success.

For every completed Workflow dependency in the same repository, readiness also
loads its latest non-invalidated, independently approved closeout candidate and
proves with Git that candidate is an ancestor of the child `pin_sha`. A missing
closeout, an unavailable Git coordinate, or a stale child pin fails closed. A
dependent phase therefore needs an explicitly superseded specification and a
fresh pin that integrates all dependency commits; completion status alone is
not dependency evidence.

### I3. Evidence-backed lease

Opt-in `evidence` leases distinguish liveness from progress:

- runtime activity may update `last_heartbeat_at`;
- heartbeat notes do not extend `claim_expires`;
- a live PID does not automatically extend an expired evidence lease;
- only a new verified workspace delta extends the lease.

Real workers publish progress through a fence-derived, per-run directory which
is separate from the terminal result proposal. Each bounded UTF-8 JSON regular
file carries the task, run, opaque fence, controller epoch, running state, and a
monotonic sequence. The gateway-hosted production tick opens each channel entry
with `O_NOFOLLOW`, validates a stable regular-file coordinate and bounded UTF-8
JSON, and uses the kernel-observed later of inode mtime/ctime as the publication
coordinate before computing Git evidence itself. A file published while its
lease was valid remains eligible when the next controller poll occurs just after
the deadline; a file created or metadata-backdated after expiry fails because
its ctime is late. A terminal proposal published before both its lease and
wall-clock cutoff can complete ingestion after the polling boundary, but cannot
renew runtime beyond that cutoff; ordinary progress remains subject to the hard
wall-clock deadline. Consumed sequences are persisted before their files are
removed. A repeated artifact digest is consumed as liveness but does not renew the lease. The worker
prompt requires an atomic first proposal before the first-evidence deadline and
new proposals after meaningful deltas and during long-running checks.

The initial claim deadline is capped by the leaf's `first_evidence_seconds`.
The remote Workflow controller—not the generic dispatcher—enforces expired
evidence leases and `wall_clock_budget_seconds`. If a host-local process
survives TERM/KILL, the task remains occupied and its task/run leases remain
expired: no successor can claim the workspace and the stale token cannot regain
mutation authority. The controller retries termination and reports degraded
health rather than adopting the attempt as healthy.

A positive process-death observation is not by itself a terminal outcome while
the current evidence lease remains valid. The run stays fenced so the runtime
can ingest a bounded terminal proposal written at worker exit. On established
controller epochs, channel ingestion precedes stale-lease reconciliation; a
proposal produced just before the boundary must not be reclaimed before it is
evaluated. If no valid proposal exists, normal lease expiry performs the typed
failure/reclaim path.

A workspace delta qualifies when the kernel observes, under an allowed path, a new scoped diff, untracked file content, or commit relative to `pin_sha`. Repeating the same digest is liveness, not progress. Empty commits, out-of-scope changes, and narrative notes do not qualify.

Legacy/default `heartbeat` leases retain current behavior.

### I4. Recoverable dispatch

Dispatch is a saga:

1. atomically create/adopt the leaf task;
2. provision/adopt its isolated worktree;
3. claim and persist the run/token;
4. spawn the worker and persist PID;
5. project the verified transition.

On restart, reconciliation reads durable task/run state and process/worktree evidence. It adopts one canonical current attempt, closes orphaned historical runs only after process death is established, and never treats missing heartbeat alone as proof of death. A live orphan or non-active task with a live current run remains ownership-occupying and degrades controller health until termination is confirmed.

Controller-side Git inspection disables system/global configuration and
repository-local executable settings such as `core.fsmonitor`, hooks, external
diffs, and text conversion before readiness or evidence computation.

### I5. Successor ownership

A worker executing an evidence-fenced leaf reports its result and proposed
follow-ups but does not create Kanban successors through its normal tool
surface. Successors are a controller mutation after closeout and canonical
GitHub readiness evaluation. Generic task creation rejects protected parents by
default; the explicit controller registration path opts in. This is a drift and
ownership guardrail, not a security claim against deliberate same-UID bypass.

### I6. SHA-bound review

A review verdict records reviewer identity, candidate head SHA, acceptance checklist, and staged-diff digest. A new commit or rebase invalidates the verdict. Protected CI must pass on the reviewed candidate SHA before merge where repository policy requires CI.

The production gateway tick calls one narrow injectable coordinator seam. Its
adapter supplies normalized canonical GitHub snapshots, exact-coordinate review
and protected-CI observations, and optimistic Project writes/read-back. The
controller persists and validates every observation, records typed failures,
and performs closeout; network/API activity remains behind the adapter. The
default adapter is paused and performs no external calls.

Terminal closeout additionally requires positive inspection of the persisted
launch identity showing that the worker no longer exists, followed by a clean
index and worktree with no untracked files. Alive or unknown workers are
quarantined as typed ambiguity; dirty tracked or untracked content is a typed
content failure. In either case the reservation, claim fence, and PID remain in
place. Only confirmed-dead, clean closeout recomputes the candidate coordinate,
finishes the guarded transaction, clears execution ownership, and releases the
workspace reservation.

### I7. One-way product authority

Workers do not write Project fields or issue intent. Project projection is
controller-owned and uses expected-version/read-back checks. Worker prompts and
normal tool selection exclude merge, release, deployment, and Project-admin
actions; controller-side verification remains authoritative.

### I8. Control-plane confinement

Evidence-fenced rows cannot be created, decomposed, linked, unlinked, assigned,
reclaimed, moved, completed, archived, or dispatched through generic Kanban
mutation routes by default. The Workflow controller uses explicit opt-in paths
bound to repository, campaign, leaf family/version, run, and expected prior
state. The desktop page is an operator client of that controller, not an
authority in its own right.

The gateway kill switch applies before decomposition, readiness projection,
claim, worktree provisioning, and launch. A generic dispatcher tick or desktop
auto-nudge cannot claim an evidence-fenced leaf.

### I9. Cooperative worker boundary

A leaf worker receives an immutable capsule, one isolated worktree, a bounded
normal tool surface, model access, finite budgets, and a terminal result schema.
Its prompt explicitly excludes successor creation, Project mutation, merge,
release, and deployment. The controller independently validates run identity,
lease state, Git evidence, and terminal output before advancing durable state.
This reduces accidental drift and duplicate work; it is not a sandbox against a
deliberately hostile host-local process.

## Failure classes

Counters and policies are separate:

| Class | Initial policy |
| --- | --- |
| Worker launch/infrastructure | At most two attempts, then inspect infrastructure |
| Content/test failure | One bounded replan, then re-contract or block |
| CI red | One evidence-based flake rerun; otherwise implementation correction |
| Scope ambiguity/owner decision | Zero automatic retries; one decision packet, then quiet wait |
| External dependency | Block until a deterministic clearance condition exists |

A global failure counter may remain for legacy tasks but is not sufficient for Workflow v1 policy.

## Blockers

Closed taxonomy:

- `dependency`: known task/dependency wait;
- `needs_input`: owner-exclusive product, architecture, or policy decision;
- `capability`: unavailable permission, credential, service, or tool;
- `transient`: bounded temporary failure;
- `scope_ambiguity`: specification cannot be interpreted safely;
- `invariant_violation`: execution-kernel contract breached.

Until all types are implemented as native columns, `scope_ambiguity` and `invariant_violation` must be represented as explicit orchestrator quarantine events rather than silently mapped to retryable failures.

If every leaf is owner-gated, notify once, preserve state, and enter low-power idle. No LLM polling loop.

## Pilot parameters

Initial tunable values:

- concurrency: `2` path-disjoint leaves;
- first qualifying evidence target: `10m`;
- evidence-backed lease: `15m`;
- normal leaf decision budget: `25m`;
- then one dependency-linked leaf.

Phase 0 is synthetic and must prove:

- concurrent duplicate creation converges on one task;
- only one run can be claimed;
- stale run ids and tokens cannot mutate the current attempt;
- identical evidence does not renew a lease;
- new in-scope evidence renews it;
- out-of-scope evidence is rejected;
- an expired live-PID evidence lease is either reclaimed after confirmed process
  death or kept expired and ownership-occupying when termination is unconfirmed;
- restart reconciliation selects one canonical attempt;
- the dispatch kill switch remains off;
- a leaf worker cannot create a successor;
- generic Kanban desktop, dashboard, CLI, slash-command, and model-tool routes
  cannot mutate or dispatch an evidence-fenced row;
- a revoked or expired run/token cannot submit evidence or terminal state.

Phase 1 uses two independent path-disjoint leaves. Phase 2 uses one dependent leaf and deliberately exercises base drift, capsule invalidation, and revalidation.

## Measurements

Record:

- ready, claim, spawn, first-evidence, first-passing-test, commit, PR, review, CI, merge timestamps;
- active worker minutes;
- capsule bytes and capsule construction time;
- changed files/lines;
- retries by failure class;
- duplicate-claim prevention;
- stale-spec/base-drift detections;
- restart reconciliation result;
- heartbeat count without artifact change;
- review invalidations by head change;
- owner-gate and low-power-idle transitions.

## Stop conditions

Immediately freeze new dispatch and quarantine the affected attempt if:

- two active attempts for one leaf run concurrently;
- stale or superseded evidence advances state;
- review/CI for a different SHA is accepted;
- a worker creates successors, edits product intent, self-approves, merges, or releases;
- material spec changes proceed without revalidation;
- restart reconciliation cannot determine one canonical attempt;
- a secret or trust boundary is crossed.

An **enforcement defect** means the invariant is correct but implementation failed: fix and rerun the synthetic phase. An **invariant escape** means the contract itself allowed unsafe behavior: redesign before continuing.

## Minimum implementation sequence

1. Add opt-in immutable leaf identity and database uniqueness.
2. Make default claim tokens opaque while preserving host-local PID detection.
3. Add run-fenced verified workspace evidence and evidence lease semantics.
4. Keep generic successor creation blocked by default and give the controller
   one explicit registration path.
5. Add deterministic reconciliation diagnostics and synthetic tests.
6. Add targeted capsule construction and readiness validation.
7. Add the GitHub-native Workflow desktop projection and typed controller API.
8. Prove a restricted worker broker/capability boundary.
9. Run Phase 1 and Phase 2 pilots.
10. Only then enable the replacement dispatch path. Do not resume the old polling jobs.

## Explicit exclusions from the first implementation slice

- arbitrary or automatic product-planning mutation from the desktop page;
- production merge, release, package publication, or deployment;
- re-enabling gateway dispatch or paused cron jobs;
- universal replacement of legacy heartbeat leases;
- pretending prompt instructions provide credential isolation;
- a new model-visible core tool before the DB and CLI seams are proven.
