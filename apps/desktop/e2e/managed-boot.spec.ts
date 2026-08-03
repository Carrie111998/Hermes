/**
 * Managed-product E2E smoke.
 *
 * Unlike upstream's local-backend fixtures, evaOS Agent is remote-only. This
 * test launches the real built Electron main/renderer without enrollment and
 * proves it stays on the Electric Sheep sign-in boundary.
 */

import { expect, test } from './test'

import { type ManagedSignedOutFixture, setupManagedSignedOut } from './fixtures'

let fixture: ManagedSignedOutFixture | null = null

test.beforeAll(async () => {
  // Importing and launching the production Electron bundle can exceed the
  // generic 90 s per-test budget on a cold, single-core Linux runner. Keep the
  // assertions on the normal timeout while giving this one shared launch a
  // bounded allowance; the CI failure artifact showed the fully rendered
  // managed sign-in screen immediately after the hook timed out.
  test.setTimeout(180_000)
  fixture = await setupManagedSignedOut()
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test.describe('managed signed-out boot', () => {
  test('uses the evaOS Agent product identity', async () => {
    await expect(fixture!.page).toHaveTitle('evaOS Agent')
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
  })
})
