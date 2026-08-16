import {
  type MockBackendFixture,
  setupMockBackend,
  waitForAppReady,
} from './fixtures'
import { collectErrorBanners, expect, installErrorBannerGuard, type Page, test } from './test'

const TARGET_PROFILE = 'window-target'

async function createNamedProfile(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'New profile' }).click()
  await page.locator('#new-profile-name').fill(TARGET_PROFILE)
  await page.getByRole('button', { name: 'Create profile' }).click()
  await expect(page.getByRole('dialog', { name: 'New profile' })).toBeHidden({ timeout: 30_000 })
  await expect(page.getByRole('button', { name: TARGET_PROFILE, exact: true })).toHaveAttribute('aria-pressed', 'true')
}

test.describe('profile context menu — New Window', () => {
  test.describe.configure({ mode: 'serial' })

  let fixture: MockBackendFixture

  test.beforeAll(async () => {
    fixture = await setupMockBackend()
    await waitForAppReady(fixture, 120_000)
  })

  test.afterAll(async () => {
    await fixture?.cleanup()
  })

  test('opens a full peer window pinned to the clicked profile without changing the original', async () => {
    const { app, page } = fixture

    await createNamedProfile(page)

    await page.getByRole('button', { name: 'Switch to default' }).click()
    await expect(page.getByRole('button', { name: TARGET_PROFILE, exact: true })).toHaveAttribute('aria-pressed', 'false')
    await expect(page.getByRole('button', { name: 'Show all profiles' })).toHaveAttribute('aria-pressed', 'true')

    const targetProfile = page.getByRole('button', { name: TARGET_PROFILE, exact: true })
    await targetProfile.click({ button: 'right' })

    const menu = page.getByRole('menu', { name: 'Actions' })
    await expect(menu).toBeVisible()
    const items = menu.getByRole('menuitem')
    await expect(items.first()).toHaveText('New Window')

    expect(app.windows()).toHaveLength(1)
    const peerPromise = app.waitForEvent('window')
    await items.first().click()
    const peer = await peerPromise
    installErrorBannerGuard(peer)
    await waitForAppReady({ ...fixture, page: peer }, 120_000)

    await expect.poll(() => app.windows().length).toBe(2)
    const peerUrl = new URL(peer.url())
    expect(peerUrl.searchParams.get('profile')).toBe(TARGET_PROFILE)
    expect(peerUrl.searchParams.has('win')).toBe(false)

    await expect(peer.locator('[data-slot="profile-rail"]')).toBeVisible()
    await expect(peer.locator('textarea, [contenteditable="true"]').first()).toBeVisible()
    await expect(peer.getByRole('button', { name: TARGET_PROFILE, exact: true })).toHaveAttribute('aria-pressed', 'true')

    await expect(targetProfile).toHaveAttribute('aria-pressed', 'false')
    await expect(page.getByRole('button', { name: 'Show all profiles' })).toHaveAttribute('aria-pressed', 'true')
    expect(new URL(page.url()).searchParams.has('profile')).toBe(false)

    expect(await collectErrorBanners(page)).toEqual([])
    expect(await collectErrorBanners(peer)).toEqual([])
  })
})
