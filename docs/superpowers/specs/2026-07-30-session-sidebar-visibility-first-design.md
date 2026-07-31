# Claude-to-Codex Visibility-First Session Inbox Design

Date: 2026-07-30

Status: approved design, pending implementation plan

## Context

Session Bridge already catalogs native Claude Code sessions, assigns durable
source and bridge identities, creates native Codex tasks, and can render a
bounded Continuation Brief with the latest five chronological source messages.
The current production behavior still fails the user-facing requirement:

- some recent Claude Code Desktop sessions never appear in Codex;
- some imported tasks contain only the cryptic Session Bridge registration
  prompt and `REGISTERED` acknowledgement;
- many existing imports are projectless and therefore absent from the
  project-scoped Codex Recents view;
- the scheduled sidebar heartbeat previously targeted ordinary user tasks and
  interrupted unrelated work;
- the raw app-server path can choose a cwd but cannot assign a saved Codex
  Desktop project;
- the installed Codex Desktop `create_thread` operation can assign a saved
  project, but cannot accept or prove runtime workspace roots and has no
  caller-owned idempotency key.

The desired upstream contract is specified in
`2026-07-30-codex-desktop-create-thread-api-request.md` and tracked in
openai/codex#36250. The installed Desktop build does not implement that
contract. A two-stage same-thread resume canary also failed to expose exact
ordered runtime-root proof.

Waiting for the upstream API leaves recent sessions invisible. Creating
projectless app-server tasks preserves source cwd but fails the actual sidebar
requirement. This design therefore chooses a constrained visibility-first
interim path.

## Decision

Create one readable native Codex Desktop task for every eligible Claude source
session inside the existing saved `.hermes` project.

The Desktop task is a Session Inbox mirror:

- Codex project and task cwd are `.hermes`;
- authenticated bridge metadata retains the exact Claude source cwd,
  worktree, Git snapshot, source cursor, and source hash;
- the initial task content is useful on first open: a Continuation Brief and
  the latest five chronological source messages precede the signed provenance
  block;
- the exact source folder is not silently treated as an attached runtime
  workspace;
- discussion, decisions, planning, and context continuation are allowed in the
  mirror;
- file mutation outside the attached `.hermes` workspace requires an explicit
  source-project handoff.

Session Bridge remains authoritative for discovery, eligibility, identity,
reservations, leases, lineage, and recovery. A single pinned Codex-owned broker
task is the only component allowed to perform Desktop task creation and
legacy-task enrichment.

This is an interim compatibility architecture. When Codex Desktop implements
the project-aware idempotent task-creation contract with runtime-root proof,
Session Bridge should adopt that contract and retire the restricted handoff
limitation after a successful live canary.

## Goals

- Show each newly eligible Claude Code Desktop session in Codex within three
  minutes during normal operation.
- Place every newly created import in the saved `.hermes` project and its
  project-scoped Recents list.
- Show a readable summary and latest five chronological messages on first
  open, not a registration-only placeholder.
- Repair authenticated legacy placeholder tasks in place exactly once.
- Recover missing recent and historical eligible sessions without duplicates
  or an arbitrary date cutoff.
- Keep all scheduled delivery activity inside one dedicated pinned broker task.
- Preserve exact source identity and prevent unsafe source-file work.
- Preserve reservations and identities through service, Desktop, or laptop
  crashes.
- Surface scanner, queue, placement, enrichment, and ambiguity failures
  explicitly.

## Non-goals and known limitations

- This design does not attach the exact Claude source cwd as a proved runtime
  workspace root.
- It does not claim that a `.hermes`-rooted task can safely edit files in the
  source project.
- It does not mutate Codex Desktop private databases, rollout files, project
  state, or packaged application code.
- It does not move, delete, archive, fork, or replace existing Codex tasks.
- It does not rewrite an immutable initial registration prompt.
- It does not migrate complete Claude transcripts into Codex.
- It does not replay every source update into an already created task.
- It does not change Codex-to-Claude native visibility, Claude `/resume`
  behavior, or Claude Desktop registry handling.
- Existing projectless tasks remain projectless. They are enriched in place
  when addressable; only new missing mirrors are guaranteed to be under
  `.hermes`.
- The bridge cannot safely replay an ambiguous Desktop creation because the
  installed API lacks an idempotency key. Unresolved ambiguity therefore
  sacrifices liveness rather than duplicate safety.

## Supersession and traceability

This design narrows and deliberately supersedes only the currently blocked
Claude-to-Codex delivery mechanics. Other Session Bridge behavior remains in
force.

| Earlier requirement | Disposition |
|---|---|
| Read provider transcripts without mutating native JSONL | Preserved |
| Unified catalog, canonical source IDs, FTS projection, and lineage | Preserved |
| Meaningful-session eligibility and reverse-loop prevention | Preserved |
| Signed markers, reserve-before-create, and no blind replacement | Preserved |
| Immutable continuation packs and exact source snapshot validation | Preserved |
| Codex targets created only through generic app-server | Superseded for Claude-to-Codex sidebar creation by the supported Desktop `create_thread` boundary |
| App-server creation with exact source cwd | Superseded by `.hermes` Desktop placement for visibility; source cwd remains authenticated metadata |
| Runtime roots include inbox and source cwd | Deferred until the upstream Desktop API can apply and prove them |
| Automatic projectless-task recovery through `thread/fork` | Deferred; legacy tasks are preserved and enriched in place |
| No scheduled Codex heartbeat in the latency path | Superseded by a dedicated pinned broker wake; ordinary user tasks remain excluded |
| No-backlog p95 at most 30 seconds | Replaced for the interim Desktop broker by normal latency under three minutes and an alert at five minutes |
| Native backfill limited to 30 days | Superseded for Claude-to-Codex repair by newest-first recovery of every discoverable eligible source |
| Codex-to-Claude interactive registrar, cost caps, and `/resume` visibility | Unchanged |
| Direct provider state mutation is forbidden | Preserved |
| MemPalace and GBrain outages must not block base bridge operation | Preserved |

The implementation plan must begin with a requirement traceability audit of:

- the July 13 cross-harness implementation plan;
- the July 17 Claude-native visibility design;
- the July 29 sidebar latency remediation design;
- the July 30 Session Inbox placement design;
- the July 30 Desktop API request;
- this specification.

Each relevant requirement must be marked implemented, missing, superseded,
deferred to upstream, or verified by a named test or canary.

Implementation evidence is maintained in
`../audits/2026-07-30-session-sidebar-requirement-traceability.md`.

## Architecture

### Session Bridge authority

Session Bridge remains the sole authority for:

- source discovery and meaningful-session eligibility;
- canonical source, bridge, and delivery identities;
- source cwd, worktree ID, Git root, branch, and HEAD;
- source cursor, source hash, and preview digest;
- signed provenance markers;
- reservations, leases, retry classification, and duplicate prevention;
- canonical source-to-Codex mirror linkage;
- continuation packs, handoff warnings, and divergence state.

The broker cannot independently discover sessions, choose candidates, or
invent task identity. It may act only on one current lease returned by the
sidebar synchronization skill.

### Dedicated Codex broker

Delivery runs only in the pinned broker task:

```text
task ID = 019f9b71-7109-7ed0-943a-d7291190245c
project = C:\Users\diego\Developer\session-sidebar-broker
title = Fix Claude session translation
```

Before acquiring a lease, every wake verifies the exact task ID, local host,
saved project, and cwd. A mismatch stops the wake without leasing or creating
anything.

The broker invokes the sidebar synchronization skill exactly once per wake and
processes at most one lease. It performs no unrelated project work, transcript
summarization, or source mutation.

### Visibility mirror

For a new eligible source, the broker calls the native Desktop task creation
operation once with:

- target type `project`;
- the exact saved local `.hermes` project ID discovered from Desktop;
- local environment;
- one deterministic readable registration payload;
- the configured model and thinking values only when explicitly required by
  the existing broker contract.

The request omits unsupported runtime-root and idempotency fields. It does not
fall back to app-server creation, external app-server creation, direct Codex
state mutation, fork creation, or replacement creation.

The returned native task ID is bound to the reservation immediately. The
bridge then verifies the task through normal Desktop inventory and read
surfaces before committing its canonical mirror link.

### Readable content contract

The first task prompt renders sections in this order:

1. bounded Continuation Brief;
2. exactly the latest five available source messages in chronological order;
3. explicit source cwd and safe-handoff warning;
4. source cursor/hash and preview digest metadata;
5. signed Session Bridge provenance marker;
6. the continuation instruction for the first later substantive user message.

Registration-only instructions never lead the visible payload. Tool results
and non-text blocks use bounded readable representations. Hidden reasoning,
secrets, bearer values, signing keys, and raw unbounded tool payloads are
excluded.

If fewer than five eligible messages exist, all available messages are shown.
The preview builder is deterministic for the same source snapshot.

### Continuation and filesystem safety

On the first later substantive user message, the same Codex task calls
`session_continue` using the authenticated source identity. The bridge refreshes
or reuses the immutable continuation pack and validates the latest source
snapshot.

The mirror may continue:

- discussion and explanation;
- requirements, decisions, and planning;
- transcript inspection and summarization;
- non-mutating context work.

Before any command or file mutation, the continuation must determine whether
the target lies inside an attached verified workspace. Because the interim
mirror proves only `.hermes`, work against an external Claude source folder
stops and offers an explicit source-project handoff. It never silently changes
cwd, broadens permissions, or creates a second mirror.

## Durable delivery model

One reservation is keyed by:

```text
(source_provider, source_session_id, target_provider, delivery_generation)
```

The reservation retains the candidate snapshot, prompt digest, marker digest,
source cwd, source cursor/hash, and any bound Codex task ID.

The logical state flow is:

```text
pending
  -> leased
  -> dispatch_reserved
  -> bound
  -> verified
  -> visible
```

Definite failures before Desktop dispatch may return to `retry`. Once Desktop
dispatch begins:

- a returned task ID is persisted before any other operation;
- an exact authenticated task may be reconciled and verified;
- uncertainty without a provable task identity becomes `needs_attention`;
- `needs_attention` never authorizes replacement creation.

The bridge maintains one canonical source-to-Codex link. A second task with the
same authenticated source marker is a fatal identity conflict, not a candidate
to select heuristically.

## Discovery and scheduling

### Source discovery

Claude discovery runs incrementally at least once per minute. A source becomes
eligible only after it contains substantive user/assistant content under the
existing meaningful-session classifier. Registration-only, automation-only,
subagent-only, empty, and bridge-origin loop candidates remain excluded.

A failed transcript does not abort unrelated changed-session indexing. Its
native ID remains pending for retry, the durable cursor does not skip it, and
provider health becomes degraded with a bounded fixed reason.

### Wake model

The installed Codex automation boundary can create and update recurring
heartbeats for an exact task, but exposes no repository-callable operation that
Hermes can use to trigger one immediately. The interim scheduler therefore
runs one heartbeat every minute against the exact pinned broker task.

No automation may target the currently focused task, an ordinary user task, or
a dynamically selected task.

Each wake processes at most one lease. Remaining pending work is processed by
the next one-minute wake. An empty wake ends silently. Three minutes without a
persisted broker heartbeat is stale and alerts; true queue-transition-triggered
wakeup remains deferred until Codex exposes a supported trigger-now API.

Normal end-to-end source-to-visible latency is under three minutes. An oldest
eligible reservation older than five minutes is an operational failure and
must alert.

## Verification

A new mirror is committed only after all available installed-API proofs pass:

- exact returned or reconciled Codex task ID;
- local host;
- exact saved `.hermes` project ID;
- task cwd equal to the canonical `.hermes` project folder;
- exact signed source marker;
- prompt digest matching the reserved readable preview;
- source metadata matching the reserved Claude identity and cwd;
- readable Continuation Brief and chronological last-five section;
- persistent task readability;
- no active turn, approval request, user-input request, or system error.

Because the installed Desktop API cannot prove runtime workspace roots, their
absence is recorded as the known interim restriction rather than falsely
reported as success.

## Legacy placeholder enrichment

An existing placeholder task is eligible for automatic enrichment only when:

- its exact Codex task ID is already bound;
- its marker authenticates the durable source and bridge;
- it contains no substantive Codex project work;
- it has no prior committed enrichment packet;
- it is quiescent;
- the source transcript remains available.

Hermes sends one bounded bridge-maintenance packet to the same task containing
the Continuation Brief and current latest five chronological messages. The
packet includes a deterministic enrichment digest and signed marker, forbids
project work and `session_continue` during maintenance, and requests one fixed
acknowledgement.

The send is reserved durably before dispatch. An uncertain send is reconciled
against the exact enrichment marker and digest. It is never resent merely
because the immediate response was lost.

The original registration prompt and task identity remain unchanged. No
replacement task is created. A projectless legacy task remains projectless and
is reported as such.

## Failure handling and recovery

- Scanner failure preserves the last successful cursor, reservations, and
  queue. Bounded backoff applies without advancing past failed source IDs.
- An unreadable or incomplete source remains pending. The registration
  placeholder is never substituted for actual source content.
- Broker task, project, cwd, or host mismatch stops before lease acquisition.
- Desktop rejection before dispatch may retry through the same reservation.
- A returned task ID is always bound before rename, verification, or commit.
- Lost or ambiguous creation searches only for the exact signed marker and
  source identity. It never invokes another create.
- Unresolved creation ambiguity enters `needs_attention` indefinitely and
  alerts.
- A verification mismatch quarantines the reservation and preserves the
  existing Codex task.
- Expired leases after a crash are recovered only after checking bound identity
  and ambiguity state.
- A missing source folder does not hide an otherwise readable mirror, but it
  disables source-file handoff until the folder can be validated.
- A missing source transcript prevents enrichment and is reported with
  canonical source and target IDs.
- Raw transcript content, signed markers, opaque lease tokens, and provider
  exception text do not appear in public health output.

## Observability

Health and status expose bounded operational data for:

- last successful Claude scan and scan lag;
- failed source count and fixed degraded reason;
- eligible, pending, leased, retry, ambiguous, needs-attention, and visible
  counts;
- oldest eligible and oldest pending ages;
- source-to-index, index-to-queue, queue-to-visible, and source-to-visible
  latency;
- exact pinned broker identity and last successful broker wake;
- `.hermes` project discovery and placement verification status;
- legacy placeholder enrichment pending, committed, ambiguous, and failed
  counts;
- projectless legacy count;
- duplicate source, bridge, marker, and Codex task identity counts.

Status never exposes source messages, secrets, full markers, or unrestricted
native paths. The configured `.hermes` inbox path and canonical IDs may be
shown.

## Testing

Implementation follows test-driven development and uses the repository's
required `scripts/run_tests.sh` wrapper.

### Unit and store tests

- substantive eligibility excludes registration-only placeholders;
- summary and latest-five rendering are deterministic and chronological;
- source cwd and inbox cwd remain distinct;
- one reservation and one canonical link exist per source;
- state transitions reject blind retry after dispatch;
- lease expiry and crash recovery reconcile bound identity;
- legacy enrichment is committed at most once;
- continuation file mutation is blocked outside verified workspaces;
- health metrics omit sensitive data.

### Desktop boundary tests

- the exact `.hermes` saved project is selected;
- unsupported runtime-root and idempotency fields are not fabricated;
- one lease causes at most one Desktop create call;
- the returned task ID is persisted immediately;
- project, cwd, marker, preview digest, and readable sections are verified;
- broker identity mismatch prevents lease and creation;
- ambiguous creation never replacement-creates;
- existing placeholder enrichment targets the exact same task once.

### Fault-injection tests

- service crash before lease, after lease, before dispatch, after dispatch,
  after task-ID binding, and before commit;
- Desktop response loss with and without an exact recoverable task;
- scanner partial failure and cursor preservation;
- broker wake loss with watchdog recovery;
- enrichment send ambiguity;
- missing source cwd and missing transcript;
- duplicate marker and conflicting source identity.

### Production canaries

1. Run a dry inventory with no native mutation.
2. Create one fresh Claude Desktop source with at least five messages.
3. Verify one `.hermes` Codex task, readable summary, chronological latest
   five, marker, lineage, and visibility within three minutes.
4. Enrich one existing placeholder-only task in place and verify no second
   task is created.
5. Deliver five recent missing sessions and verify exact uniqueness and
   placement.
6. Restart Session Bridge and Codex during controlled reserved states and
   verify reconciliation without duplicates.

## Rollout

1. Complete and publish the cross-document traceability audit.
2. Ship schema, status, and dry-run inventory support with mutation disabled.
3. Run focused tests, fault injection, and the fresh live canary.
4. Run one legacy enrichment canary.
5. Deliver and manually inspect the five newest missing sessions.
6. Enable continuous delivery for newly eligible sessions.
7. Recover every discoverable eligible missing source newest-first.
8. Enrich every addressable authenticated placeholder-only task newest-first.
9. Run restart and ambiguity canaries.
10. Observe a clean production soak and run the full Session Bridge suite.

Rollback disables new leases and the dedicated heartbeat automation while
preserving reservations, task bindings, mappings, enrichment state, and every
created native task. It never deletes tasks or discards source sessions.

## Acceptance criteria

- Every discoverable eligible Claude session is accounted for as visible,
  explicitly excluded, or blocked by a named failure.
- Every newly created mirror appears under the saved `.hermes` project.
- New mirrors open with a Continuation Brief and latest five chronological
  source messages before signed provenance metadata.
- Normal new-session visibility latency is under three minutes.
- Eligible work older than five minutes alerts with a fixed stage and reason.
- Every source has at most one canonical Codex task.
- Repeated scans, restarts, and ambiguous responses create no duplicates.
- Every addressable authenticated legacy placeholder receives at most one
  readable enrichment packet.
- No ordinary Codex task is awakened by sidebar delivery automation.
- The dedicated broker processes at most one valid lease per wake.
- Context continuation remains in the same task.
- File changes outside a verified attached workspace require an explicit
  source-project handoff.
- Provider transcripts and Codex private state remain unmodified.
- The cross-document traceability audit has no silently unresolved relevant
  requirement.
- Focused, fault-injection, live-canary, and full Session Bridge tests pass
  through the required wrapper.

## Upstream transition

Adoption of openai/codex#36250 requires a fresh design and live gate proving:

- project-aware creation accepts ordered inbox/source runtime roots;
- Desktop returns actual project, cwd, and root proof;
- one stable idempotency key survives retry and Desktop restart;
- same-key replay returns the same task;
- a source-project operation is contained within the verified runtime roots;
- no ordinary task interruption or duplicate creation occurs.

Until that gate passes, this visibility-first design remains intentionally
restricted to `.hermes`-safe work plus explicit source-project handoff.
