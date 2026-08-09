# Task 6 failure report

Date: 2026-08-09

## Root cause

`apps/desktop/electron/main.ts` regressed legacy keepalive semantics in `touchPoolBackend(profile, options)`.

The recent exact-target fast path always resolved `backendPoolTargetKey(profile, options)` even when `options` was omitted. For a legacy plain touch like `touchPoolBackend('default')`, that still resolves to `default`, so the function updated only the pooled `default` entry and returned early. That skipped the established fallback through `touchBackendPoolEntries(...)`, which is the code path that refreshes `default`, `local:default`, and `remote:default` together for legacy root-profile touches.

The explicit-target behavior itself was correct: `localOnly` / `remoteOnly` touches should keep those root rails isolated so they do not collide. The bug was that unscoped legacy touches were accidentally treated as explicit target touches.

## Changed files

- `apps/desktop/electron/main.ts`
- `apps/desktop/electron/main-backend-routing.test.ts`
- `.superpowers/sdd/2026-08-09-desktop-backend-rail-plan/task-6-failure-report.md`

## Commands

### Initial reproduction

Command:

`bunx vitest run --project electron electron/main-backend-routing.test.ts`

Exit status: `1`

Relevant output:

```text
FAIL  |electron| electron/main-backend-routing.test.ts > hermes:backend:touch refreshes pooled explicit root rails through the main IPC handler
AssertionError: Expected values to be strictly equal:
+ actual - expected

+ 2
- 1786293194114
```

### Red verification after adding regression coverage

Command:

`bunx vitest run --project electron electron/main-backend-routing.test.ts`

Exit status: `1`

Relevant output:

```text
FAIL  |electron| electron/main-backend-routing.test.ts > hermes:backend:touch refreshes pooled explicit root rails through the main IPC handler
AssertionError: Expected values to be strictly equal:
+ actual - expected

+ 2
- 1786293260876
```

### Focused Electron routing verification after fix

Command:

`bunx vitest run --project electron electron/main-backend-routing.test.ts electron/connection-config.test.ts`

Exit status: `0`

Relevant output:

```text
Test Files  2 passed (2)
Tests  91 passed (91)
```

### Desktop typecheck

Command:

`bun run typecheck`

Exit status: `0`

Relevant output:

```text
$ tsc -p . --noEmit && tsc -p tsconfig.electron.json --noEmit && tsc -p tsconfig.e2e.json --noEmit
```

## Concerns

- `bunx vitest` still emits the pre-existing Vite warning about `__dirname` with `configLoader: 'native'`; it did not affect test outcomes here.
- There is an unrelated pre-existing worktree change outside this task: deleted `.superpowers/sdd/2026-08-09-desktop-backend-rail-plan/task-3-report.md`. I left it untouched.
