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
  const activeTranscript = page.locator('[data-slot="aui_thread-viewport"]:visible').last()

  // A new draft briefly coexists with the previous session while the renderer
  // switches context. Wait for the new empty transcript instead of sleeping.
  await expect(activeTranscript).not.toContainText(REPLY, { timeout: 10_000 })

  const composer = page.locator('[contenteditable="true"]:visible').last()
  await composer.waitFor({ state: 'visible', timeout: 10_000 })
  await expect.poll(() => composer.textContent(), { timeout: 10_000 }).toBe('')
  await composer.click()
  await expect(composer).toBeFocused()
  await composer.type(text, { delay: 20 })
  await page.keyboard.press('Enter')

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

test('reveals a close control and closes an identified inactive tab without changing selection', async ({
  playwright: _playwright
}, testInfo) => {
  const page = fixture!.page

  await sendMessage(page, 'first close-button session')
  await page.locator('[data-slot="sidebar"] button[aria-label="New session"]').first().click()
  await sendMessage(page, 'second close-button session')

  const sessionRows = page.locator('[data-slot="sidebar"] button:has([data-reorder-handle])')
  await expect(sessionRows).toHaveCount(2)

  // Dispatch the ctrl-modified click directly so macOS does not translate the
  // gesture into a native context click before React receives it.
  await sessionRows.last().dispatchEvent('click', { ctrlKey: true })

  const closeButtons = page.locator('[data-tree-tab] > button[aria-label="Close tab"]')
  await expect.poll(() => closeButtons.count()).toBeGreaterThan(1)

  const tabList = closeButtons.first().locator('xpath=ancestor::*[@role="tablist"][1]')
  const tabItems = tabList.locator('[data-tree-tab]')
  const selectedTab = tabList.locator('[data-tree-tab] > [role="tab"][aria-selected="true"]')

  const inactiveTab = tabList
    .locator('[data-tree-tab]:has(> button[aria-label="Close tab"]) > [role="tab"][aria-selected="false"]')
    .first()

  await expect(selectedTab).toHaveCount(1)
  await expect(inactiveTab).toHaveCount(1)

  const selectedTabItem = selectedTab.locator('xpath=..')
  const inactiveTabItem = inactiveTab.locator('xpath=..')
  const selectedTabId = await selectedTabItem.getAttribute('data-tree-tab')
  const inactiveTabId = await inactiveTabItem.getAttribute('data-tree-tab')

  if (!selectedTabId || !inactiveTabId) {
    throw new Error('Expected stacked pane tabs to expose stable data-tree-tab identities')
  }

  expect(inactiveTabId).not.toBe(selectedTabId)

  const activeTranscript = page.locator('[data-slot="aui_thread-viewport"]:visible').last()
  await expect(activeTranscript).toContainText('first close-button session')

  const closeButton = inactiveTabItem.locator('button[aria-label="Close tab"]')
  const closeIcon = closeButton.locator('[data-slot="pane-tab-close-icon"]')
  await expect(closeIcon).toHaveCSS('opacity', '0')

  await inactiveTabItem.hover()
  await expect(closeIcon).toHaveCSS('opacity', '1')
  await page.screenshot({ path: testInfo.outputPath('tab-close-hover.png') })

  await closeButton.click()
  await expect
    .poll(() => tabItems.evaluateAll(items => items.map(item => item.getAttribute('data-tree-tab'))))
    .not.toContain(inactiveTabId)

  const remainingSelectedTab = tabList.locator('[data-tree-tab] > [role="tab"][aria-selected="true"]')
  await expect(remainingSelectedTab).toHaveCount(1)
  await expect(remainingSelectedTab.locator('xpath=..')).toHaveAttribute('data-tree-tab', selectedTabId)
  await expect(activeTranscript).toContainText('first close-button session')
})
