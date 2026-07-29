# Session Sidebar Latency Remediation Design

## Context

The cross-harness bridge is functionally delivering native Claude Code and
Hermes sessions into the Codex sidebar, but the delivery latency is not
acceptable. Live inspection on 2026-07-28 showed:

- Claude source activity could take 50–315 seconds to become a sidebar job.
- A queued job could take 90–1,434 seconds to become a visible Codex task
  during a burst.
- The source scanner reported `claude_scan_failed` and
  `claude_refresh_failed`.
- Native delivery safely claimed one job at a time.
- Each registration ran a real Codex model turn under the user's normal Codex
  configuration, starting MCP and plugin processes that are irrelevant to the
  required `REGISTERED` response.

The service's three-second catalog interval is therefore not the dominant
latency. The delays come from source refresh failures plus a slow serialized
registration operation.

## Goals

1. Make a newly meaningful Claude Code or Hermes session visible in Codex
   within 30 seconds at p95 when no backlog exists.
2. Drain a burst of ten eligible sessions within three minutes on the current
   workstation.
3. Preserve the single-writer, reserve-before-create, no-blind-retry safety
   invariants.
4. Preserve normal Codex capabilities when the user later opens an imported
   task.
5. Expose enough stage timing to distinguish indexing, queueing, and native
   registration delays.

## Non-goals

- Empty Hermes Desktop drafts will not be projected. Hermes intentionally
  persists a session only after the first meaningful prompt so abandoned
  drafts do not create empty history.
- Automation-only and subagent-only sessions remain excluded.
- This change does not redesign transcript hydration, continuation, task
  identity, or the signed-marker format.
- This change does not increase native-create concurrency until a lean
  single-writer registrar has been measured.

## Considered Approaches

### Lower the scan interval

Rejected as the primary remedy. The scanner already runs on a short interval,
and measured delays are much larger than the interval. More scans would add
contention without reducing the synchronous registration cost.

### Add parallel native creation

Deferred. Parallel creation could improve burst throughput, but it increases
the ambiguity surface around native task creation and recovery. It is
unnecessary if the registration operation is reduced to a few seconds.

### Use a lean registration runtime

Selected. The registration task needs to create one durable Codex task, record
the signed marker, receive the exact acknowledgement, and prove persistence.
It does not need MCP servers, plugins, skills, shell tools, browser tools, or
project work. The broker will use a registration-only app-server configuration
for that operation while retaining the user's real `CODEX_HOME`.

## Architecture

### Registration runtime profile

Add one focused helper that builds the app-server session-layer overrides for
sidebar registration. The profile will:

- disable all configured MCP servers for the broker-owned app-server process;
- disable plugin loading for that process;
- retain the real `CODEX_HOME`, authentication, session storage, cwd, and
  thread source;
- keep the existing signed registration prompt and exact acknowledgement;
- keep the existing fresh-process persistence verification.

The override is process-local. It must not write to the user's
`config.toml`. A characterization test must prove that a task registered by
the lean process resumes from a normal app-server process with the user's
normal capabilities. If Codex persists the lean configuration into the task,
the implementation must restore normal configuration through `thread/resume`
before committing visibility; it must not ship a task whose later turns are
permanently tool-restricted.

The model and service tier remain the user's normal defaults in the first
patch. Persistently changing a task's model merely to accelerate registration
would be surprising. A model override may be considered only after proving
that normal configuration is restored before handoff.

### Source discovery priority

Keep the existing complete catalog and durable cursor, but ensure each
recovery cycle probes the newest changed meaningful sessions before catch-up
work. A changed Claude transcript or newly persisted Hermes row must be
eligible for queueing without waiting behind old refresh reconciliation.

Claude scan failures remain fail-closed, but one malformed or concurrently
changing transcript must not abort the entire changed-session batch. The
scanner will preserve the failed native ID for retry, record a bounded error
code, and continue indexing the other changed sessions.

### Delivery scheduling

Keep `limit == 1` for native creation. Once an operation returns
`visible`, `retry`, or a terminal result, the continuous recovery worker
immediately takes the next job. No scheduled Codex heartbeat participates in
this path.

Concurrency is a follow-up gate, not part of this change. It may be enabled
only if a ten-session burst still misses the three-minute acceptance target
after the lean runtime is deployed.

### Hermes session semantics

The bridge-visible boundary is the first meaningful persisted user prompt,
not the act of opening an empty composer. Profile databases remain part of
candidate discovery. The implementation will add a regression test proving a
new profile session with a meaningful user message is discovered and queued;
it will also preserve the exclusion of empty drafts, cron sessions, and
subagents.

### Observability

Persist or derive four timestamps for each delivery:

1. `source_active_at`
2. `indexed_at`
3. `queued_at`
4. `native_visible_at`

Status output will report bounded recent p50/p95 values for:

- source-to-index;
- index-to-queue;
- queue-to-visible;
- end-to-end source-to-visible.

The status surface must not expose transcript text, native paths, tokens, or
signed markers.

## Error Handling

- Registration process startup failure: retry with the existing bounded
  backoff and no replacement creation.
- Native create ambiguity: preserve the exact reserved identity and use the
  existing reconciliation path.
- Lean-to-normal handoff cannot be proven: do not commit the task as visible;
  return a bounded retry code and preserve the created task.
- One Claude transcript refresh fails: retain that native ID as pending,
  continue the batch, and report the provider as degraded.
- Profile identity collision: continue to fail closed; never guess which
  Hermes profile owns the session.

## Testing

The implementation follows test-driven development.

1. Unit-test the registration app-server argument/config builder.
2. Prove the sidebar executor uses the lean factory for create/register and a
   normal fresh client for persistence and handoff verification.
3. Characterize a real local app-server registration in an isolated test
   fixture and assert no local MCP child processes are started.
4. Resume the resulting task with a normal app-server client and prove normal
   effective configuration is restored.
5. Reproduce a changed Claude batch containing one failing transcript and
   prove the other changed transcripts still index.
6. Prove a meaningful Hermes profile session is queued ahead of historical
   catch-up candidates.
7. Run the focused session-bridge suite, then the repository's required test
   wrapper for all session-bridge tests.
8. Restart the service and run a live canary plus a ten-session burst
   measurement. Do not declare success unless both the duplicate-safety
   invariants and latency targets pass.

## Acceptance Criteria

- No-backlog p95 source-to-visible latency is at most 30 seconds.
- A ten-session burst drains within three minutes.
- No duplicate Codex tasks are created.
- No broker registration process starts configured local MCP subprocesses.
- Imported tasks resume with the user's normal Codex capabilities.
- A meaningful Hermes profile session appears after its first prompt.
- Empty Hermes drafts, cron sessions, and subagents remain absent.
- The status endpoint exposes stage latency without sensitive data.

