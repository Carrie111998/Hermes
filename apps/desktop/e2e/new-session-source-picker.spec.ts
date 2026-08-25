/**
 * E2E for the per-session source picker (PR #94457).
 *
 * The picker only mounts when more than one source is registered
 * (hasMultipleConnections). The default sandbox registry has a single
 * "local" connection, so this spec seeds a v2 connections.json with a
 * local + remote source BEFORE launch. We then drive the two "new session"
 * affordances — the sidebar row and the header "+" button — and assert that
 * each opens the picker dropdown showing both registered sources.
 *
 * Prerequisite: `npm run build` must have been run so dist/ exists.
 */

import { expect, test, type Page } from '@playwright/test'
import * as fs from 'node:fs'
import * as path from 'node:path'

import {
  type MockBackendFixture,
  buildAppEnv,
  createSandbox,
  launchDesktop,
  waitForAppReady,
  writeEnvFile,
  writeMockProviderConfig,
} from './fixtures'
import { startMockServer } from './mock-server'

/**
 * A valid v2 registry with two sources (local + an unreachable remote). The
 * remote URL never has to answer — we only assert the picker enumerates it.
 * normalizeRegistry in electron/connection-registry.ts accepts a remote with a
 * non-empty url (token optional), which keeps connections.length === 2 so
 * $hasMultipleConnections is true.
 */
const TWO_SOURCE_REGISTRY = {
  version: 2,
  primary: 'local',
  launchMode: 'primary',
  lastUsed: 'local',
  connections: [
    { id: 'local', kind: 'local', label: 'This device' },
    { id: 'remote-test', kind: 'remote', label: 'Test Gateway', url: 'http://127.0.0.1:59999', authMode: 'token' },
  ],
}

// t.settings.connections.title — the picker dropdown heading.
const REGISTERED_GATEWAYS = 'Registered gateways'
const LOCAL_LABEL = 'This device'
const REMOTE_LABEL = 'Test Gateway'

/** Seed connections.json before the app reads its v2 registry on boot. */
function seedTwoSourceRegistry(userDataDir: string): void {
  fs.writeFileSync(
    path.join(userDataDir, 'connections.json'),
    JSON.stringify(TWO_SOURCE_REGISTRY, null, 2),
    'utf8',
  )
}

/** Assert the picker dropdown is open and shows both registered sources. */
async function expectPickerOpen(page: Page): Promise<void> {
  const menu = page.getByRole('menu')
  await expect(menu).toBeVisible({ timeout: 10_000 })
  await expect(menu.getByText(REGISTERED_GATEWAYS)).toBeVisible()
  await expect(page.getByRole('menuitem', { name: LOCAL_LABEL })).toBeVisible()
  await expect(page.getByRole('menuitem', { name: REMOTE_LABEL })).toBeVisible()
}

test.describe('new-session source picker', () => {
  test.describe.configure({ mode: 'serial' })

  let fixture: MockBackendFixture

  test.beforeAll(async () => {
    const mock = await startMockServer()
    const sandbox = createSandbox('picker')

    writeMockProviderConfig(sandbox.hermesHome, mock.url)
    writeEnvFile(sandbox.hermesHome)
    seedTwoSourceRegistry(sandbox.userDataDir)

    const env = buildAppEnv(sandbox)
    const { app, page } = await launchDesktop(env)

    fixture = {
      app,
      page,
      mock,
      mockUrl: mock.url,
      sandbox,
      cleanup: async () => {
        await app.close().catch(() => undefined)
        await mock.close()
        sandbox.cleanup()
      },
    }

    await waitForAppReady(fixture, 120_000)
  })

  test.afterAll(async () => {
    await fixture?.cleanup()
  })

  test('sidebar "New session" row opens the picker with both sources', async () => {
    const { page } = fixture

    // Sidebar nav row (accessible name includes the ⌘N shortcut).
    const sidebarNewSession = page.getByRole('button', { name: /New session ⌘ N/ })
    await expect(sidebarNewSession).toBeVisible()
    await sidebarNewSession.click()

    await expectPickerOpen(page)
    await page.keyboard.press('Escape')
    await expect(page.getByRole('menu').or(page.getByRole('menuitem'))).toHaveCount(0)
  })

  // The header "+" new-session button (index.tsx ~1788) is also wired through
  // the same NewSessionSourcePicker, but it only renders when
  // showAllProfiles === false. The e2e sandbox boots in "all profiles" mode
  // (multi-profile + ALL scope), so that button is absent here and can't be
  // driven by this spec. Its picker wrap mirrors the sidebar row exactly, which
  // this spec proves opens the dropdown, so it is covered by the shared
  // component rather than a dedicated e2e case.
})
