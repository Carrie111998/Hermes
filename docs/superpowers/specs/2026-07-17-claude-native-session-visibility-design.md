# Claude Native Session Visibility Design

Date: 2026-07-17

## Objective

Make meaningful native Codex and Hermes sessions visible and resumable inside
Claude Code while preserving Session Bridge as the authoritative cross-harness
catalog and lineage store.

Acceptance has two surfaces:

1. A genuine Claude Code session appears in the native `/resume` picker. The
   user can press `Ctrl+A` to view mirrored sessions from every project.
2. A `/session-bridge` Claude Code command exposes the complete unified catalog
   with search and exact continuation.

The initial rollout covers meaningful sessions active during the previous 30
days, followed by continuous registration. Native creation is limited to 25
sessions per day.

## Platform Constraint

Claude Code stores local CLI sessions per project under
`~/.claude/projects/<project>/<session-id>.jsonl`. Its native session picker
excludes sessions created with `claude -p` or the Agent SDK. The existing
`ClaudeTargetAdapter` uses `--print`, so those placeholders can be resumed by
exact ID but cannot satisfy native picker visibility.

The bridge must therefore create a real interactive Claude session through a
terminal-compatible process. It must not synthesize or edit Claude JSONL files.
That format is not a supported injection API and direct writes would make
correctness depend on undocumented implementation details.

## Scope

### Included

- Native Codex and native Hermes source sessions.
- At least one meaningful user request under the existing deterministic
  eligibility policy.
- Exact original cwd/worktree and recorded Git identity.
- Native `/resume` visibility with `[Codex]` and `[Hermes]` names.
- Unified catalog search and exact continuation from Claude Code.
- A reviewed 30-day backfill and continuous registration within one minute.
- Haiku-only, tools-disabled registration turns.

### Excluded

- Native Claude sources, bridge placeholders, and bridge continuations. This
  prevents reverse loops.
- Automation-only, subagent-only, acknowledgement-only, and session-control
  conversations.
- Direct mutation of Claude transcript files.
- Mirroring full source transcripts into the registration turn.
- A fabricated global Claude sidebar. The CLI's native surface is `/resume`;
  the VS Code extension and Claude desktop maintain separate histories.

## Architecture

### Session Bridge authority

Session Bridge remains authoritative for:

- source discovery and meaningful-session eligibility;
- deterministic bridge and idempotency identities;
- source cwd/worktree and Git snapshot;
- signed lineage markers;
- leases, retries, failure classification, and duplicate prevention;
- immutable continuation packs and catalog relationships.

Claude-native delivery uses its own additive job state. It must not reuse or
couple transitions to `session_sidebar_jobs`, which remains specific to Codex.

### Claude delivery jobs

Add a `session_claude_visibility_jobs` table with one row per eligible source.
The durable fields include:

- job ID and idempotency key;
- source session ID and bridge ID;
- deterministic reserved Claude UUID;
- state, attempt count, next-attempt time, and fixed error code;
- lease digest and expiry;
- completion digest;
- eligible, created, updated, and visible timestamps.

The reserved Claude UUID is persisted before native process launch. A unique
constraint covers source session ID, bridge ID, idempotency key, and reserved
Claude UUID independently.

States are `claude_pending`, `claude_leased`, `claude_retry`,
`claude_visible`, and `claude_failed`.

### Single registrar

A dedicated local registrar is the only delivery worker. It:

1. performs provider, authentication, cost-cap, and cwd preflight checks;
2. claims exactly one valid lease;
3. reconciles the reserved Claude UUID before any creation;
4. launches Claude Code interactively in the exact source cwd through a Windows
   pseudoterminal;
5. passes the reserved UUID, deterministic name, Haiku model, no tools, and
   `dontAsk` permission mode;
6. sends the fixed signed registration prompt;
7. requires the exact `REGISTERED` response;
8. sends `/exit` and waits for a bounded clean exit;
9. re-reads the native transcript with `ClaudeSourceAdapter` and verifies the
   UUID, cwd, name, marker, origin, and project placement;
10. commits the exact reserved UUID as visible.

The registrar never performs project work and never includes source transcript
content beyond the bounded title and signed metadata.

### Claude Code catalog command

Install a personal Claude Code plugin command named `/session-bridge`. It uses
the authenticated Session Bridge MCP to:

- browse or search all Claude, Codex, and Hermes sessions;
- show provider, title, cwd, activity time, mirror state, and preview;
- inspect a selected session;
- continue the selected session into the current Claude mirror.

The command does not replace `/resume`. It is the global catalog surface;
`/resume` remains the native picker for registered Claude sessions.

## Identity and Naming

The source/provider pair produces a deterministic idempotency key and bridge
ID. The Claude UUID is derived deterministically from the bridge identity in a
versioned UUID namespace and is reserved in the database before launch.

Native names are:

- `[Codex] <bounded original request>`
- `[Hermes] <bounded original request>`

Names use the existing normalization, redaction, whitespace compaction, and
length limit applied to Codex sidebar titles.

The registration prompt contains:

- a signed Session Bridge marker;
- canonical source session ID and bridge ID;
- source provider;
- exact cwd/worktree and recorded Git identity;
- the instruction to reply exactly `REGISTERED` without tools;
- the instruction to call `session_continue` only after the first subsequent
  substantive user request.

## Visibility and Continuation Flow

Sessions are created in their original cwd. Consequently:

- `/resume` shows mirrors for the current project or worktree;
- `Ctrl+W` widens to repository worktrees when available;
- `Ctrl+A` widens to all projects on the laptop;
- `/session-bridge` always searches the complete catalog.

Opening a placeholder performs no automatic hydration or filesystem work. On
the first substantive user request, Claude calls `session_continue` with the
signed source identity. Session Bridge then:

1. verifies the native Claude target and signed lineage;
2. revalidates the exact cwd/worktree and Git identity;
3. freezes or reuses an immutable context pack;
4. hydrates the continuation relationship;
5. changes the catalog state from `mirrored` to `continued`.

If exact filesystem continuity is unsafe, Claude reports the mismatch and
blocks edits rather than silently changing directories.

## Cost Controls

Registration uses Haiku, disables all tools, and permits only one fixed reply.
The registrar exits immediately afterward.

Two independent limits apply:

- no more than 25 successful or attempted native registrations per local day;
- an emergency dollar threshold configured independently of the daily count.

Reconciliation and read-only health checks do not consume a registration slot.
Once either limit is reached, the registrar leaves remaining jobs pending until
the next permitted window and emits one durable fixed status code. It does not
spin, repeatedly invoke Claude, or spend tokens on eligibility decisions.

## Reliability and Failure Handling

Only one worker may own a delivery lease. No backfill batch may be queued while
pending, leased, retry, or failed work exists.

After any launch timeout or unknown process result, the job becomes
`creation_ambiguous`. Recovery checks the exact reserved UUID and signed marker.
A missing or delayed picker result never authorizes a replacement UUID.

Known transient failures receive bounded backoff:

- Claude executable unavailable;
- Claude authentication unavailable;
- desktop or pseudoterminal unavailable;
- native transcript not indexed yet;
- clean exit not observed;
- Session Bridge temporarily unavailable.

Fatal failures pause delivery:

- UUID, source, bridge, provider, cwd, name, or marker conflict;
- duplicate native UUID or idempotency identity;
- unknown retry code;
- any `claude_failed` row.

Every lease ends in exactly one commit or fail/release transition. Restarting
the service or registrar preserves the reserved UUID and resumes exact-ID
reconciliation.

## Backfill and Continuous Registration

The initial dry-run considers 30 days and applies the same meaningful-session
classifier as Codex visibility. Review every exclusion before applying a batch.
The registrar processes no more than the remaining daily allowance and never
queues another batch while work is open.

After the backfill reaches zero candidates and completes a clean soak,
continuous discovery registers new eligible Codex and Hermes sessions within
one minute. Discovery may enqueue immediately; native delivery remains subject
to the single-worker lease and cost gates.

## Testing

### Unit and store tests

- eligibility and reverse-loop exclusions;
- deterministic job, bridge, idempotency, and UUID identities;
- additive schema migration and uniqueness constraints;
- lease, bind, retry, commit, and failure transitions;
- daily and emergency cost caps;
- fixed error-code validation;
- exact marker, name, cwd, and UUID checks.

### Pseudoterminal integration tests

A fake interactive Claude executable proves:

- exact argv and cwd;
- one registration prompt and exact reply handling;
- `/exit` and clean-exit behavior;
- timeouts and ambiguous creation;
- restart reconciliation without replacement creation;
- no tool invocation and no source transcript leakage.

These tests spend no Anthropic tokens.

### Real characterization

One disposable real session validates the installed Claude Code version:

- interactive creation appears in `/resume`;
- `Ctrl+A` exposes the cross-project entry;
- the deterministic name and UUID survive Claude restart;
- exact UUID resume works;
- cleanup removes only the characterized session after identity verification.

### End-to-end acceptance

Roll out one Hermes canary and one Codex canary. For each, verify exact source,
bridge, idempotency, UUID, name, cwd, marker, native visibility, unified catalog
relationship, and first-message hydration.

Final acceptance requires:

- zero pending, leased, retry, or failed jobs;
- exact uniqueness of visible source ID, bridge ID, idempotency key, and Claude
  UUID;
- zero remaining reviewed 30-day candidates;
- at least one healthy empty registrar cycle;
- one new meaningful source registered continuously within one minute;
- successful native `/resume` and `/session-bridge` discovery;
- successful continuation with an immutable context pack;
- healthy Session Bridge, MemPalace, and GBrain MCP endpoints;
- a 30-minute clean soak and full regression suite.

## Rollout

1. Implement schema, store, policy, and pseudoterminal registrar behind a
   disabled feature flag.
2. Add the Claude Code plugin command and install it locally.
3. Run all unit, integration, fault-injection, and characterization tests.
4. Deploy through the canonical Session Bridge launcher.
5. Register one Hermes canary and one Codex canary.
6. Perform reviewed 30-day backfill batches under the 25/day cap.
7. Enable continuous registration after a clean empty cycle.
8. Run the final soak, write MemPalace and GBrain checkpoints, and remove the
   temporary rollout supervisor.

## Non-Negotiable Safety Properties

- Never write Claude transcript JSONL directly.
- Never create without one valid lease and one persisted reserved UUID.
- Never create a replacement after creation ambiguity.
- Never hydrate or touch project files during registration.
- Never broaden cwd or Git identity silently.
- Never exceed the daily count or emergency cost threshold.
- Never allow native Claude mirrors to re-enter eligibility as new sources.
