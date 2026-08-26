/**
 * E2E regression: publishing persisted authority during a warm-route resume
 * must keep a settled, bottom-following transcript anchored to the bottom.
 *
 * The test deliberately delays the real session-messages response, lets the
 * cached viewport settle, then publishes a non-equivalent persisted message
 * list. A requestAnimationFrame observer fails if a rendered frame exposes a
 * stale scroll offset.
 *
 * Prerequisite: `npm run build` must have been run so dist/ exists.
 */

import { expect, test } from './test'

import {
  type MockBackendFixture,
  waitForAppReady,
  createSandbox,
  writeMockProviderConfig,
  writeEnvFile,
  buildAppEnv,
  launchDesktop
} from './fixtures'
import { startMockServer } from './mock-server'
import { RealSessionBuilder } from './real-session-builder'

const SESSION_TITLE = 'E2E Warm Resume Jitter Test'

// Inactive tabs stay mounted under a data-pane-hidden ancestor. Match the
// renderer's keep-alive visibility policy instead of relying on DOM order.
const SURFACE = '[data-composer-target]:not([data-pane-hidden] [data-composer-target])'
/** 32 messages (16 user/assistant pairs) — enough DOM churn for detection. */
const MESSAGE_COUNT = 32
const AUTHORITY_ONLY_TEXT = Array.from(
  { length: 8 },
  (_, index) => `E2E delayed persisted authority row ${index + 1}`
).join('\n')
const COMPLETED_REPLY = 'Hello from the mock inference server! The full boot chain is working.'
/** Seeded PRNG so the generated content is deterministic across runs. */
const RNG_SEED = 42

/** Mulberry32 — tiny deterministic PRNG. */
function mulberry32(seed: number): () => number {
  let a = seed
  return () => {
    a |= 0
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/** Generate ~40 chars of gibberish from a seeded PRNG. */
function gibberish(rng: () => number): string {
  const len = 30 + Math.floor(rng() * 20)
  let s = ''
  for (let i = 0; i < len; i++) {
    s += String.fromCharCode(97 + Math.floor(rng() * 26))
  }
  return s
}

/** First user message — used as a wait target in the test. */
const FIRST_USER_MSG = gibberish(mulberry32(RNG_SEED))

/**
 * Generate the user turns for a real session. The mock provider produces the
 * assistant side of each pair through the normal AIAgent persistence path.
 */
function generateSessionTurns(): string[] {
  const rng = mulberry32(RNG_SEED)
  const turns: string[] = []

  for (let i = 0; i < MESSAGE_COUNT / 2; i++) {
    turns.push(gibberish(rng))
    gibberish(rng)
  }

  return turns
}

/**
 * Set up a mock-backend sandbox with a real persisted session in state.db.
 *
 * Unlike the shared `setupMockBackend()`, this variant creates the session
 * through the real stdio gateway before launching desktop so the session is
 * visible in the sidebar on first load.
 */
async function setupSeededMockBackend(): Promise<MockBackendFixture> {
  // 1. Start mock server
  const mock = await startMockServer()

  // 2. Create sandbox + write config
  const sandbox = createSandbox('warm-seed')
  writeMockProviderConfig(sandbox.hermesHome, mock.url)
  writeEnvFile(sandbox.hermesHome)

  // 3. Produce all 16 user/assistant pairs through the real TUI gateway,
  // AIAgent, mock provider, and SessionDB persistence path before desktop starts.
  const builder = await RealSessionBuilder.start(sandbox.hermesHome)
  try {
    await builder.createSession({ title: SESSION_TITLE, turns: generateSessionTurns() })
  } finally {
    await builder.close()
  }

  // 4. Build env + launch
  const env = buildAppEnv(sandbox)
  const { app, page } = await launchDesktop(env)

  return {
    app,
    page,
    mock,
    mockUrl: mock.url,
    sandbox,
    cleanup: async () => {
      await app.close().catch(() => undefined)
      await mock.close()
      sandbox.cleanup()
    }
  }
}

let fixture: MockBackendFixture | null = null

test.beforeAll(async () => {
  fixture = await setupSeededMockBackend()
  await waitForAppReady(fixture!, 120_000)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

/** Wait until the ACTIVE chat surface's transcript contains `text`. */
async function waitForActiveTranscriptText(
  page: import('@playwright/test').Page,
  text: string,
  timeout = 30_000
): Promise<void> {
  await page.waitForFunction(
    ([expected, surfaceSelector]: [string, string]) => {
      const surfaces = document.querySelectorAll(surfaceSelector)
      const active = surfaces[surfaces.length - 1]

      return (active?.querySelector('[data-slot="aui_thread-viewport"]')?.textContent ?? '').includes(expected)
    },
    [text, SURFACE] as [string, string],
    { timeout }
  )
}

async function waitForActiveTranscriptWithoutText(page: import('@playwright/test').Page, text: string): Promise<void> {
  await page.waitForFunction(
    ([expected, surfaceSelector]: [string, string]) => {
      const surfaces = document.querySelectorAll(surfaceSelector)
      const active = surfaces[surfaces.length - 1]

      return !(active?.querySelector('[data-slot="aui_thread-viewport"]')?.textContent ?? '').includes(expected)
    },
    [text, SURFACE] as [string, string],
    { timeout: 15_000 }
  )
}

/** Replace the primary surface with a draft while retaining its warm cache. */
async function openFreshDraft(page: import('@playwright/test').Page, priorText: string): Promise<void> {
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+N' : 'Control+N')
  await waitForActiveTranscriptWithoutText(page, priorText)
}

type MainAuthorityGate = {
  hit: boolean
  release: () => void
  released: boolean
}

async function armDelayedPersistedAuthority(app: MockBackendFixture['app']): Promise<void> {
  await app.evaluate(({ ipcMain }, marker) => {
    type InvokeHandler = (...args: unknown[]) => unknown
    const handlers = (ipcMain as unknown as { _invokeHandlers?: Map<string, InvokeHandler> })._invokeHandlers
    const original = handlers?.get('hermes:api')

    if (!handlers || !original) {
      throw new Error('Electron hermes:api invoke handler is unavailable')
    }

    let releaseResponse!: () => void
    const held = new Promise<void>(resolve => {
      releaseResponse = resolve
    })
    const gate: MainAuthorityGate = {
      hit: false,
      released: false,
      release: () => {
        if (!gate.released) {
          gate.released = true
          releaseResponse()
        }
      }
    }
    const mainGlobal = globalThis as typeof globalThis & { __E2E_AUTHORITY_GATE__?: MainAuthorityGate }
    mainGlobal.__E2E_AUTHORITY_GATE__ = gate

    handlers.set('hermes:api', async (...args: unknown[]) => {
      const result = await original(...args)
      const request = args[1] as { method?: string; path?: string } | undefined
      const isTargetRead =
        (!request?.method || request.method === 'GET') &&
        /^\/api\/sessions\/[^/]+\/messages(?:\?|$)/.test(request?.path ?? '')

      if (!gate.hit && isTargetRead) {
        gate.hit = true
        handlers.set('hermes:api', original)
        await held

        const response = result as { messages?: unknown[] }

        if (!Array.isArray(response?.messages)) {
          throw new Error('Target session-messages response has no messages array')
        }

        const maxId = response.messages.reduce<number>((current, row) => {
          const record = row && typeof row === 'object' ? (row as { id?: unknown; row_id?: unknown }) : null
          const candidate = Number(record?.id ?? record?.row_id)

          return Number.isFinite(candidate) ? Math.max(current, candidate) : current
        }, 0)

        return {
          ...response,
          messages: [
            ...response.messages,
            {
              content: marker,
              id: maxId + 1000,
              role: 'user',
              timestamp: Date.now() / 1000
            }
          ]
        }
      }

      return result
    })
  }, AUTHORITY_ONLY_TEXT)
}

async function persistedAuthorityRequestIsWaiting(app: MockBackendFixture['app']): Promise<boolean> {
  return app.evaluate(() => {
    const mainGlobal = globalThis as typeof globalThis & { __E2E_AUTHORITY_GATE__?: MainAuthorityGate }

    return Boolean(mainGlobal.__E2E_AUTHORITY_GATE__?.hit && !mainGlobal.__E2E_AUTHORITY_GATE__?.released)
  })
}

async function releasePersistedAuthority(app: MockBackendFixture['app']): Promise<void> {
  await app.evaluate(() => {
    const mainGlobal = globalThis as typeof globalThis & { __E2E_AUTHORITY_GATE__?: MainAuthorityGate }
    const gate = mainGlobal.__E2E_AUTHORITY_GATE__

    if (!gate?.hit) {
      throw new Error('Persisted-authority request was not waiting at release time')
    }

    gate.release()
  })
}

interface ViewportAnchorReport {
  authorityReleaseFrame: number
  maxDistanceFromBottom: number
  samples: Array<{
    authorityVisible: boolean
    clientHeight: number
    distanceFromBottom: number
    following: string | undefined
    frame: number
    messageCount: number
    mutation: number
    scrollHeight: number
    scrollTop: number
  }>
  settled: boolean
  settledFrame: number
  worstFrame: number
}

async function installViewportAnchorObserver(
  page: import('@playwright/test').Page,
  expectedCachedMessageCount: number
): Promise<void> {
  await page.evaluate(
    ([surfaceSelector, authorityMarker, expectedCount]: [string, string, number]) => {
      const surfaces = [...document.querySelectorAll(surfaceSelector)]
      const viewport = surfaces.at(-1)?.querySelector<HTMLElement>('[data-slot="aui_thread-viewport"]')

      if (!viewport) {
        throw new Error('Active draft thread viewport not found before warm resume')
      }

      const state = {
        authorityReleaseFrame: -1,
        bottomStreak: 0,
        frame: 0,
        lastMutation: 0,
        maxDistanceFromBottom: 0,
        mutation: 0,
        quietStreak: 0,
        samples: [] as ViewportAnchorReport['samples'],
        settled: false,
        settledFrame: -1,
        stopped: false,
        worstFrame: -1
      }
      const debugWindow = window as unknown as {
        __ANCHOR_OBSERVER__: typeof state
        __ANCHOR_VIEWPORT__: Element
      }
      debugWindow.__ANCHOR_OBSERVER__ = state
      debugWindow.__ANCHOR_VIEWPORT__ = viewport

      const observer = new MutationObserver(() => {
        state.mutation += 1
      })
      observer.observe(viewport, { childList: true, subtree: true })

      const tick = () => {
        if (state.stopped) {
          observer.disconnect()
          return
        }

        state.frame += 1
        const distance = Math.max(0, viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop)
        const messageCount = viewport.querySelectorAll('[data-role="user"], [data-role="assistant"]').length
        const cachedTranscriptComplete = messageCount >= expectedCount
        const quiet = state.mutation === state.lastMutation
        state.lastMutation = state.mutation

        if (!state.settled) {
          const atBottom = cachedTranscriptComplete && distance <= 2
          state.bottomStreak = atBottom ? state.bottomStreak + 1 : 0
          state.quietStreak = atBottom && quiet ? state.quietStreak + 1 : 0

          if (state.bottomStreak >= 4 && state.quietStreak >= 3) {
            state.settled = true
            state.settledFrame = state.frame
          }
        } else {
          if (distance > 4 && state.samples.length < 50) {
            state.samples.push({
              authorityVisible: (viewport.textContent ?? '').includes(authorityMarker),
              clientHeight: viewport.clientHeight,
              distanceFromBottom: distance,
              following: viewport.dataset.following,
              frame: state.frame,
              messageCount,
              mutation: state.mutation,
              scrollHeight: viewport.scrollHeight,
              scrollTop: viewport.scrollTop
            })
          }

          if (distance > state.maxDistanceFromBottom) {
            state.maxDistanceFromBottom = distance
            state.worstFrame = state.frame
          }
        }

        requestAnimationFrame(tick)
      }

      requestAnimationFrame(tick)
    },
    [SURFACE, AUTHORITY_ONLY_TEXT, expectedCachedMessageCount] as [string, string, number]
  )
}

async function markAuthorityRelease(page: import('@playwright/test').Page): Promise<void> {
  await page.evaluate(() => {
    const state = (
      window as unknown as {
        __ANCHOR_OBSERVER__?: ViewportAnchorReport & { frame: number; stopped: boolean }
      }
    ).__ANCHOR_OBSERVER__

    if (state) {
      state.authorityReleaseFrame = state.frame
    }
  })
}

async function readViewportAnchorReport(page: import('@playwright/test').Page): Promise<ViewportAnchorReport | null> {
  return page.evaluate(() => {
    const state = (
      window as unknown as {
        __ANCHOR_OBSERVER__?: ViewportAnchorReport & { stopped: boolean }
      }
    ).__ANCHOR_OBSERVER__

    if (!state) {
      return null
    }

    state.stopped = true

    return {
      authorityReleaseFrame: state.authorityReleaseFrame,
      maxDistanceFromBottom: state.maxDistanceFromBottom,
      samples: state.samples,
      settled: state.settled,
      settledFrame: state.settledFrame,
      worstFrame: state.worstFrame
    }
  })
}

test('warm-route resume keeps the settled viewport anchored across persisted authority publication', async ({}, testInfo) => {
  const page = fixture!.page
  const { mock } = fixture!

  // Wait for the sidebar to populate with our seeded session.
  const sessionRow = page.locator('[data-slot="sidebar"] button').filter({ hasText: SESSION_TITLE }).first()
  await sessionRow.waitFor({ state: 'visible', timeout: 60_000 })

  // Step 1: Cold resume — populate the warm cache.
  await sessionRow.click()
  await waitForActiveTranscriptText(page, FIRST_USER_MSG)
  await page.waitForTimeout(2_000)

  // Step 2: Send a message — triggers inference via the mock server.
  const PROMPT = 'E2E post-inference warm resume test prompt'
  const replyCountBefore = await page.getByText(COMPLETED_REPLY, { exact: true }).count()
  const composer = page.locator('[contenteditable="true"]').first()
  await composer.click()
  await composer.type(PROMPT, { delay: 10 })
  await page.keyboard.press('Enter')

  // Wait for the mock response to appear in the transcript, confirming
  // the turn completed and message.complete fired (which updates the warm
  // cache via updateSessionState).
  await expect.poll(() => mock.receivedPrompts.filter(prompt => prompt === PROMPT).length, { timeout: 60_000 }).toBe(1)
  await expect
    .poll(() => page.getByText(COMPLETED_REPLY, { exact: true }).count(), { timeout: 60_000 })
    .toBeGreaterThan(replyCountBefore)
  // Extra settle for message.complete → updateSessionState → cache write.
  await page.waitForTimeout(2_000)

  // Verify the prompt was received by the mock server.
  expect(mock.receivedPrompts).toContain(PROMPT)

  // Step 3: Replace the primary chat; the warm cache retains the updated messages.
  await openFreshDraft(page, PROMPT)
  await page.waitForTimeout(500)

  // Step 4: Delay a deliberately non-equivalent persisted response until the
  // cached viewport has settled, then assert that publication never exposes a
  // stale scroll offset to a rendered frame.
  await armDelayedPersistedAuthority(fixture!.app)
  await installViewportAnchorObserver(page, MESSAGE_COUNT + 2)
  await sessionRow.click()

  // Wait for the transcript to reappear — the warm cache should already
  // have the completed turn (updated by message.complete events).
  await waitForActiveTranscriptText(page, FIRST_USER_MSG)

  await page.waitForFunction(
    () => {
      const w = window as unknown as { __ANCHOR_OBSERVER__?: { settled: boolean } }

      return Boolean(w.__ANCHOR_OBSERVER__?.settled)
    },
    undefined,
    { timeout: 15_000 }
  )

  await expect.poll(() => persistedAuthorityRequestIsWaiting(fixture!.app), { timeout: 10_000 }).toBe(true)
  await markAuthorityRelease(page)
  await releasePersistedAuthority(fixture!.app)
  await waitForActiveTranscriptText(page, AUTHORITY_ONLY_TEXT, 15_000)
  await page.waitForTimeout(500)

  const report = await readViewportAnchorReport(page)
  await testInfo.attach('viewport-anchor-report.json', {
    body: Buffer.from(JSON.stringify(report, null, 2)),
    contentType: 'application/json'
  })
  await page.screenshot({ path: testInfo.outputPath('warm-resume-post-inference.png') })
  expect(report?.settled).toBe(true)
  expect(
    report?.maxDistanceFromBottom,
    `Persisted authority exposed a stale scroll offset at frame ${report?.worstFrame} after settling at frame ${report?.settledFrame}.`
  ).toBeLessThanOrEqual(4)
})
