/**
 * E2E single-select clarify test — one choice click must answer and resume the
 * turn without a redundant Continue action.
 */

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'
import { BLOCKING_CLARIFY_QUESTION, BLOCKING_CLARIFY_TRIGGER } from './mock-server'
import { expect, test } from './test'

let fixture: MockBackendFixture | null = null

test.beforeAll(async () => {
  fixture = await setupMockBackend()
  await waitForAppReady(fixture!, 120_000)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test('single-select clarify submits and settles on the first click', async () => {
  const page = fixture!.page
  const composer = page.locator('[contenteditable="true"]').first()

  await composer.waitFor({ state: 'visible', timeout: 10_000 })
  await composer.click()
  await composer.type(BLOCKING_CLARIFY_TRIGGER, { delay: 20 })
  await page.keyboard.press('Enter')

  const card = page.locator('form[data-clarify-choices]')

  await card.getByText(BLOCKING_CLARIFY_QUESTION).waitFor({ state: 'visible', timeout: 60_000 })
  await expect(card.getByRole('button', { name: /Continue/ })).toHaveCount(0)
  await card.getByRole('button', { name: /Yes/ }).click()

  const settled = page.locator('[data-clarify-settled]')

  await settled.waitFor({ state: 'visible', timeout: 30_000 })
  await expect(settled.getByText(BLOCKING_CLARIFY_QUESTION)).toBeVisible()
  await expect(settled.locator('[data-clarify-answer]')).toHaveText('Yes')
  await expect(page.locator('form[data-clarify-choices]')).toHaveCount(0)
})
