# Hermes enhancement control plane

Status: active
Branch: `feat/hermes-enhancement-control-plane`
Base: `origin/main` at `2b0fb72acae67f51652de5c51db556bc15a68f0e`
Worktree: `C:/Projects/hermes-agent.wt/enhancement-control-plane`

## Operating contract

- Read `AGENTS.md` and any scoped `AGENTS.md` before each change.
- Never edit the installed/primary checkout at `C:/Users/auron/AppData/Local/hermes/hermes-agent`.
- Reconcile the roadmap against current `main` before implementing; current main changes rapidly.
- Extend existing seams. Do not add permanent core-tool schemas unless no higher Footprint Ladder rung works.
- Preserve byte-stable system prompts and toolsets for the life of a conversation.
- One bounded change per loop tick. Add behavior tests first, then implementation, then focused verification.
- Never commit, push, merge, or modify the live `agy` profile without explicit user direction.
- Mark unsupported assumptions as rejected instead of coding them.

## Current-main reconciliation

| Roadmap item | Current disposition | Next action |
|---|---|---|
| Context inspector | Implemented: `agent/context_breakdown.py`, CLI `/context [all]`, gateway tests | Verify Desktop parity and measured accuracy; do not duplicate |
| Usage/cost accounting | Implemented: canonical provider usage, session/insights surfaces | Add only missing attribution backed by a reproduced gap |
| Deferred MCP/plugin tools | Implemented | Preserve prompt-cache behavior |
| Progressive disclosure for core tools | Intentionally absent | Benchmark a static, session-start core-tool manifest before proposing any change; never mutate mid-session |
| Per-task auxiliary routing | Implemented: `auxiliary.<task>` resolution | Extend toward explicit policy receipts only after a failing routing case is reproduced |
| Provider truth | Partial: primary registry plus localized compatibility maps | Reconcile duplicated mappings without breaking compatibility paths |
| Delegation completion contracts | Substantially implemented | Reproduce dropped/unowned delivery or non-terminal completion before patching |
| Memory provenance | Implemented for memory-write bridge and skill provenance | Audit retrieval provenance and conflict/staleness visibility |
| Native A2A protocol | Absent | Opt-in plugin or MCP adapter only; not a core tool |
| Windows update handoff | Implemented with updater marker, backend tree-kill, lock wait, state backup | Improve exact external lock-owner diagnostics and transactional verification after reproduction |
| Durable graph orchestration | Kanban links/dependencies and delegation exist; standardized reasoning DAG engine absent | Build opt-in graph layer over stable traces, not a speculative core framework |

## Evidence added 2026-07-27

- X post: https://x.com/beamnxw/status/2081670690223030343
- Primary paper: https://arxiv.org/abs/2505.24354
- Paper: *Unifying Language Agent Algorithms with Graph-based Orchestration Engine for Reproducible Agent Research* (AGORA).
- AGORA contributes modular graph workflows, reusable reasoning algorithms, memory/state routing, and standardized evaluation. Its experiments also report that simpler Chain-of-Thought can be robust at substantially lower computational overhead. Therefore graph execution must remain opt-in and benchmarked against the simplest working loop.
- Social claim requiring correction: https://x.com/RoundtableSpace/status/2081707421756457213 says Andrej Karpathy "released" a 12-page self-improving multi-agent graph guide. The linked `Karpathy-Graph-Engineering-Systems.pdf` is not an authoritative Karpathy publication: the visible document/reply disclaimer says it is independently compiled, not affiliated with Andrej Karpathy, and not endorsed by him. Treat its graph ideas as third-party inspiration only; do not cite it as Karpathy evidence or use it to override the AGORA primary paper and Hermes acceptance gates.
- Recovered the exact previously-unextractable post https://x.com/RoundtableSpace/status/2081684772464513281 through both the live X page and FxTwitter's status API. Its full text is: “ANTHROPIC JUST RELEASED A FREE 2-HOUR WORKSHOP ON BUILDING SELF-IMPROVING AGENT GRAPHS.” The post is authored by `RoundtableSpace`, sourced from Twitter Web App, and carries a 9,433.2-second (2:37:13) video rather than a two-hour video. A visible frame is co-branded Anthropic and LangChain Academy, but no authoritative Anthropic publication URL was established. Treat this as a third-party social pointer, not primary evidence, until the original workshop page is identified.

## Prioritized implementation queue

### P0-A: Windows update lock-owner diagnostics

Goal: when the venv shim remains locked after the bounded teardown, report exact candidate external holder PIDs and executable paths without exposing command-line secrets.

Done when:

- The update failure distinguishes desktop-owned teardown failures from external holders.
- Candidate PID, parent PID, executable basename/path, and ownership category are logged and surfaced safely.
- Command lines, environment variables, tokens, and credentials are never emitted.
- Pure parsing/classification functions have Windows and failure-path tests.
- Existing update-marker, updater-process, Windows path, and Desktop typecheck suites pass.

### P0-B: Update transaction verification

Goal: prove update activation, backend readiness, WebSocket readiness, active profile, and rollback state before success is shown.

Done when:

- Every update stage has an explicit terminal state and durable receipt.
- Fault injection covers updater spawn, venv mutation, Desktop build, activation, and first boot.
- Profiles, sessions, cron, configuration, and credentials remain untouched in fixture homes.

### P1-A: Provider registry reconciliation

Goal: remove duplicated provider facts only where current compatibility behavior can be preserved by one resolved provider descriptor.

Done when:

- A behavior test demonstrates the current drift.
- CLI, gateway, auxiliary routing, and Desktop resolve the same provider/model relationship.
- Unknown/custom pricing remains `unknown`, never zero.

### P1-B: Routing policy receipts

Goal: expose why a main/auxiliary/subagent route selected a model/provider, its budget, fallback, and result.

Done when:

- Existing explicit `auxiliary.<task>` configuration remains authoritative.
- Every automatic route has a deterministic reason code.
- No silent fallback occurs.
- SOL/TERRA/LUNA and all-model policies are configuration, not hardcoded model families.

### P1-C: Context accuracy and attribution

Goal: verify `/context` estimates against actual request payload accounting and expose discrepancies without changing the prompt.

Done when:

- Context categories reconcile within an explicit tolerance against serialized payloads in tests.
- Cache-read/write/input/output accounting stays provider-canonical.
- No prompt or toolset mutation occurs mid-conversation.

### P2-A: Durable graph orchestration plugin

Goal: an opt-in graph layer that composes existing agent, delegation, Kanban, verification, approval, and checkpoint primitives.

Initial schema:

- Node: deterministic function, LLM call, specialist profile, tool, verifier, or human gate.
- Edge: sequence, condition, retry, parallel fan-out, join, escalation, or terminal transition.
- State: typed versioned payload with explicit merge rules.
- Receipt: node input hash, output artifact, model/provider, cost, latency, verifier result, and transition reason.
- Stop: success evidence, budget exhaustion, timeout, irrecoverable failure, or human escalation.

Done when:

- Kill/restart resumes from a committed checkpoint without duplicate side effects.
- A model ending its turn is not accepted as workflow completion.
- Parallel joins are deterministic and idempotent.
- CoT, ReAct, and one tree/graph reasoning strategy can be expressed as data.
- Benchmarks compare graph strategies against a simple single-agent/CoT baseline for quality, cost, and latency.

### P2-B: Mission Control

Goal: inspect the graph, node states, ownership, costs, artifacts, retries, and blockers using existing backend truth.

Done when:

- UI never becomes a second chat implementation.
- Background events do not steal focus.
- Every displayed state maps to a durable backend record.

### P3: Trust and interoperability

- Retrieval provenance/conflict/staleness visibility.
- Opt-in A2A adapter after protocol and threat-model verification.
- RBAC at gateway/execution boundaries, not in tool schemas.
- Worktree transactions plus independent verification receipts.

## Loop tick protocol

1. Acquire the external loop lock supplied by the cron pre-run script. If busy, make no changes.
2. Read this file and current `git status`/`git diff`.
3. If an unverified prior edit exists, verify or repair it before selecting new work.
4. Select the first pending item whose implementation and focused tests fit the tick.
5. Reproduce or write a failing behavior test before implementation.
6. Apply one surgical change.
7. Run focused tests plus syntax/type checks for touched files.
8. Update this plan with timestamped evidence, exact commands, exits, and remaining blocker.
9. Remove the loop lock only if this tick acquired it.

## Progress log

### 2026-07-27 13:xx EDT

- Created isolated worktree from `origin/main` at `2b0fb72a`.
- Ran parallel AGY read-only discovery and an independent verifier.
- Rejected stale assumptions: `/context`, task-specific auxiliary routing, delegation contracts, and memory-write provenance already exist on current main.
- Rejected an AGY-proposed Windows `resolveHermesCliBinary` patch after tracing the caller: the function is used only by `applyUpdatesPosixInApp`; Windows follows the staged updater path.
- Added AGORA graph-orchestration evidence and constrained it to an opt-in, benchmarked layer.

### 2026-07-27 14:09 EDT

- P0-A microtask verified: Windows lock-timeout diagnostics now collect a command-line-free process inventory, classify venv candidates as `desktop-owned`, `desktop-descendant`, or `external`, log PID/parent PID/executable path, and surface the relevant candidates in the update error.
- Touched: `apps/desktop/electron/main.ts`, `apps/desktop/electron/windows-lock-owner-diagnostics.ts`, `apps/desktop/electron/windows-lock-owner-diagnostics.test.ts`.
- Evidence: `npx vitest run --project electron electron/windows-lock-owner-diagnostics.test.ts electron/updater-process.test.ts electron/update-marker.test.ts` (exit 0; 14 tests); `npx tsc -p tsconfig.electron.json --noEmit` (exit 0). An initial parallel typecheck hit transient MSYS fork exhaustion (exit 1), then passed serially.
- Next: finish P0-A with a Windows failure-path integration test proving the lock-timeout result distinguishes external candidates from a surviving Desktop-owned backend.

### 2026-07-27 14:36 EDT

- P0-A completed: extracted the live lock-timeout failure-message policy and behavior-tested that an external venv holder is reported ahead of Desktop processes while a surviving Desktop-owned backend receives a distinct diagnosis.
- Touched: `apps/desktop/electron/main.ts`, `apps/desktop/electron/windows-lock-owner-diagnostics.ts`, `apps/desktop/electron/windows-lock-owner-diagnostics.test.ts`, this plan.
- Failing reproduction: `npx vitest run --project electron electron/windows-lock-owner-diagnostics.test.ts` (exit 1; 2 expected failures before implementation).
- Verification: `npx vitest run --project electron electron/windows-lock-owner-diagnostics.test.ts electron/updater-process.test.ts electron/update-marker.test.ts && npx tsc -p tsconfig.electron.json --noEmit` (exit 0; 16 tests and Electron typecheck).
- Next: P0-B, select the smallest update-transaction stage lacking an explicit terminal receipt and reproduce it with fault injection.

### 2026-07-27 14:42 EDT

- P0-B updater-spawn fault microtask verified: updater launch now produces a typed terminal receipt; a synchronous spawn failure emits the terminal `error` stage, returns the failure receipt, and restarts the backend instead of rejecting after teardown.
- Touched: `apps/desktop/electron/main.ts`, `apps/desktop/electron/updater-process.ts`, `apps/desktop/electron/updater-process.test.ts`, this plan. Prior P0-A files remain intact.
- Failing reproduction: `npx vitest run --project electron electron/updater-process.test.ts` (exit 1; 2 expected failures before implementation).
- Verification: `npx vitest run --project electron electron/updater-process.test.ts electron/windows-lock-owner-diagnostics.test.ts electron/update-marker.test.ts && npx tsc -p tsconfig.electron.json --noEmit` (exit 0; 18 tests and Electron typecheck). An intermediate run passed tests but exposed a type-narrowing error (exit 2), repaired before final verification.
- Next: P0-B activation/first-boot receipt — reproduce the smallest path that can report success before backend and WebSocket readiness are proven.

### 2026-07-27 14:47 EDT

- Corrected the P0-B updater-spawn receipt before moving on: Node commonly reports launch failures through the asynchronous child `error` event, so a synchronous `try/catch` was not a sufficient terminal gate.
- TDD reproduction: `npx vitest run --project electron electron/updater-process.test.ts` (exit 1; 3 expected failures before implementation).
- The update handoff now awaits either the child `spawn` event or terminal `error` event; it only writes the marker and exits Desktop after a proven spawn, and it keeps Desktop alive/restarts the backend after asynchronous launch failure.
- Verification: `npx vitest run --project electron electron/updater-process.test.ts electron/windows-lock-owner-diagnostics.test.ts electron/update-marker.test.ts && npx tsc -p tsconfig.electron.json --noEmit` (exit 0; 19 tests and Electron typecheck).
- Next: P0-B activation/first-boot receipt — reproduce the smallest path that can report success before backend and WebSocket readiness are proven.

### 2026-07-27 14:50 EDT

- P0-B update-wait terminal-state microtask verified: local startup now receives an explicit `skipped`, `completed`, or `timed-out` receipt, so a bounded timeout can no longer be conflated with verified updater completion.
- Touched: `apps/desktop/electron/main.ts`, `apps/desktop/electron/update-wait.ts`, `apps/desktop/electron/update-wait.test.ts`, this plan. Prior P0-A/updater-spawn edits remain intact.
- Failing reproduction: `npx vitest run --project electron electron/update-wait.test.ts` (exit 1; missing implementation).
- Verification: `npx vitest run --project electron electron/update-wait.test.ts electron/updater-process.test.ts electron/windows-lock-owner-diagnostics.test.ts electron/update-marker.test.ts && npx tsc -p tsconfig.electron.json --noEmit` (exit 0; 22 tests and Electron typecheck); `git diff --check` (exit 0).
- Next: P0-B durable activation/first-boot receipt tying update completion to backend HTTP readiness, WebSocket readiness, and the expected active profile.

### 2026-07-27 14:59 EDT

- Repaired the prior P0-B update-wait integration: the explicit `timed-out` receipt was being discarded, so local startup still launched a backend while the updater marker remained live. `requireUpdateGateCompletion` now converts that unsafe terminal state into a startup failure while allowing `skipped` and verified `completed` states.
- Touched: `apps/desktop/electron/main.ts`, `apps/desktop/electron/update-wait.ts`, `apps/desktop/electron/update-wait.test.ts`, this plan. All earlier P0-A/P0-B edits remain intact.
- Failing reproduction: `npx vitest run --project electron electron/update-wait.test.ts` (exit 1; 1 expected failure before implementation).
- Verification: `npx vitest run --project electron electron/update-wait.test.ts electron/updater-process.test.ts electron/windows-lock-owner-diagnostics.test.ts electron/update-marker.test.ts && npx tsc -p tsconfig.electron.json --noEmit && git diff --check` (exit 0; 23 tests, Electron typecheck, and whitespace validation).
- Next: P0-B durable activation/first-boot receipt tying verified update completion to backend HTTP readiness, WebSocket readiness, and the expected active profile.

### 2026-07-27 15:06 EDT

- P0-B first-boot readiness microtask verified: local startup no longer emits `backend.ready` after HTTP alone; it now requires successful HTTP/token adoption and a live `/api/ws` probe, then logs a typed terminal receipt.
- Touched: `apps/desktop/electron/main.ts`, `apps/desktop/electron/update-activation-verification.ts`, `apps/desktop/electron/update-activation-verification.test.ts`, this plan. Prior P0-A/P0-B edits remain intact.
- Failing reproduction: `npx vitest run --project electron electron/update-activation-verification.test.ts` (exit 1; missing implementation).
- Verification: `npx vitest run --project electron electron/update-activation-verification.test.ts electron/update-wait.test.ts electron/updater-process.test.ts electron/windows-lock-owner-diagnostics.test.ts electron/update-marker.test.ts && npx tsc -p tsconfig.electron.json --noEmit && git diff --check` (exit 0; 26 tests, Electron typecheck, and whitespace validation).
- Next: P0-B active-profile verification and durable on-disk activation receipt; prove the serving backend profile matches Desktop's expected profile before showing update success.

### 2026-07-27 15:15 EDT

- P0-B durable activation-receipt microtask verified: after HTTP and WebSocket first-boot checks pass, Desktop atomically persists a terminal activation receipt before emitting `backend.ready`; failed verification cannot write a success receipt.
- Touched: `apps/desktop/electron/main.ts`, `apps/desktop/electron/update-activation-receipt.ts`, `apps/desktop/electron/update-activation-receipt.test.ts`, this plan. Prior P0-A/P0-B edits remain intact.
- Failing reproduction: `npx vitest run --project electron electron/update-activation-receipt.test.ts` (exit 1; missing implementation).
- Verification: `npx vitest run --project electron electron/update-activation-receipt.test.ts electron/update-activation-verification.test.ts electron/update-wait.test.ts electron/updater-process.test.ts electron/windows-lock-owner-diagnostics.test.ts electron/update-marker.test.ts && npx tsc -p tsconfig.electron.json --noEmit && git diff --check` (exit 0; 27 tests, Electron typecheck, and whitespace validation).
- Next: P0-B active-profile verification; prove the serving backend profile matches Desktop's expected profile before activation success is persisted.

### 2026-07-27 15:22 EDT

- P0-B active-profile verification microtask verified: first boot now compares the authenticated `/api/status` `hermes_home` against Desktop's explicit or sticky expected profile home after HTTP/WebSocket readiness and rejects mismatches before writing activation success.
- Touched: `apps/desktop/electron/main.ts`, `apps/desktop/electron/update-activation-verification.ts`, `apps/desktop/electron/update-activation-verification.test.ts`, this plan. Prior P0-A/P0-B edits remain intact.
- Failing reproduction: `npx vitest run --project electron electron/update-activation-verification.test.ts` (exit 1; 2 expected failures before implementation).
- Verification: `npx vitest run --project electron electron/update-activation-verification.test.ts electron/update-activation-receipt.test.ts electron/update-wait.test.ts electron/updater-process.test.ts electron/windows-lock-owner-diagnostics.test.ts electron/update-marker.test.ts && npx tsc -p tsconfig.electron.json --noEmit && git diff --check` (exit 0; 28 tests, Electron typecheck, and whitespace validation).
- Next: P0-B rollback-state receipt; reproduce the smallest activation or first-boot failure path whose rollback state is not durably recorded.

### 2026-07-27 15:28 EDT

- Corrected Windows active-profile verification before rollback work: profile-home comparison now uses case-insensitive, separator-stable Windows path semantics rather than a case-sensitive `path.resolve` string comparison.
- TDD reproduction: `npx vitest run --project electron electron/update-activation-verification.test.ts` (exit 1; missing comparator).
- Verification: `npx vitest run --project electron electron/update-activation-verification.test.ts electron/update-activation-receipt.test.ts electron/update-wait.test.ts electron/updater-process.test.ts electron/windows-lock-owner-diagnostics.test.ts electron/update-marker.test.ts && npx tsc -p tsconfig.electron.json --noEmit && git diff --check` (exit 0; 29 tests, Electron typecheck, and whitespace validation).
- Next: P0-B rollback-state receipt; reproduce the smallest activation or first-boot failure path whose rollback state is not durably recorded.

### 2026-07-27 15:32 EDT

- P0-B rollback-state receipt microtask verified: failed first-boot activation now atomically overwrites any stale success receipt with a terminal `failed` receipt that records transport evidence, the safe error, and the honest rollback state `not-attempted` before startup surfaces the failure.
- Touched: `apps/desktop/electron/main.ts`, `apps/desktop/electron/update-activation-receipt.ts`, `apps/desktop/electron/update-activation-receipt.test.ts`, this plan. Earlier P0-A/P0-B edits remain intact.
- Failing reproduction: `npx vitest run --project electron electron/update-activation-receipt.test.ts` (exit 1; missing failure-receipt implementation).
- Verification: `npx vitest run --project electron electron/update-activation-receipt.test.ts electron/update-activation-verification.test.ts electron/update-wait.test.ts electron/updater-process.test.ts electron/windows-lock-owner-diagnostics.test.ts electron/update-marker.test.ts && npx tsc -p tsconfig.electron.json --noEmit && git diff --check` (exit 0; 30 tests, Electron typecheck, and whitespace validation).
- Next: P0-B rollback execution fault injection; identify the narrowest venv activation/first-boot failure path where an available prior runtime can be restored, then persist `completed` or `failed` rollback evidence.

### 2026-07-27 16:04 EDT

- Repaired the complete Windows Desktop verification gate rather than accepting platform failures as unrelated. The 19 failures came from six platform-assumption classes: POSIX SSH mux tests inheriting the Windows no-mux default, native separators in injected POSIX path tests, case-only Windows realpath differences, unsupported POSIX mode assertions on NTFS, Node 26's experimental global Web Storage shadowing jsdom, and an NSIS tempfile race under the machine-wide `C:\Windows\Temp`.
- Cross-platform tests now select their intended path/mux semantics explicitly; production path helpers use the requested platform's `path.posix`/`path.win32` implementation; UI Vitest scripts disable Node's experimental Web Storage; and electron-builder receives an isolated per-build user temp directory under `%LOCALAPPDATA%\Temp`.
- Failing reproduction: six Electron files produced 19 failures (`19 failed | 109 passed`); plain `npm run check` then reached packaging and reproduced NSIS `!include: could not find C:\WINDOWS\TEMP\nst*.tmp`.
- Verification: plain `npm run check` exited 0. UI: 297 files, 2,526 passed / 1 skipped. Electron: 72 files passed / 1 skipped, 812 passed / 2 skipped. Renderer/Electron/E2E typechecks, ESLint, production build, Windows NSIS packaging, artifact validation, node-pty payload validation, and `git diff --check` all passed. Installer: `apps/desktop/release/Hermes-0.17.0-win-x64.exe`.
- Next: P0-B rollback execution fault injection; identify the narrowest venv activation/first-boot failure path where an available prior runtime can be restored, then persist `completed` or `failed` rollback evidence.
