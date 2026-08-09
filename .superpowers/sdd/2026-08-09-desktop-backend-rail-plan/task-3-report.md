# Task 3 Report — backend-root selection and prewarm actions

Status: complete

Commit message:

`feat(desktop): add backend root selection`

Changed files:

- `apps/desktop/src/store/profile.ts`
- `apps/desktop/src/store/profile.test.ts`
- `.superpowers/sdd/2026-08-09-desktop-backend-rail-plan/task-3-report.md`

Design decisions:

- Added `BackendTarget = 'local' | 'remote'` in the profile store and mapped it to Task 2's target-aware gateway options with a small `backendOptions()` helper.
- Reused Task 2's target-aware store interfaces (`ensureGatewayForProfile`, `openGatewayForProfile`, `activeGatewayTargetOptions`) instead of re-encoding gateway identity keys.
- Added `isActiveGatewayTarget()` to compare the active gateway profile plus target-aware options, with a default-root fallback to the current `$connection.mode` so local default remains a no-op even when the active registry target is represented as "plain".
- Implemented `selectBackend(target)` to:
  - clear all-profiles browsing,
  - pin new-chat profile selection to `default`,
  - request a fresh session only when the backend target actually changes,
  - activate the chosen backend root through `ensureGatewayProfile('default', backendOptions(target))`,
  - swallow background activation failures so the current live connection remains intact.
- Implemented `prewarmBackend(target)` to open the target-aware default root without activating it, using the existing prewarm throttle map and silent failure behavior.
- Updated `prewarmProfileBackend()` and `ensureGatewayProfile()` to share the same active-target guard so unchanged targets remain no-ops.
- Used synthetic/redacted test data only (`default`, `warm-*`, `remote unavailable`, redacted remote URL fixture already present in the test file).

Commands run:

1. Read skills/brief and inspect code
   - `sed -n '1,220p' /Users/mosta/.codex/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/using-superpowers/SKILL.md`
   - `sed -n '1,260p' /Users/mosta/.codex/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/test-driven-development/SKILL.md`
   - `sed -n '1,220p' /Users/mosta/.codex/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/verification-before-completion/SKILL.md`
   - `sed -n '1,260p' /Users/mosta/.hermes/hermes-agent/.superpowers/sdd/2026-08-09-desktop-backend-rail-plan/task-3-brief.md`
   - `sed -n '1,240p' /Users/mosta/.codex/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/brainstorming/SKILL.md`
   - `sed -n '1,220p' /Users/mosta/.codex/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/using-superpowers/references/codex-tools.md`
   - `git -C /Users/mosta/.hermes/hermes-agent status --short`
   - `sed -n '1,260p' /Users/mosta/.hermes/hermes-agent/apps/desktop/src/store/profile.ts`
   - `sed -n '1,320p' /Users/mosta/.hermes/hermes-agent/apps/desktop/src/store/profile.test.ts`
   - `sed -n '260,520p' /Users/mosta/.hermes/hermes-agent/apps/desktop/src/store/profile.ts`
   - `sed -n '520,620p' /Users/mosta/.hermes/hermes-agent/apps/desktop/src/store/profile.ts`
   - `sed -n '1,260p' /Users/mosta/.hermes/hermes-agent/apps/desktop/src/store/gateway.ts`
   - `jq '{name, scripts}' /Users/mosta/.hermes/hermes-agent/package.json`
   - `jq '{name, scripts}' /Users/mosta/.hermes/hermes-agent/apps/desktop/package.json`
   - Exit status: `0`

2. RED verification after adding tests
   - `bunx vitest run --project ui src/store/profile.test.ts`
   - Exit status: `1`
   - Relevant output:
     - `6 failed | 18 passed (24)`
     - `TypeError: selectBackend is not a function`
     - `TypeError: prewarmBackend is not a function`

3. First GREEN attempt after implementation
   - `bunx vitest run --project ui src/store/profile.test.ts`
   - Exit status: `1`
   - Relevant output:
     - `1 failed | 23 passed (24)`
     - failing test: `requests a fresh session only when the backend target changes`
     - unhandled rejection surfaced from mocked `remote unavailable`

4. Focused GREEN verification after fixing target no-op + background failure handling
   - `bunx vitest run --project ui src/store/profile.test.ts`
   - Exit status: `0`
   - Relevant output:
     - `1 passed (1)`
     - `24 passed (24)`

5. Typecheck attempt using the brief's command at repo root
   - `bun run typecheck`
   - Working directory: `/Users/mosta/.hermes/hermes-agent`
   - Exit status: `1`
   - Relevant output:
     - `error: Script not found "typecheck"`

6. Typecheck attempt using the desktop package's actual script
   - `bun run typecheck`
   - Working directory: `/Users/mosta/.hermes/hermes-agent/apps/desktop`
   - Exit status: `2`
   - Relevant output:
     - `src/store/profile.ts(77,3): error TS2322: Type 'boolean | undefined' is not assignable to type 'boolean'.`

7. Final fresh verification
   - `bunx vitest run --project ui src/store/profile.test.ts`
   - Working directory: `/Users/mosta/.hermes/hermes-agent/apps/desktop`
   - Exit status: `0`
   - Relevant output:
     - `24 passed (24)`
   - `bun run typecheck`
   - Working directory: `/Users/mosta/.hermes/hermes-agent/apps/desktop`
   - Exit status: `0`
   - Relevant output:
     - `$ tsc -p . --noEmit && tsc -p tsconfig.electron.json --noEmit && tsc -p tsconfig.e2e.json --noEmit`

8. Self-review / patch hygiene
   - `git -C /Users/mosta/.hermes/hermes-agent diff --check`
   - `git -C /Users/mosta/.hermes/hermes-agent diff --stat -- apps/desktop/src/store/profile.ts apps/desktop/src/store/profile.test.ts`
   - `git -C /Users/mosta/.hermes/hermes-agent diff -- apps/desktop/src/store/profile.ts apps/desktop/src/store/profile.test.ts`
   - Exit status: `0`
   - Relevant output:
     - `2 files changed, 138 insertions(+), 14 deletions(-)`

Concerns:

- The brief's repo-root `bun run typecheck` command is not available in this checkout. I used the desktop package's real `typecheck` script instead, which passed after fixing one strict-boolean issue in the new helper.

## Fix round 1 — review follow-up

Summary:

- Moved the `selectBackend()` fresh-session bump behind successful `ensureGatewayProfile()` activation so failed backend switches do not clear the visible session.
- Added regression coverage that a failed `selectBackend()` leaves `$freshSessionRequest` unchanged.
- Added direct regression coverage that `prewarmBackend()` throttles repeated prewarms for the same target.

Changed files:

- `apps/desktop/src/store/profile.ts`
- `apps/desktop/src/store/profile.test.ts`
- `.superpowers/sdd/2026-08-09-desktop-backend-rail-plan/task-3-report.md`

Design decisions:

- Kept `selectBackend()` as a fire-and-forget store action, but chained `requestFreshSession()` from the successful resolution of `ensureGatewayProfile(profile, options)`.
- Preserved the prior background-failure behavior by continuing to swallow activation errors inside `selectBackend()`; on failure the active connection and visible session remain on the current backend.
- Added a tiny `flushMicrotasks()` test helper so store tests can assert post-activation behavior deterministically without changing production code shape.
- For direct `prewarmBackend()` throttle coverage, used fake time plus a non-active target (`local` while the active default connection is remote) so the test measures throttling rather than the active-target no-op guard.

Commands run:

1. Inspect current code and report
   - `sed -n '1,260p' /Users/mosta/.codex/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/receiving-code-review/SKILL.md`
   - `git -C /Users/mosta/.hermes/hermes-agent status --short && sed -n '1,260p' /Users/mosta/.hermes/hermes-agent/apps/desktop/src/store/profile.ts && sed -n '1,340p' /Users/mosta/.hermes/hermes-agent/apps/desktop/src/store/profile.test.ts && sed -n '1,260p' /Users/mosta/.hermes/hermes-agent/.superpowers/sdd/2026-08-09-desktop-backend-rail-plan/task-3-report.md`
   - Exit status: `0`

2. RED verification after adding the new regression tests
   - `bunx vitest run --project ui src/store/profile.test.ts`
   - Working directory: `/Users/mosta/.hermes/hermes-agent/apps/desktop`
   - Exit status: `1`
   - Relevant output:
     - `2 failed | 24 passed (26)`
     - `leaves the fresh-session request unchanged when the background switch fails`
     - `throttles repeat pre-warms for the same backend target within the interval`
     - failure detail: `expected 1 to be 0`

3. Focused GREEN verification after moving the fresh-session trigger behind successful activation
   - `bunx vitest run --project ui src/store/profile.test.ts`
   - Working directory: `/Users/mosta/.hermes/hermes-agent/apps/desktop`
   - Exit status: `0`
   - Relevant output:
     - `26 passed (26)`

4. Desktop typecheck
   - `bun run typecheck`
   - Working directory: `/Users/mosta/.hermes/hermes-agent/apps/desktop`
   - Exit status: `0`
   - Relevant output:
     - `$ tsc -p . --noEmit && tsc -p tsconfig.electron.json --noEmit && tsc -p tsconfig.e2e.json --noEmit`

5. Diff inspection for the fix round
   - `git -C /Users/mosta/.hermes/hermes-agent diff -- apps/desktop/src/store/profile.ts apps/desktop/src/store/profile.test.ts .superpowers/sdd/2026-08-09-desktop-backend-rail-plan/task-3-report.md`
   - Exit status: `0`

Concerns:

- The same repo-root script gap remains: `bun run typecheck` still does not exist at `/Users/mosta/.hermes/hermes-agent`, so the verified typecheck command for this task continues to be the desktop package script in `apps/desktop`.
