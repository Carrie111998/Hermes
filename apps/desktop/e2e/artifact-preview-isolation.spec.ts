import { buildAppEnv, createSandbox, launchDesktop, type Sandbox } from './fixtures'
import { type ElectronApplication, expect, type Page, test } from './test'

let app: ElectronApplication | null = null
let page: Page | null = null
let sandbox: Sandbox | null = null

test.beforeAll(async () => {
  sandbox = createSandbox('artifact-isolation')

  const launched = await launchDesktop(
    buildAppEnv(sandbox, {
      HERMES_DESKTOP_BOOT_FAKE: '1',
      HERMES_DESKTOP_BOOT_FAKE_STEP_MS: '20'
    })
  )

  app = launched.app
  page = launched.page
  await page.waitForSelector('body', { state: 'attached' })
})

test.afterAll(async () => {
  await app?.close().catch(() => undefined)
  sandbox?.cleanup()
  app = null
  page = null
  sandbox = null
})

test('artifact webview keeps a busy generated page out of the host renderer', async () => {
  if (!app || !page) {
    throw new Error('Desktop fixture did not launch')
  }

  const runningApp = app
  const hostPage = page

  await hostPage.evaluate(() => {
    const state = window as unknown as {
      __artifactHostTicks?: number
      __artifactHostTimer?: number
    }

    state.__artifactHostTicks = 0
    state.__artifactHostTimer = window.setInterval(() => {
      state.__artifactHostTicks = (state.__artifactHostTicks ?? 0) + 1
    }, 25)

    const guest = document.createElement('webview')
    guest.id = 'artifact-isolation-probe'
    guest.setAttribute('partition', 'hermes-artifact-preview')
    guest.setAttribute('webpreferences', 'contextIsolation=yes,nodeIntegration=no,sandbox=yes,webSecurity=yes')
    guest.setAttribute(
      'src',
      `data:text/html;charset=utf-8,${encodeURIComponent(
        '<!doctype html><title>Busy artifact</title><script>setTimeout(() => { while (true) {} }, 100)</script>'
      )}`
    )
    document.body.appendChild(guest)
  })

  await expect
    .poll(
      () =>
        runningApp.evaluate(({ session, webContents }) => {
          const partition = session.fromPartition('hermes-artifact-preview')

          return webContents.getAllWebContents().some(contents => contents.session === partition)
        }),
      { timeout: 15_000 }
    )
    .toBe(true)

  const guestPreferences = await runningApp.evaluate(({ session, webContents }) => {
    const partition = session.fromPartition('hermes-artifact-preview')

    const guest = webContents
      .getAllWebContents()
      .find(contents => contents.session === partition)

    const preferences = (
      guest as unknown as {
        getLastWebPreferences?(): {
          contextIsolation?: boolean
          nodeIntegration?: boolean
          sandbox?: boolean
          webSecurity?: boolean
        }
      }
    )?.getLastWebPreferences?.()

    return {
      contextIsolation: preferences?.contextIsolation,
      nodeIntegration: preferences?.nodeIntegration,
      sandbox: preferences?.sandbox,
      type: guest?.getType(),
      webSecurity: preferences?.webSecurity
    }
  })

  expect(guestPreferences).toEqual({
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    type: 'webview',
    webSecurity: true
  })

  await hostPage.waitForTimeout(750)
  expect(
    await hostPage.evaluate(() => (window as unknown as { __artifactHostTicks?: number }).__artifactHostTicks ?? 0)
  ).toBeGreaterThan(5)

  await hostPage.evaluate(() => {
    const state = window as unknown as { __artifactHostTimer?: number }

    if (state.__artifactHostTimer) {
      window.clearInterval(state.__artifactHostTimer)
    }

    document.getElementById('artifact-isolation-probe')?.remove()
  })
})
