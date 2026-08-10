import type { Page } from '@playwright/test'

import {
  buildAppEnv,
  launchDesktop,
  type MockBackendFixture,
  setupMockBackend,
  waitForAppReady
} from './fixtures'
import { expect, test } from './test'

const LAYOUT_TREE_KEY = 'hermes.desktop.layoutTree.v2'
const ACTIVE_PRESET_KEY = 'hermes.desktop.layoutPreset.active'
const COMPOSER_POPOUT_KEY = 'hermes.desktop.composerPopout.zones.v1'
const PANE_STATES_KEY = 'hermes.desktop.paneStates.v1'
const RAN_MODE_KEY = 'hermes.desktop.ranMode.v1'
const RAN_MODE_LOCK_KEY = 'hermes.desktop.ranMode.journal.v1'

const RAN_OWNED_STORAGE_KEYS = [
  ACTIVE_PRESET_KEY,
  'hermes.desktop.composerPopout.zones.v1',
  'hermes.desktop.dismissedPanes.v1',
  LAYOUT_TREE_KEY,
  PANE_STATES_KEY,
  'hermes.desktop.panesFlipped',
  'hermes.desktop.reviewOpen',
  'hermes.desktop.rightRailActiveTab',
  'hermes.desktop.statusbarVisible',
  'hermes.desktop.terminalTakeover',
  'hermes.desktop.toolView.technical',
  'hermes.desktop.userPlacedPanes.v1'
] as const

let fixture: MockBackendFixture | null = null
let rendererErrors: string[] = []
const watchedRendererPages = new WeakSet<Page>()

function watchRendererErrors(page: Page): void {
  if (watchedRendererPages.has(page)) {
    return
  }

  watchedRendererPages.add(page)
  page.on('pageerror', error => rendererErrors.push(`pageerror: ${error.message}`))
  page.on('console', message => {
    if (message.type() === 'error') {
      rendererErrors.push(`console: ${message.text()}`)
    }
  })
}

async function openAppearance(page: Page): Promise<void> {
  const settingsButton = page.getByRole('button', { name: 'Open settings' })
  await settingsButton.click()
  await page.getByRole('button', { name: 'Appearance', exact: true }).click()
  await expect(page.getByTestId('ran-mode-toggle')).toBeVisible()
  await expect(page.getByText('Status Bar', { exact: true })).toBeVisible()
}

async function setRanMode(page: Page, enabled: boolean): Promise<void> {
  await openAppearance(page)
  await page
    .getByTestId('ran-mode-toggle')
    .getByRole('button', { name: enabled ? 'On' : 'Off', exact: true })
    .click()
  await expect(page.locator('[data-ran-mode="true"]')).toHaveCount(enabled ? 1 : 0)
  await page.getByRole('button', { name: 'Close settings' }).click()
}

async function expectAuxiliaryLayoutControlsAbsent(page: Page): Promise<void> {
  await expect(page.getByRole('button', { name: 'Layout editor' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Open settings' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Swap sidebar sides' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /^(Default|Ran Mode|Focus|Terminal deck|Quad)$/ })).toHaveCount(0)
  await expect(page.getByTestId('ran-mode-toggle')).toHaveCount(0)

  await page.keyboard.press('Control+K')
  const palette = page.getByRole('dialog')

  await expect(palette).toBeVisible()
  const search = palette.getByRole('combobox')

  for (const command of [
    'Swap sidebar sides',
    'Toggle Ran Mode',
    'Toggle layout edit mode',
    'Toggle status bar',
    'Toggle terminal',
    'Settings',
    'Keyboard shortcuts',
    'Change Theme',
    'Change Color Mode',
    'Reset layout'
  ]) {
    await search.fill(command)
    await expect(palette.getByRole('option', { name: new RegExp(command, 'i') })).toHaveCount(0)
  }

  await page.keyboard.press('Escape')
  await expect(palette).not.toBeVisible()
}

async function captureOwnedUiState(page: Page): Promise<Record<string, string | null>> {
  return page.evaluate(
    ({ activePresetKey, composerPopoutKey, layoutTreeKey, paneStatesKey }) => ({
      activePreset:
        document.querySelector('[data-layout-preset]')?.getAttribute('data-layout-preset') ??
        window.localStorage.getItem(activePresetKey),
      composerPopout: window.localStorage.getItem(composerPopoutKey),
      layoutTree: window.localStorage.getItem(layoutTreeKey),
      paneStates: window.localStorage.getItem(paneStatesKey)
    }),
    {
      activePresetKey: ACTIVE_PRESET_KEY,
      composerPopoutKey: COMPOSER_POPOUT_KEY,
      layoutTreeKey: LAYOUT_TREE_KEY,
      paneStatesKey: PANE_STATES_KEY
    }
  )
}

async function captureRanOwnedStorage(page: Page): Promise<Record<string, string | null>> {
  return page.evaluate(
    keys => Object.fromEntries(keys.map(key => [key, window.localStorage.getItem(key)])),
    RAN_OWNED_STORAGE_KEYS
  )
}

async function pressAuxiliaryMutationAndExpectPrimaryStorageUnchanged(
  auxiliary: Page,
  primary: Page,
  durableBefore: Record<string, string | null>,
  key: string
): Promise<void> {
  await auxiliary.keyboard.press(key)
  expect(await captureRanOwnedStorage(primary)).toEqual(durableBefore)
}

async function chooseQuadLayout(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Layout editor' }).click()
  await page.getByRole('button', { name: /Quad/ }).click()
  await page.waitForFunction(key => window.localStorage.getItem(key) === 'quad', ACTIVE_PRESET_KEY)
  expect(await page.evaluate(key => window.localStorage.getItem(key), ACTIVE_PRESET_KEY)).toBe('quad')
  await page.getByRole('button', { name: 'Done', exact: true }).click()
}

async function send(page: Page, text: string): Promise<void> {
  const composer = page.locator('[contenteditable="true"]').first()
  await composer.waitFor({ state: 'visible', timeout: 15_000 })
  await composer.click()
  await composer.type(text, { delay: 4 })
  await page.keyboard.press('Enter')
}

test.describe.serial('Ran Mode / Focus preset', () => {
  test.beforeAll(async () => {
    rendererErrors = []
    fixture = await setupMockBackend({ onWindow: watchRendererErrors })
    await waitForAppReady(fixture, 120_000)
  })

  test.afterEach(() => {
    expect(rendererErrors).toEqual([])
  })

  test.afterAll(async () => {
    if (!fixture) {
      return
    }

    try {
      await fixture.app.close()
    } catch {
      // The restart test may already have closed a prior Electron handle.
    }

    await fixture.mock.close()
    fixture.sandbox.cleanup()
    fixture = null
  })

  test('serializes the named Ran journal lock across full instance renderers', async () => {
    const page = fixture!.page
    let peer: Page | null = null

    try {
      const peerPromise = fixture!.app.waitForEvent('window')
      await expect(
        page.evaluate(() =>
          (
            window as unknown as Window & {
              hermesDesktop: { openWindow: () => Promise<{ ok: boolean }> }
            }
          ).hermesDesktop.openWindow()
        )
      ).resolves.toMatchObject({ ok: true })
      peer = await peerPromise
      await peer.waitForLoadState('domcontentloaded')

      await page.evaluate(lockName => {
        const state = window as Window & { __ranLockProbe?: { held: boolean; release?: () => void } }
        state.__ranLockProbe = { held: false }
        void navigator.locks.request(lockName, { mode: 'exclusive' }, async () => {
          state.__ranLockProbe!.held = true
          await new Promise<void>(resolve => {
            state.__ranLockProbe!.release = resolve
          })
          state.__ranLockProbe!.held = false
        })
      }, RAN_MODE_LOCK_KEY)

      await page.waitForFunction(() =>
        Boolean((window as Window & { __ranLockProbe?: { held: boolean } }).__ranLockProbe?.held)
      )

      await peer.evaluate(lockName => {
        const state = window as Window & { __ranPeerProbe?: { entered: boolean; resolved: boolean } }
        state.__ranPeerProbe = { entered: false, resolved: false }
        void navigator.locks
          .request(lockName, { mode: 'exclusive' }, () => {
            state.__ranPeerProbe!.entered = true
          })
          .then(() => {
            state.__ranPeerProbe!.resolved = true
          })
      }, RAN_MODE_LOCK_KEY)

      await peer.waitForTimeout(500)
      expect(
        await peer.evaluate(
          () =>
            (window as Window & { __ranPeerProbe?: { entered: boolean; resolved: boolean } }).__ranPeerProbe
        )
      ).toEqual({ entered: false, resolved: false })

      const peerQuery = await peer.evaluate(() => navigator.locks.query())
      expect((peerQuery.pending ?? []).some(lock => lock.name === RAN_MODE_LOCK_KEY)).toBe(true)

      await page.evaluate(() =>
        (window as Window & { __ranLockProbe?: { release?: () => void } }).__ranLockProbe?.release?.()
      )
      await peer.waitForFunction(
        () => {
          const probe = (window as Window & { __ranPeerProbe?: { entered: boolean; resolved: boolean } }).__ranPeerProbe

          return probe?.entered === true && probe.resolved === true
        },
        undefined
      )
      expect(
        await peer.evaluate(
          () => (window as Window & { __ranPeerProbe?: { entered: boolean; resolved: boolean } }).__ranPeerProbe
        )
      ).toEqual({ entered: true, resolved: true })
    } finally {
      await page
        .evaluate(() =>
          (window as Window & { __ranLockProbe?: { release?: () => void } }).__ranLockProbe?.release?.()
        )
        .catch(() => undefined)
      await peer?.close().catch(() => undefined)
    }
  })

  test('activates, presents a chat-first layout, and restores the prior state', async () => {
    const page = fixture!.page

    const originalComposerPopout = await page.evaluate(
      ({ composerPopoutKey, layoutTreeKey }) => {
        const tree = JSON.parse(window.localStorage.getItem(layoutTreeKey) ?? 'null') as null | {
          children?: unknown[]
          id?: string
          type?: string
        }

        const groupIds: string[] = []

        const visit = (node: unknown) => {
          if (!node || typeof node !== 'object') {
            return
          }

          const candidate = node as { children?: unknown[]; id?: string; type?: string }

          if (candidate.type === 'group' && typeof candidate.id === 'string') {
            groupIds.push(candidate.id)
          }

          candidate.children?.forEach(visit)
        }

        visit(tree)

        const removedByRan = groupIds.find(id => id !== 'ran-mode-sessions' && id !== 'ran-mode-workspace')

        if (!removedByRan) {
          throw new Error('E2E requires a pre-Ran layout group removed by the Ran layout')
        }

        const value = JSON.stringify({
          [removedByRan]: { poppedOut: true, position: { bottom: 43, right: 71 } }
        })

        window.localStorage.setItem(composerPopoutKey, value)

        return value
      },
      { composerPopoutKey: COMPOSER_POPOUT_KEY, layoutTreeKey: LAYOUT_TREE_KEY }
    )

    await page.reload()
    await expect(page.getByRole('button', { name: 'Open settings' })).toBeVisible({ timeout: 30_000 })

    const before = await captureOwnedUiState(page)

    expect(before.composerPopout).toBe(originalComposerPopout)

    await setRanMode(page, true)

    await expect(page.locator('[data-ran-mode="true"]')).toBeVisible()
    await expect(page.locator('[data-layout-preset="ran-mode"]')).toHaveCount(1)
    await expect(page.locator('[data-terminal-slot]:visible')).toHaveCount(0)
    await expect(page.locator('[data-review-pane]:visible')).toHaveCount(0)
    await expect(page.locator('[data-statusbar]:visible')).toHaveCount(0)
    await expect(page.locator('[contenteditable="true"]').first()).toBeVisible()

    for (const viewport of [
      { height: 768, width: 1366 },
      { height: 1080, width: 1920 }
    ]) {
      await page.setViewportSize(viewport)
      await expect(page.locator('[data-ran-mode="true"]')).toBeVisible()
      await expect(page.locator('[contenteditable="true"]').first()).toBeVisible()
    }

    const record = await page.evaluate(key => window.localStorage.getItem(key), RAN_MODE_KEY)
    expect(JSON.parse(record ?? '{}')).toMatchObject({ enabled: true, phase: 'active', version: 1 })
    expect(await page.evaluate(key => window.localStorage.getItem(key), COMPOSER_POPOUT_KEY)).toBe('{}')

    await setRanMode(page, false)
    expect(await captureOwnedUiState(page)).toEqual(before)
    expect(await page.evaluate(key => window.localStorage.getItem(key), RAN_MODE_KEY)).toBeNull()

    await page.evaluate(key => window.localStorage.removeItem(key), COMPOSER_POPOUT_KEY)
    await page.reload()
    await expect(page.getByRole('button', { name: 'Open settings' })).toBeVisible({ timeout: 30_000 })
  })

  test('restores an explicitly customized layout instead of defaults', async () => {
    const page = fixture!.page
    await chooseQuadLayout(page)
    const customized = await captureOwnedUiState(page)

    await setRanMode(page, true)
    await expect(page.locator('[data-ran-mode="true"]')).toBeVisible()
    await setRanMode(page, false)

    expect(await captureOwnedUiState(page)).toEqual(customized)
  })

  test('enters through the command palette and the layout picker', async () => {
    const page = fixture!.page

    await page.keyboard.press('Control+K')
    const palette = page.getByRole('dialog')
    await expect(palette).toBeVisible()
    const commandSearch = palette.getByRole('combobox')
    await commandSearch.fill('Toggle Ran Mode')
    const ranCommand = palette.getByRole('option', { name: /Toggle Ran Mode/ })
    await expect(ranCommand).toBeVisible()
    await ranCommand.dispatchEvent('pointermove')
    await expect(ranCommand).toHaveAttribute('aria-selected', 'true')
    await commandSearch.press('Enter')
    await expect(page.locator('[data-ran-mode="true"]')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(palette).not.toBeVisible()
    await setRanMode(page, false)

    await page.getByRole('button', { name: 'Layout editor' }).click()
    await page.getByRole('button', { name: /Ran Mode/ }).click()
    await expect(page.locator('[data-ran-mode="true"]')).toBeVisible()
    await expect(page.locator('[data-layout-preset="ran-mode"]')).toHaveCount(1)
    await setRanMode(page, false)
  })

  test('omits Ran Mode and layout mutation controls from HUD and secondary renderers', async () => {
    const page = fixture!.page
    await page.evaluate(() => {
      window.localStorage.setItem(
        'hermes.desktop.composerPopout.zones.v1',
        JSON.stringify({
          'primary-custom-zone': { poppedOut: true, position: { bottom: 37, right: 53 } }
        })
      )
      window.localStorage.setItem('hermes.desktop.panesFlipped', 'true')
      window.localStorage.setItem('hermes.desktop.reviewOpen', 'true')
      window.localStorage.setItem('hermes.desktop.statusbarVisible', 'true')
      window.localStorage.setItem('hermes.desktop.terminalTakeover', 'true')
      window.localStorage.setItem('hermes.desktop.toolView.technical', 'true')
    })
    const durableBefore = await captureRanOwnedStorage(page)
    let hud: Page | null = null
    let secondary: Page | null = null

    try {
      const hudPromise = fixture!.app.waitForEvent('window')
      await expect(
        page.evaluate(() =>
          (
            window as unknown as Window & {
              hermesDesktop: { hud?: { open: () => Promise<{ ok: boolean }> } }
            }
          ).hermesDesktop.hud?.open()
        )
      ).resolves.toMatchObject({ ok: true })
      hud = await hudPromise
      await hud.waitForLoadState('domcontentloaded')
      await expect(hud.locator('[contenteditable="true"]').first()).toBeVisible({ timeout: 30_000 })
      await expectAuxiliaryLayoutControlsAbsent(hud)

      for (const key of [
        'Control+Backslash',
        'Control+G',
        'Control+Backquote',
        'Control+Shift+Backquote',
        'Control+Comma',
        'Control+Slash',
        'Control+Shift+S'
      ]) {
        await pressAuxiliaryMutationAndExpectPrimaryStorageUnchanged(hud, page, durableBefore, key)
      }

      await expect(hud.getByRole('button', { name: 'Appearance', exact: true })).toHaveCount(0)
      await expect(hud.getByText('Tool Call Display', { exact: true })).toHaveCount(0)

      const secondaryPromise = fixture!.app.waitForEvent('window')
      await expect(
        page.evaluate(() =>
          (
            window as unknown as Window & {
              hermesDesktop: { openSessionWindow: (sessionId: string) => Promise<{ ok: boolean }> }
            }
          ).hermesDesktop.openSessionWindow('ran-mode-auxiliary-e2e')
        )
      ).resolves.toMatchObject({ ok: true })
      secondary = await secondaryPromise
      await secondary.waitForLoadState('domcontentloaded')
      await expect(secondary.locator('[contenteditable="true"]').first()).toBeVisible({ timeout: 30_000 })
      await expectAuxiliaryLayoutControlsAbsent(secondary)

      for (const key of [
        'Control+Backslash',
        'Control+G',
        'Control+Backquote',
        'Control+Shift+Backquote',
        'Control+Comma',
        'Control+Slash',
        'Control+Shift+S'
      ]) {
        await pressAuxiliaryMutationAndExpectPrimaryStorageUnchanged(secondary, page, durableBefore, key)
      }

      await expect(secondary.getByRole('button', { name: 'Appearance', exact: true })).toHaveCount(0)
      await expect(secondary.getByText('Tool Call Display', { exact: true })).toHaveCount(0)

      expect(await captureRanOwnedStorage(page)).toEqual(durableBefore)
    } finally {
      await secondary?.close().catch(() => undefined)
      await hud?.close().catch(() => undefined)
      await page
        .evaluate(() =>
          (
            window as unknown as Window & {
              hermesDesktop: { hud?: { close: () => Promise<{ ok: boolean }> } }
            }
          ).hermesDesktop.hud?.close()
        )
        .catch(() => undefined)
      await page.evaluate(key => window.localStorage.removeItem(key), COMPOSER_POPOUT_KEY)
    }
  })

  test('remains coherent across a Desktop restart and then restores the original state', async () => {
    const before = await captureOwnedUiState(fixture!.page)
    await setRanMode(fixture!.page, true)

    await fixture!.app.close()
    const restartEnv = buildAppEnv(fixture!.sandbox)

    await expect(
      launchDesktop({ ...restartEnv, HERMES_E2E_INJECT_STARTUP_RENDERER_ERROR: '1' })
    ).rejects.toThrow(/HERMES_E2E_STARTUP_RENDERER_ERROR_SENTINEL/)

    const relaunched = await launchDesktop(restartEnv, watchRendererErrors)
    fixture!.app = relaunched.app
    fixture!.page = relaunched.page
    await waitForAppReady(fixture!, 120_000)

    const page = fixture!.page
    await expect(page.locator('[data-ran-mode="true"]')).toBeVisible()
    expect(await page.evaluate(key => window.localStorage.getItem(key), RAN_MODE_KEY)).not.toBeNull()

    await setRanMode(page, false)
    expect(await captureOwnedUiState(page)).toEqual(before)
  })

  test('treats Reset Layout as an explicit exit instead of a hidden rollback', async () => {
    const page = fixture!.page
    await chooseQuadLayout(page)
    await setRanMode(page, true)

    await page.getByRole('button', { name: 'Layout editor' }).click({ modifiers: ['Control'] })

    await expect(page.locator('[data-ran-mode="false"]')).toBeVisible()
    await expect(page.locator('[data-layout-preset="default"]')).toBeVisible()
    expect(await page.evaluate(key => window.localStorage.getItem(key), RAN_MODE_KEY)).toBeNull()

    const done = page.getByRole('button', { name: 'Done', exact: true })

    if (await done.isVisible()) {
      await done.click()
    }
  })

  test('keeps live and approval activity visible, collapses settled tool details, and works in a narrow window', async () => {
    const page = fixture!.page
    await page.setViewportSize({ width: 900, height: 700 })
    await setRanMode(page, true)

    await send(page, 'E2E_CORRECTION_SWITCH_TRIGGER')
    const liveTool = page.locator('[data-tool-row]').last()
    await expect(liveTool).toBeVisible({ timeout: 30_000 })
    await expect(liveTool).toContainText(/Running|terminal/i)
    await expect(
      page.getByRole('paragraph').filter({ hasText: /^The corrected task finished\.$/ })
    ).toBeVisible({ timeout: 45_000 })
    await expect(liveTool).not.toHaveAttribute('data-tool-open', '')

    await liveTool.getByRole('button').first().click()
    await expect(liveTool).toHaveAttribute('data-tool-open', '')

    await send(page, 'E2E_QUEUE_STOP_TRIGGER')
    await expect(page.getByText('Keep working?', { exact: true })).toBeVisible({ timeout: 30_000 })
    await expect(page.getByRole('button', { name: /Yes|No/ }).first()).toBeVisible()

    await expect(page.locator('[contenteditable="true"]').first()).toBeVisible()

    await setRanMode(page, false)
  })
})
