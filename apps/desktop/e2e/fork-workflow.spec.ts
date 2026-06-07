/**
 * Full Desktop regression for branching from an exact message boundary.
 *
 * The parent history is created through the real gateway, Desktop forks it
 * through the message action, and the child's first turn goes through a real
 * child runtime to the isolated mock provider. Request capture proves that
 * the child inherited only the selected prefix; the UI assertions prove the
 * child surface opened and the in-memory origin link fronts the existing
 * parent surface.
 */

import { type ElectronApplication, expect, type Page, test } from './test'

import {
  buildAppEnv,
  createSandbox,
  launchDesktop,
  type Sandbox,
  waitForAppReady,
  writeEnvFile,
  writeMockProviderConfig
} from './fixtures'
import { MOCK_REPLY, startMockServer, type MockServer } from './mock-server'
import { RealSessionBuilder } from './real-session-builder'

const FIRST_PARENT_PROMPT = 'E2E fork parent turn one'
const LATER_PARENT_PROMPT = 'E2E fork parent turn two must stay out of the child'
const CHILD_PROMPT = 'E2E fork child first prompt'

interface ForkFixture {
  app: ElectronApplication
  mock: MockServer
  page: Page
  parentSessionId: string
  sandbox: Sandbox
  cleanup: () => Promise<void>
}

async function setupForkFixture(): Promise<ForkFixture> {
  const mock = await startMockServer()
  const sandbox = createSandbox('fork-workflow')
  writeMockProviderConfig(sandbox.hermesHome, mock.url)
  writeEnvFile(sandbox.hermesHome)

  const builder = await RealSessionBuilder.start(sandbox.hermesHome)
  let parentSessionId: string

  try {
    const parent = await builder.createSession({
      title: FIRST_PARENT_PROMPT,
      turns: [FIRST_PARENT_PROMPT, LATER_PARENT_PROMPT]
    })
    parentSessionId = parent.sessionId
  } finally {
    await builder.close()
  }

  const { app, page } = await launchDesktop(buildAppEnv(sandbox))

  return {
    app,
    mock,
    page,
    parentSessionId,
    sandbox,
    cleanup: async () => {
      await app.close().catch(() => undefined)
      await mock.close()
      sandbox.cleanup()
    }
  }
}

function transcript(surface: ReturnType<Page['locator']>) {
  return surface.locator('[data-slot="aui_thread-viewport"]')
}

function requestMessages(request: Record<string, unknown>): Array<{ content?: unknown; role?: unknown }> {
  return Array.isArray(request.messages) ? request.messages : []
}

test('forks at an exact message, routes the child turn, and returns to the open parent', async ({}, testInfo) => {
  test.slow()

  const fixture = await setupForkFixture()

  try {
    const { mock, page, parentSessionId } = fixture
    await waitForAppReady(fixture, 120_000)

    const parentRow = page.locator('[data-slot="sidebar"] button').filter({ hasText: FIRST_PARENT_PROMPT }).first()
    await parentRow.waitFor({ state: 'visible', timeout: 60_000 })
    await parentRow.click()

    const parentSurface = page.locator('[data-session-anchor="workspace"]')
    const parentTranscript = transcript(parentSurface)
    await expect(parentTranscript).toContainText(LATER_PARENT_PROMPT, { timeout: 30_000 })

    const firstAnswer = parentSurface
      .locator('[data-slot="aui_assistant-message-root"]')
      .filter({ hasText: MOCK_REPLY })
      .first()
    await firstAnswer.hover()
    await firstAnswer.getByRole('button', { name: 'Branch in new chat' }).click()

    const childTab = page.locator('[data-tree-tab^="session-tile:"][aria-selected="true"]')
    await expect(childTab).toBeVisible({ timeout: 30_000 })
    const childPaneId = await childTab.getAttribute('data-tree-tab')
    expect(childPaneId).toMatch(/^session-tile:.+/)

    const childStoredSessionId = childPaneId!.slice('session-tile:'.length)
    expect(childStoredSessionId).not.toBe(parentSessionId)

    const childSurface = page.locator(`[data-session-anchor="${childPaneId}"]`)
    const childTranscript = transcript(childSurface)
    await expect(childSurface).toBeVisible({ timeout: 30_000 })
    await expect(childTranscript).toContainText(FIRST_PARENT_PROMPT)
    await expect(childTranscript).toContainText(MOCK_REPLY)
    await expect(childTranscript).not.toContainText(LATER_PARENT_PROMPT)

    const origin = childSurface.getByRole('button', { name: /Open source conversation/ })
    await expect(origin).toBeVisible()

    const requestsBeforeChildPrompt = mock.receivedRequests.length
    const childComposer = childSurface.locator('[contenteditable="true"]').first()
    await childComposer.click()
    await childComposer.type(CHILD_PROMPT, { delay: 2 })
    await page.keyboard.press('Enter')

    await expect(childTranscript).toContainText(CHILD_PROMPT, { timeout: 15_000 })
    await expect
      .poll(
        () =>
          mock.receivedRequests
            .slice(requestsBeforeChildPrompt)
            .some(request => requestMessages(request).some(message => message.content === CHILD_PROMPT)),
        { timeout: 60_000 }
      )
      .toBe(true)

    const childRequest = mock.receivedRequests
      .slice(requestsBeforeChildPrompt)
      .find(request => requestMessages(request).some(message => message.content === CHILD_PROMPT))
    expect(childRequest, 'the child runtime should submit the first child prompt').toBeDefined()
    const childRequestText = requestMessages(childRequest!)
      .map(message => (typeof message.content === 'string' ? message.content : ''))
      .join('\n')
    expect(childRequestText).toContain(FIRST_PARENT_PROMPT)
    expect(childRequestText).toContain(CHILD_PROMPT)
    expect(childRequestText).not.toContain(LATER_PARENT_PROMPT)

    await expect(
      childSurface.locator('[data-slot="aui_assistant-message-root"]').filter({ hasText: MOCK_REPLY })
    ).toHaveCount(2, { timeout: 60_000 })
    await page.screenshot({ path: testInfo.outputPath('fork-child-first-turn.png') })

    await origin.click()

    const workspaceTab = page.locator('[data-tree-tab="workspace"]')
    await expect(workspaceTab).toHaveAttribute('aria-selected', 'true')
    await expect(parentSurface).toBeVisible()
    await expect(parentTranscript).toContainText(FIRST_PARENT_PROMPT)
    await expect(parentTranscript).toContainText(LATER_PARENT_PROMPT)
    await expect(parentTranscript).not.toContainText(CHILD_PROMPT)
    await expect(page.locator(`[data-tree-tab="${childPaneId}"]`)).toHaveCount(1)
    await page.screenshot({ path: testInfo.outputPath('fork-returned-to-parent.png') })
  } finally {
    await fixture.cleanup()
  }
})
