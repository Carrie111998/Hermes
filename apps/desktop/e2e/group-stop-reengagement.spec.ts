import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'
import { expect, test } from './test'

const HELD_TASK = 'E2E_GROUP_STOP_HELD_TASK'

let fixture: MockBackendFixture | null = null

async function openBots(page: MockBackendFixture['page']): Promise<void> {
  const tab = page
    .getByRole('button', { name: 'Bots', exact: true })
    .or(page.getByRole('tab', { name: 'Bots', exact: true }))
    .first()

  await tab.click()
  await expect(page.getByRole('button', { name: 'New bot or group chat' })).toBeVisible()
}

async function createAgent(page: MockBackendFixture['page'], name: string, title: string): Promise<void> {
  await page.getByRole('button', { name: 'New bot or group chat' }).click()
  await page.getByRole('menuitem', { name: 'New Bot' }).click()

  const dialog = page.getByRole('dialog', { name: 'New Bot' })

  await dialog.getByPlaceholder('inbox-triage').fill(name)
  await dialog.getByPlaceholder('Inbox Triage').fill(title)
  await dialog.getByRole('button', { name: 'Create Bot' }).click()
  await expect(dialog).toBeHidden({ timeout: 30_000 })
  await expect(page.getByRole('button', { name: new RegExp(`^${title}\\b`) }).first()).toBeVisible({ timeout: 30_000 })
}

async function sendRoomMessage(page: MockBackendFixture['page'], room: string, text: string): Promise<void> {
  const composer = page.getByRole('textbox', { name: `Message ${room}` }).filter({ visible: true })

  await composer.click()
  await composer.pressSequentially(text)
  await page.keyboard.press('Enter')
}

test.beforeAll(async () => {
  fixture = await setupMockBackend({
    mockServer: { holdFirstCompletionContaining: HELD_TASK },
  })
  await waitForAppReady(fixture, 120_000)
})

test.afterAll(async () => {
  fixture?.mock.releaseHeldStream()
  await fixture?.cleanup()
  fixture = null
})

test('an actionable @all task re-engages every bot after Stop', async () => {
  test.setTimeout(180_000)
  const page = fixture!.page
  const room = 'Architect, Auditor'

  await openBots(page)
  await createAgent(page, 'architect', 'Architect')
  await createAgent(page, 'auditor', 'Auditor')

  await page.getByRole('button', { name: 'New bot or group chat' }).click()
  await page.getByRole('menuitem', { name: 'New Group Chat' }).click()

  const dialog = page.getByRole('dialog', { name: 'New Group Chat' })

  for (const title of ['Architect', 'Auditor']) {
    await dialog.getByText(title, { exact: true }).locator('xpath=ancestor::label').getByRole('checkbox').click()
  }

  await dialog.getByRole('textbox', { name: 'Group name' }).fill(room)
  await dialog.getByRole('button', { name: 'Create Group (2)' }).click()
  await expect(page.getByRole('textbox', { name: `Message ${room}` }).filter({ visible: true })).toBeVisible({ timeout: 20_000 })

  await sendRoomMessage(page, room, HELD_TASK)
  await fixture!.mock.waitForHeldCompletion()
  await page.getByRole('button', { name: 'Stop', exact: true }).first().click()

  const holdStatus = page.locator('[data-slot="group-hold-status"]')

  await expect(holdStatus).toBeVisible()
  await expect(holdStatus).toContainText(/paused/i)

  await sendRoomMessage(page, room, '@all review the stopped task')

  await expect(holdStatus).toBeHidden({ timeout: 20_000 })
  const botReplies = page.getByText('Hello from the mock inference server!', { exact: false })

  await expect(botReplies).toHaveCount(2, { timeout: 60_000 })
})
