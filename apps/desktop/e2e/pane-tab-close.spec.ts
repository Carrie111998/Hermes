/**
 * E2E coverage for directly closing stacked session tabs.
 *
 * Prerequisite: `npm run build` must have been run so dist/ exists.
 */

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'
import { expect, test } from './test'

let fixture: MockBackendFixture | null = null

const REPLY = 'Hello from the mock inference server! The full boot chain is working.'

async function sendMessage(page: MockBackendFixture['page'], text: string): Promise<void> {
  const composer = page.locator('[contenteditable="true"]:visible').last()
  await composer.waitFor({ state: 'visible', timeout: 10_000 })
  await expect.poll(() => composer.textContent(), { timeout: 10_000 }).toBe('')
  await page.waitForTimeout(300)
  await composer.click()
  await expect(composer).toBeFocused()
  await composer.type(text, { delay: 20 })
  await page.keyboard.press('Enter')

  const activeTranscript = page.locator('[data-slot="aui_thread-viewport"]:visible').last()
  await expect(activeTranscript).toContainText(text, { timeout: 15_000 })
  await expect(activeTranscript).toContainText(REPLY, { timeout: 60_000 })
}

test.beforeAll(async () => {
  fixture = await setupMockBackend()
  await waitForAppReady(fixture, 120_000)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test('reveals a close control on hover and closes only that tab', async ({ playwright: _playwright }, testInfo) => {
  const page = fixture!.page

  await sendMessage(page, 'first close-button session')
  await page.locator('[data-slot="sidebar"] button[aria-label="New session"]').first().click()
  await sendMessage(page, 'second close-button session')

  const sessionRows = page.locator('[data-slot="sidebar"] button:has([data-reorder-handle])')
  await expect(sessionRows).toHaveCount(2)

  // Dispatch the ctrl-modified click directly so macOS does not translate the
  // gesture into a native context click before React receives it.
  await sessionRows.last().dispatchEvent('click', { ctrlKey: true })

  const activeTab = page
    .locator('[role="tab"][aria-selected="true"]:visible')
    .filter({ has: page.locator('button[aria-label="Close tab"]') })

  await expect(activeTab).toHaveCount(1)

  const tabList = activeTab.locator('xpath=ancestor::*[@role="tablist"][1]')
  const closeButtons = tabList.locator('button[aria-label="Close tab"]')
  await expect.poll(() => closeButtons.count()).toBeGreaterThan(1)
  const openTabCount = await closeButtons.count()

  const closeButton = activeTab.locator('button[aria-label="Close tab"]')
  const closeIcon = closeButton.locator('[data-slot="pane-tab-close-icon"]')
  await expect(closeIcon).toHaveCSS('opacity', '0')

  await activeTab.hover()
  await expect(closeIcon).toHaveCSS('opacity', '1')
  await page.screenshot({ path: testInfo.outputPath('tab-close-hover.png') })

  await closeButton.click()
  await expect(closeButtons).toHaveCount(openTabCount - 1)
})
