# Desktop Messaging Profile Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route desktop messaging-platform reads, updates, and tests through the profile selected in the desktop UI.

**Architecture:** Reuse the renderer's existing `profileScoped()` request descriptor so Electron can choose the selected profile's backend or append the profile query for a shared remote backend. Keep the backend endpoints and Messaging Platforms page unchanged because both already honor profile-scoped requests.

**Tech Stack:** TypeScript, Electron IPC bridge, Vitest

---

## File Structure

- Modify `apps/desktop/src/hermes-profile-scope.test.ts`: add the regression contract for all three messaging-platform helpers.
- Modify `apps/desktop/src/hermes.ts`: attach the existing active-profile descriptor to each messaging-platform request.

### Task 1: Add The Messaging Profile-Scope Contract

**Files:**
- Test: `apps/desktop/src/hermes-profile-scope.test.ts:3-15,83`
- Modify: `apps/desktop/src/hermes.ts:1265-1286`

- [x] **Step 1: Write the failing regression test**

Add the three messaging helpers to the existing import:

```ts
import {
  checkHermesUpdate,
  getActionStatus,
  getElevenLabsVoices,
  getMemoryProviderConfig,
  getMessagingPlatforms,
  getStatus,
  restartGateway,
  saveMemoryProviderConfig,
  setApiRequestProfile,
  speakText,
  testMessagingPlatform,
  transcribeAudio,
  updateHermes,
  updateMessagingPlatform
} from './hermes'
```

Add this test inside `describe('backend action helpers are profile-scoped', ...)`:

```ts
it('forwards the active profile to messaging platform endpoints', () => {
  setApiRequestProfile('hmbot2')
  const update = { enabled: true, env: { TELEGRAM_BOT_TOKEN: 'replacement-token' } }

  void getMessagingPlatforms()
  void updateMessagingPlatform('telegram', update)
  void testMessagingPlatform('telegram')

  expect(api.mock.calls.map(call => call[0])).toEqual([
    { profile: 'hmbot2', path: '/api/messaging/platforms' },
    {
      profile: 'hmbot2',
      path: '/api/messaging/platforms/telegram',
      method: 'PUT',
      body: update
    },
    {
      profile: 'hmbot2',
      path: '/api/messaging/platforms/telegram/test',
      method: 'POST'
    }
  ])
})
```

Also extend the existing single-profile compatibility test so the messaging
read is covered explicitly:

```ts
it('omits profile when none is active (single-profile users unaffected)', () => {
  void getStatus()
  void getMessagingPlatforms()

  for (const call of api.mock.calls) {
    expect(call[0].profile).toBeUndefined()
  }
})
```

- [x] **Step 2: Run the focused test and verify RED**

Run:

```bash
npm --prefix apps/desktop run test:ui -- src/hermes-profile-scope.test.ts
```

Expected: FAIL in `forwards the active profile to messaging platform endpoints`; each messaging request is missing `profile: 'hmbot2'`.

- [x] **Step 3: Implement the minimal profile propagation**

Update only the three request descriptors in `apps/desktop/src/hermes.ts`:

```ts
export function getMessagingPlatforms(): Promise<MessagingPlatformsResponse> {
  return window.hermesDesktop.api<MessagingPlatformsResponse>({
    ...profileScoped(),
    path: '/api/messaging/platforms'
  })
}

export function updateMessagingPlatform(
  platformId: string,
  body: MessagingPlatformUpdate
): Promise<{ ok: boolean; platform: string }> {
  return window.hermesDesktop.api<{ ok: boolean; platform: string }>({
    ...profileScoped(),
    path: `/api/messaging/platforms/${encodeURIComponent(platformId)}`,
    method: 'PUT',
    body
  })
}

export function testMessagingPlatform(platformId: string): Promise<MessagingPlatformTestResponse> {
  return window.hermesDesktop.api<MessagingPlatformTestResponse>({
    ...profileScoped(),
    path: `/api/messaging/platforms/${encodeURIComponent(platformId)}/test`,
    method: 'POST'
  })
}
```

- [x] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
npm --prefix apps/desktop run test:ui -- src/hermes-profile-scope.test.ts
```

Expected: PASS for the complete `hermes-profile-scope.test.ts` file.

- [x] **Step 5: Commit the tested fix**

```bash
git add apps/desktop/src/hermes.ts apps/desktop/src/hermes-profile-scope.test.ts
git commit -m "fix(desktop): scope messaging requests to active profile"
```

### Task 2: Verify The Change Against The Wider Repository

**Files:**
- Verify: `apps/desktop/src/hermes.ts`
- Verify: `apps/desktop/src/hermes-profile-scope.test.ts`

- [x] **Step 1: Run the complete desktop unit-test suite**

```bash
npm --prefix apps/desktop test
```

Expected: all UI and Electron Vitest projects pass.

- [x] **Step 2: Run desktop type checking and linting**

```bash
npm --prefix apps/desktop run typecheck
npm --prefix apps/desktop run lint
```

Expected: both commands exit with status 0 and report no errors.

- [x] **Step 3: Run the repository Python suite required by AGENTS.md**

```bash
source venv/bin/activate
python -m pytest tests/ -q
```

Expected: the full Python test suite exits with status 0 and no failures.

- [x] **Step 4: Inspect the final diff and worktree**

```bash
git diff origin/main...HEAD --check
git status --short --branch
```

Expected: no whitespace errors; the branch contains only the design, plan, regression test, and three profile-scope additions.
