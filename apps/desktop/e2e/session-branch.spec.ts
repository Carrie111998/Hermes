import {
  buildAppEnv,
  createSandbox,
  launchDesktop,
  type MockBackendFixture,
  waitForAppReady,
  writeEnvFile,
  writeMockProviderConfig
} from './fixtures'
import { startMockServer } from './mock-server'
import { RealSessionBuilder } from './real-session-builder'
import { expect, test } from './test'

const SESSION_LABEL = 'E2E persisted conversation branch parent'

async function setupSeededBackend(): Promise<MockBackendFixture> {
  const mock = await startMockServer()
  const sandbox = createSandbox('session-branch')

  writeMockProviderConfig(sandbox.hermesHome, mock.url)
  writeEnvFile(sandbox.hermesHome)

  const builder = await RealSessionBuilder.start(sandbox.hermesHome)

  try {
    await builder.createSession({ title: SESSION_LABEL, turns: [SESSION_LABEL] })
  } finally {
    await builder.close()
  }

  const { app, page } = await launchDesktop(buildAppEnv(sandbox))

  return {
    app,
    page,
    mock,
    mockUrl: mock.url,
    sandbox,
    cleanup: async () => {
      await app.close()
      await mock.close()
      sandbox.cleanup()
    }
  }
}

test.describe('persisted session branching', () => {
  let fixture: MockBackendFixture

  test.beforeAll(async () => {
    fixture = await setupSeededBackend()
    await waitForAppReady(fixture, 120_000)
  })

  test.afterAll(async () => {
    await fixture?.cleanup()
  })

  test('branches from the visible session-row action and renders the nested child', async () => {
    const parentLabel = fixture.page.getByText(SESSION_LABEL, { exact: true }).first()

    await expect(parentLabel).toBeVisible({ timeout: 30_000 })

    const parentRow = parentLabel.locator('xpath=ancestor::div[contains(@class, "row-hover")]').first()

    await parentRow.hover()
    await parentRow.getByRole('button', { name: 'Session actions' }).click()
    await fixture.page.getByRole('menuitem', { name: 'Branch', exact: true }).click()

    const childLabel = fixture.page.getByText(/draft: branch #\d+/i).first()

    await expect(childLabel).toBeVisible({ timeout: 30_000 })
    await expect(parentLabel).toBeVisible()
  })
})
