/**
 * Tests for electron/tray.ts — the close-to-tray prefs + tray lifecycle.
 *
 * Run with: npx vitest run --project electron electron/tray.test.ts
 */

import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { destroyTray, isTrayEnabled, loadTrayPrefs, setTrayEnabled } from './tray'

// --- electron mock ---------------------------------------------------------

const electronMock = vi.hoisted(() => {
  let userDataPath = ''

  class FakeTray {
    destroyed = false
    toolTip: string | null = null
    contextMenu: unknown = null
    clickHandlers: Array<() => void> = []

    constructor() {
      instances.push(this)
    }

    setToolTip(text: string) {
      this.toolTip = text
    }

    setContextMenu(menu: unknown) {
      this.contextMenu = menu
    }

    on(event: string, handler: () => void) {
      if (event === 'click') {
        this.clickHandlers.push(handler)
      }

      return this
    }

    destroy() {
      this.destroyed = true
    }
  }

  const instances: FakeTray[] = []

  return {
    app: {
      isReady: () => true,
      getAppPath: () => '/fake/app-path',
      getPath: (name: string) => {
        if (name === 'userData') {
          return userDataPath
        }

        throw new Error(`unexpected getPath(${name})`)
      },
      __setUserDataPath(p: string) {
        userDataPath = p
      }
    },
    Menu: {
      buildFromTemplate: (template: unknown) => template
    },
    nativeImage: {
      createFromPath: () => ({ isEmpty: () => false })
    },
    Tray: FakeTray,
    __instances: instances
  }
})

vi.mock('electron', () => ({
  app: electronMock.app,
  Menu: electronMock.Menu,
  nativeImage: electronMock.nativeImage,
  Tray: electronMock.Tray
}))

// --- tests -----------------------------------------------------------------

describe('tray prefs', () => {
  let dir: string

  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tray-test-'))
    electronMock.app.__setUserDataPath(dir)
    // Module-level `tray` state survives across tests in one file; reset it
    // so each test starts with no tray instance.
    destroyTray()
    electronMock.__instances.length = 0
  })

  afterEach(() => {
    fs.rmSync(dir, { recursive: true, force: true })
  })

  it('defaults to enabled when no prefs file exists', () => {
    expect(loadTrayPrefs()).toBe(true)
  })

  it('persists and reloads the choice', () => {
    setTrayEnabled(false, { onShow: () => {}, onQuit: () => {} })
    expect(loadTrayPrefs()).toBe(false)
    setTrayEnabled(true, { onShow: () => {}, onQuit: () => {} })
    expect(loadTrayPrefs()).toBe(true)
    expect(isTrayEnabled()).toBe(true)
  })

  it('reads an existing prefs file with closeToTray: false', () => {
    fs.writeFileSync(path.join(dir, 'tray-prefs.json'), JSON.stringify({ closeToTray: false }))
    expect(loadTrayPrefs()).toBe(false)
  })

  it('creates a tray with tooltip and context menu when enabled', () => {
    setTrayEnabled(true, { onShow: () => {}, onQuit: () => {} })
    expect(electronMock.__instances.length).toBe(1)
    const tray = electronMock.__instances[0]
    expect(tray.destroyed).toBe(false)
    expect(tray.toolTip).toBe('Hermes')
    expect(tray.contextMenu).toBeTruthy()
    expect(tray.clickHandlers.length).toBe(1)
  })

  it('destroys the tray on destroyTray', () => {
    setTrayEnabled(true, { onShow: () => {}, onQuit: () => {} })
    destroyTray()
    expect(electronMock.__instances[0].destroyed).toBe(true)
  })
})
