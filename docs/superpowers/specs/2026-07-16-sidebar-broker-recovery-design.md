# Session Sidebar Broker Recovery Design

Date: 2026-07-16

Status: approved by the user after the live root-cause diagnosis

## Objective

Finish the 30-day Claude/Hermes-to-Codex sidebar rollout and keep continuous
delivery running without exhausting Codex retries, memory, or native app control.
The unified Session Bridge catalog remains authoritative. Native Codex tasks remain
the presentation mechanism required for the Codex left sidebar.

## Confirmed root cause

Session Bridge provider scans, catalog state, leases, signed markers, and the 81
completed native bindings were healthy. The failure was inside the Codex task host:
each repeated rollout heartbeat left a duplicate stdio MCP/tool bundle alive. Seven
bundles accumulated under the active Codex process. Six stale bundles consumed about
2.8 GiB. With only about 2 GiB free, `list_projects` and `automation_update` stopped
returning. After removing only those six stale bundles, `list_projects` returned in
240 ms.

The existing skill also leased a job before checking native project health. The
resource failure therefore consumed five `project_lookup_failed` attempts for the
same source and moved it to `sidebar_failed`, even though no native task was created.

## Considered approaches

### 1. Keep the current heartbeat and add a global process watchdog

This would recover memory but risks terminating MCP processes belonging to unrelated
active Codex tasks. It treats the symptom globally and is rejected as the primary
design.

### 2. Dedicated minimal broker task plus preflight-before-lease

This is selected. A dedicated local Codex project contains project-scoped config that
disables heavyweight stdio MCPs and plugins not used by the broker. GBrain,
MemPalace, and Session Bridge remain enabled. Its one-minute heartbeat invokes only
the sidebar skill. The skill reads sanitized Session Bridge status, performs native
project lookup, and leases only after both preflights succeed.

This confines any per-turn Codex leak to a small broker runtime and prevents native
outages from consuming job attempts. The source tasks themselves still use normal
Codex projects and the user's normal tool configuration.

### 3. Stop native mirroring and expose only the Hermes catalog

This is operationally cheapest but fails the accepted requirement that sessions be
visible in the Codex left sidebar. It remains the fallback catalog, not the primary
delivery surface.

## Selected architecture

### Authoritative catalog and queue

Session Bridge keeps ownership of eligibility, idempotency, worktree identity,
signed markers, leases, retry state, and Codex thread bindings. No Codex database,
global UI state, or packaged application file is edited directly.

### Broker-specific Codex project

The broker project is `C:\Users\diego\Developer\session-sidebar-broker`. It is a
small Git repository with:

- `.codex/config.toml`, disabling `codegraph`, `context7`, `github`, `node_repl`,
  `openaiDeveloperDocs`, and unrelated plugins for this project only;
- GBrain, MemPalace, and Session Bridge explicitly enabled;
- `AGENTS.md`, limiting the task to one deterministic skill invocation and forbidding
  project work, transcript copying, or external app-server creation;
- `README.md`, documenting operation and rollback.

Normal Codex work in `.hermes` and other projects keeps the full global configuration.

### Preflight order

Each broker heartbeat follows this order:

1. Call `session_status` once. Exit silently if there is no pending or retry work. Do
   not lease when Session Bridge is stopped, its watcher is stopped, or a provider
   reports an active degradation reason.
2. Call native `list_projects({})` once and build the canonical local project map.
   If lookup fails, exit without calling `session_sidebar_pending`; no source attempt
   is consumed.
3. Call `session_sidebar_pending(limit=1)` exactly once. If the result is empty, exit
   silently.
4. Reconcile, create if proven absent, wait until indexed and idle, rename, and commit
   under the existing signed-marker safety contract.

### Failed-row recovery

The one failed row has `project_lookup_failed`, no Codex thread ID, no completion
digest, and no visible timestamp. The existing operator-only
`retry_failed_sidebar_job` path resets it only when the exact source and expected
error code match. It is used once after the new preflight skill is installed.

### Resource gate

Before enabling continuous delivery, run at least three empty broker heartbeats and
measure process count and working set under the Codex process. If the dedicated
minimal runtime leaves more than one stale bundle or grows by more than 150 MiB, do
not enable continuous delivery. Add a broker-thread-scoped cleanup hook keyed by the
exact Codex session ID; never install a global reaper.

## Rollout

1. Keep both existing automations paused.
2. Land and install the preflight-first skill.
3. Create and save the minimal broker project and dedicated broker task.
4. Recover the exact failed row through the reviewed operator path.
5. Drain one job at a time and verify signed-marker uniqueness after every commit.
6. Delete the historical rollout automation once no candidates remain.
7. Point the production one-minute heartbeat at the dedicated broker task.
8. Pass the empty-cycle resource gate.
9. Enable continuous registration.
10. Verify one newly meaningful Claude or Hermes session appears and continues from
    its exact source cwd.

## Rollback

- Pause the production heartbeat.
- Disable Session Bridge continuous sidebar registration.
- Leave existing native tasks and catalog links intact.
- Remove the dedicated broker project from Codex if desired.
- Do not delete, archive, or recreate previously bound native tasks automatically.

## Acceptance criteria

- All eligible 30-day Claude and Hermes sessions have one unique native Codex task or
  one explicitly reviewed safety exclusion.
- Native project failure never consumes a sidebar job attempt.
- The one existing `project_lookup_failed` row is recovered without duplicate
  creation.
- The one-minute broker runs in the dedicated minimal project.
- Three empty cycles do not materially grow stale Codex MCP/tool processes.
- Continuous registration is enabled only after the resource gate.
- One new meaningful source appears in the Codex sidebar and continues from the exact
  source cwd.
- GBrain and MemPalace remain available to ordinary Codex tasks.

