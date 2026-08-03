/**
 * Managed-product E2E smoke.
 *
 * Unlike upstream's local-backend fixtures, evaOS Agent is remote-only. This
 * test launches the real built Electron main/renderer without enrollment and
 * proves it stays on the Electric Sheep sign-in boundary.
 */

import { allowErrorBanners, expect, test } from './test'

import { type ManagedSignedOutFixture, setupManagedSignedOut } from './fixtures'

let fixture: ManagedSignedOutFixture | null = null

test.beforeAll(async () => {
  fixture = await setupManagedSignedOut()
})

test.beforeEach(() => {
  // The shared error-banner collector performs a renderer evaluate from
  // afterEach. Electron's Linux CI transport can leave that diagnostic read
  // pending even after this remote-only screen has rendered and all in-test
  // page operations have completed. Assert the same no-alert contract inside
  // each managed smoke instead, where it remains part of the test timeout and
  // cannot strand teardown.
  allowErrorBanners()
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test.describe('managed signed-out boot', () => {
  test('uses the evaOS Agent product identity', async () => {
    await expect(fixture!.page).toHaveTitle('evaOS Agent')
    expect(await fixture!.page.locator('[role="alert"]').count()).toBe(0)
  })

  test('requires Electric Sheep enrollment without local-backend fallback', async () => {
    const page = fixture!.page

    await expect(page.getByRole('heading', { name: 'Managed by Electric Sheep' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Sign in to evaOS Agent' })).toBeVisible()

    const status = await page.evaluate(() => {
      const desktop = Reflect.get(window, 'hermesDesktop') as {
        eva: { status: () => Promise<Record<string, unknown>> }
      }

      return desktop.eva.status()
    })

    expect(status).toMatchObject({
      managed: true,
      productName: 'evaOS Agent',
      desktopSessionActive: false,
      runtimeSessionActive: false,
      customerId: null,
      agentId: null,
      updateChannel: 'managed-beta',
    })
    expect(await page.locator('[role="alert"]').count()).toBe(0)
  })
})
