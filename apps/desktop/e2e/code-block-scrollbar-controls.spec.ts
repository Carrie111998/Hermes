/**
 * Regression coverage for native Linux/Windows code-block scrollbars covering
 * the copy and expand controls. This boots a fully isolated Electron instance
 * and exercises the real markdown renderer with vertical overflow.
 */

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'
import { expect, test } from './test'

const PROMPT = 'Render the scrollbar-control regression fixture.'

const CODE_REPLY = ['```bash', ...Array.from({ length: 18 }, (_, index) => `echo line-${index + 1}`), '```'].join(
  '\n',
)

let fixture: MockBackendFixture | null = null

test.setTimeout(180_000)

test.beforeAll(async () => {
  fixture = await setupMockBackend({ mockServer: { reply: CODE_REPLY } })
  await waitForAppReady(fixture, 120_000)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

// Playwright requires an object destructuring pattern even when this spec owns its Electron fixture.
// eslint-disable-next-line no-empty-pattern
test('keeps code-block controls clear of a native vertical scrollbar', async ({}, testInfo) => {
  const { page } = fixture!
  const composer = page.locator('[contenteditable="true"]').first()

  await composer.click()
  await composer.fill(PROMPT)
  await page.keyboard.press('Enter')

  const card = page.locator('[data-slot="code-card"]').last()
  await expect(card).toBeVisible({ timeout: 60_000 })
  await expect(card).toContainText('echo line-18', { timeout: 60_000 })
  await card.hover()
  await expect(card.getByRole('button', { name: 'Copy code' })).toBeVisible()
  await expect(card.getByRole('button', { name: 'Expand' })).toBeVisible()

  const geometry = await card.evaluate(element => {
    const scroller = element.querySelector<HTMLElement>('.overflow-y-auto')
    const toggle = element.querySelector<HTMLElement>('button[aria-label="Expand"]')
    const copy = element.querySelector<HTMLElement>('button[aria-label="Copy code"]')

    if (!scroller || !toggle || !copy) {
      throw new Error('Code-card scrollbar controls were not rendered')
    }

    const scrollRect = scroller.getBoundingClientRect()
    const toggleRect = toggle.getBoundingClientRect()
    const copyRect = copy.getBoundingClientRect()
    const rootFontSize = Number.parseFloat(getComputedStyle(document.documentElement).fontSize)

    return {
      verticalOverflow: scroller.scrollHeight > scroller.clientHeight,
      preservesNativeOverlayClass: scroller.classList.contains('scrollbar-overlay'),
      verticalGutter: scroller.offsetWidth - scroller.clientWidth,
      copyBaseInset: rootFontSize * 0.375,
      copyRightGap: scrollRect.right - copyRect.right,
      toggleRightGap: scrollRect.right - toggleRect.right,
    }
  })

  expect(geometry.verticalOverflow).toBe(true)
  expect(geometry.preservesNativeOverlayClass).toBe(true)
  expect(geometry.verticalGutter).toBeGreaterThan(0)
  expect(Math.abs(geometry.copyRightGap - (geometry.copyBaseInset + geometry.verticalGutter))).toBeLessThanOrEqual(0.5)
  expect(Math.abs(geometry.toggleRightGap - geometry.verticalGutter)).toBeLessThanOrEqual(0.5)

  await page.screenshot({ path: testInfo.outputPath('code-block-scrollbar-controls.png') })
})
