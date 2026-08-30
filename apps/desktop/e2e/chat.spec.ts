/**
 * E2E chat tests — send a message and verify a response appears.
 *
 * Requires the full boot chain to complete (hermes serve + mock inference
 * provider). The mock server returns a canned reply, so we verify the
 * response text shows up in the chat transcript.
 *
 * Prerequisite: `npm run build` must have been run so dist/ exists.
 */

import { expect, test } from './test'

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'
import { BLOCKING_CLARIFY_QUESTION, BLOCKING_CLARIFY_TRIGGER, MOCK_REPLY } from './mock-server'
import { expectVisualSnapshot } from './visual-snapshot'

let fixture: MockBackendFixture | null = null

test.beforeAll(async () => {
  fixture = await setupMockBackend()
  await waitForAppReady(fixture!, 120_000)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test.describe('chat interaction with mock backend', () => {
  test('send a message and receive a response', async () => {
    const page = fixture!.page

    // Find the composer — it's a contenteditable textbox.
    const composer = page.locator('[contenteditable="true"]').first()
    await composer.waitFor({ state: 'visible', timeout: 10_000 })

    // Click to focus, then type the message character by character.
    // Using `type` instead of `fill` because the composer is a
    // contenteditable div with custom keydown handling that tracks
    // IME composition state — `fill` bypasses the event chain.
    await composer.click()
    await composer.type('Hello, can you hear me?', { delay: 20 })

    // Submit with Enter — the composer's keydown handler intercepts
    // plain Enter (without Shift) and calls submitDraft().
    await page.keyboard.press('Enter')

    // Wait for the user's message to appear in the transcript.
    // The message renders as an assistant-ui message in the chat view.
    await page.waitForFunction(
      () => {
        const body = document.body

        if (!body) {
          return false
        }

        return (body.textContent ?? '').includes('Hello, can you hear me?')
      },
      undefined,
      { timeout: 15_000 }
    )

    // Wait for the mock response to appear. The canned reply is:
    // "Hello from the mock inference server! The full boot chain is working."
    // Give it a generous timeout — the inference request goes through the
    // gateway → hermes serve → mock server → streaming SSE back.
    await page.waitForFunction(
      () => {
        const body = document.body

        if (!body) {
          return false
        }

        const text = body.textContent ?? ''

        return text.includes('mock inference server') || text.includes('boot chain is working')
      },
      undefined,
      { timeout: 60_000 }
    )
  })

  test('jumps from a sticky user prompt to the start of its answer', async ({}, testInfo) => {
    const page = fixture!.page
    const composer = page.locator('[contenteditable="true"]').first()

    await composer.click()
    await composer.type('E2E jump to answer start')
    await page.keyboard.press('Enter')
    await expect(page.getByText(MOCK_REPLY).last()).toBeVisible({ timeout: 60_000 })

    const turn = page.locator('[data-slot="aui_turn-pair"]').last()
    const prompt = turn.locator('[data-slot="aui_user-message-root"]')
    const answer = turn.locator('[data-slot="aui_assistant-message-root"]')
    const viewport = page.locator('[data-slot="aui_thread-viewport"]')

    // The mock reply is intentionally short. Give this real rendered answer a
    // long body for the navigation check without adding a special inference
    // protocol or changing production data.
    await answer.evaluate(element => {
      element.style.minHeight = '2400px'
    })
    await viewport.evaluate(element => {
      element.scrollTop = element.scrollHeight
      element.dispatchEvent(new Event('scroll'))
    })

    await prompt.hover()
    const jump = turn.getByRole('button', { name: 'Jump to answer start' })

    await expect(jump).toBeVisible()
    await expect(jump.locator('svg.tabler-icon-arrow-bar-up')).toBeVisible()
    await expect(jump.locator('..')).toHaveCSS('opacity', '1')
    await page.screenshot({ path: testInfo.outputPath('answer-start-jump-hover.png') })
    await jump.click()

    await expect
      .poll(async () => {
        const promptBox = await prompt.boundingBox()
        const answerBox = await answer.boundingBox()

        if (!promptBox || !answerBox) {
          return Number.POSITIVE_INFINITY
        }

        return Math.abs(answerBox.y - (promptBox.y + promptBox.height))
      })
      .toBeLessThan(3)
  })

  test('screenshot of chat with messages', async () => {
    await expectVisualSnapshot(fixture!.page, { name: 'chat-with-messages', app: fixture!.app })
  })

  test('offers stop, steer, and queue actions while busy', async ({}, testInfo) => {
    const page = fixture!.page
    const composer = page.locator('[contenteditable="true"]').first()
    const primary = page.locator('[data-slot="composer-root"] button[type="submit"]')
    const queue = page.locator('[data-slot="composer-root"] button[aria-label="Queue message"]')
    const dictation = page.locator('[data-slot="composer-root"] button[aria-label="Voice dictation"]')
    const speakReplies = page.locator(
      '[data-slot="composer-root"] button[aria-label="Read replies aloud"], [data-slot="composer-root"] button[aria-label="Stop reading replies aloud"]'
    )

    await composer.click()
    await composer.type(BLOCKING_CLARIFY_TRIGGER)
    await page.keyboard.press('Enter')
    await page.getByText(BLOCKING_CLARIFY_QUESTION).waitFor({ state: 'visible', timeout: 30_000 })

    await expect(primary).toHaveAttribute('aria-label', 'Stop')
    await expect(primary.locator('span')).toHaveClass(/bg-current/)

    await composer.click()
    await composer.type('please answer tersely')
    await expect(primary).toHaveAttribute('aria-label', /Steer/)
    await expect(dictation).toBeVisible()
    await expect(speakReplies).toBeVisible()
    await expect(queue).toBeVisible()
    await expect(queue.locator('svg.tabler-icon-layers-intersect-2')).toBeVisible()
    const controlLabels = await page
      .locator('[data-slot="composer-root"] button')
      .evaluateAll(buttons => buttons.map(button => button.getAttribute('aria-label')))
    const speakRepliesIndex = controlLabels.findIndex(
      label => label === 'Read replies aloud' || label === 'Stop reading replies aloud'
    )
    expect(controlLabels.indexOf('Voice dictation')).toBeLessThan(speakRepliesIndex)
    expect(speakRepliesIndex).toBeLessThan(controlLabels.indexOf('Queue message'))
    expect(controlLabels.indexOf('Queue message')).toBeLessThan(
      controlLabels.findIndex(label => label?.startsWith('Steer'))
    )
    await page.screenshot({ path: testInfo.outputPath('busy-composer-steer.png') })
    await expect(primary.locator('svg.tabler-icon-steering-wheel')).toBeVisible()

    await queue.click()
    await expect(primary).toHaveAttribute('aria-label', 'Stop')
    await expect(queue).toHaveCount(0)
    await page.screenshot({ path: testInfo.outputPath('busy-composer-queue.png') })
    await expect(page.getByText('1 Queued')).toBeVisible()

    await primary.click()
    await expect(page.getByText('1 Queued — paused')).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath('busy-composer-queue-paused.png') })
  })
})
