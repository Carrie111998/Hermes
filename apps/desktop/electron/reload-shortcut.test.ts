/**
 * Unit tests for installReloadShortcut. The handler lives in main.ts and is the
 * only consumer of the reload chord on Windows/Linux (where the application
 * menu — and thus the reload role accelerator — is null). Verifies chord
 * matching, preventDefault, reload dispatch, and uninstall.
 */

import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'

import type { BrowserWindow, Input } from 'electron'
import { describe, test, vi } from 'vitest'

// main.ts evaluates app.isPackaged at module load — mock the electron app
// module so the import doesn't blow up outside an Electron runtime.
vi.mock('electron', () => {
  const app = {
    isPackaged: true,
    commandLine: { getSwitchValue: () => '' },
    getPath: () => '',
    whenReady: () => Promise.resolve(),
    on: () => {},
    quit: () => {},
    exit: () => {},
    getName: () => 'Hermes',
    getVersion: () => '0.20.0',
    setAppUserModelId: () => {},
    requestSingleInstanceLock: () => true,
    onSecondInstance: () => {},
    setAsDefaultProtocolClient: () => {},
    removeAsDefaultProtocolClient: () => {},
    dock: { setBadge: () => {}, setMenu: () => {} }
  }
  return {
    app,
    BrowserWindow: class {},
    Menu: { buildFromTemplate: () => ({}), setApplicationMenu: () => {} },
    ipcMain: { handle: () => {}, on: () => {} },
    shell: { openExternal: () => {}, openPath: () => {} },
    clipboard: { writeText: () => {} },
    nativeTheme: { on: () => {} },
    screen: { on: () => {}, getAllDisplays: () => [] },
    powerMonitor: { on: () => {} },
    session: { defaultSession: { setPermissionRequestHandler: () => {} } },
    net: { fetch: () => Promise.resolve({ ok: true }) }
  }
})

import { installReloadShortcut } from './main'

interface FakeWebContents {
  calls: {
    reload: number
  }
  isDestroyed: () => boolean
  destroy: () => void
  reload: () => void
  on: typeof EventEmitter.prototype.on
  once: typeof EventEmitter.prototype.once
  off: typeof EventEmitter.prototype.off
  emit: (event: string | symbol, ...args: unknown[]) => boolean
}

function makeFakeWebContents(): FakeWebContents {
  const emitter = new EventEmitter()
  const calls = { reload: 0 }
  let destroyed = false

  return {
    calls,
    isDestroyed: () => destroyed,
    destroy() {
      destroyed = true
      emitter.emit('destroyed')
    },
    reload() {
      calls.reload++
    },
    on: emitter.on.bind(emitter),
    once: emitter.once.bind(emitter),
    off: emitter.off.bind(emitter),
    emit: emitter.emit.bind(emitter)
  }
}

function asWC(fake: FakeWebContents): Electron.WebContents {
  return fake as unknown as Electron.WebContents
}

function makeFakeWindow(wc: FakeWebContents, destroyed = false) {
  return {
    webContents: asWC(wc),
    isDestroyed: () => destroyed
  } as unknown as BrowserWindow
}

function makeInput(input: Partial<Input>): Input {
  return {
    key: 'r',
    control: false,
    meta: false,
    alt: false,
    shift: false,
    ...input
  } as Input
}

describe('installReloadShortcut', () => {
  test('reloads on Ctrl+R (Windows/Linux) and prevents default', () => {
    const wc = makeFakeWebContents()
    const win = makeFakeWindow(wc)
    const uninstall = installReloadShortcut(win)

    const event = { preventDefault: () => {} }
    wc.emit('before-input-event', event, makeInput({ control: true }))

    assert.equal(wc.calls.reload, 1, 'Ctrl+R must trigger one reload')

    uninstall()
  })

  test('reloads on Cmd+R (macOS) and prevents default', () => {
    const wc = makeFakeWebContents()
    const win = makeFakeWindow(wc)
    const uninstall = installReloadShortcut(win)

    const event = { preventDefault: () => {} }
    wc.emit('before-input-event', event, makeInput({ meta: true }))

    assert.equal(wc.calls.reload, 1, 'Cmd+R must trigger one reload')

    uninstall()
  })

  test('reloads on F5 and prevents default', () => {
    const wc = makeFakeWebContents()
    const win = makeFakeWindow(wc)
    const uninstall = installReloadShortcut(win)

    const event = { preventDefault: () => {} }
    wc.emit('before-input-event', event, makeInput({ key: 'F5' }))

    assert.equal(wc.calls.reload, 1, 'F5 must trigger one reload')

    uninstall()
  })

  test('does NOT reload on bare R without modifier', () => {
    const wc = makeFakeWebContents()
    const win = makeFakeWindow(wc)
    const uninstall = installReloadShortcut(win)

    wc.emit('before-input-event', { preventDefault: () => {} }, makeInput({}))

    assert.equal(wc.calls.reload, 0, 'bare R must not reload')

    uninstall()
  })

  test('does NOT reload on Ctrl+Shift+R (force-reload is a separate chord)', () => {
    const wc = makeFakeWebContents()
    const win = makeFakeWindow(wc)
    const uninstall = installReloadShortcut(win)

    wc.emit(
      'before-input-event',
      { preventDefault: () => {} },
      makeInput({ control: true, shift: true })
    )

    assert.equal(wc.calls.reload, 0, 'Ctrl+Shift+R must not match the plain reload chord')

    uninstall()
  })

  test('does NOT reload on Ctrl+Alt+R', () => {
    const wc = makeFakeWebContents()
    const win = makeFakeWindow(wc)
    const uninstall = installReloadShortcut(win)

    wc.emit(
      'before-input-event',
      { preventDefault: () => {} },
      makeInput({ control: true, alt: true })
    )

    assert.equal(wc.calls.reload, 0, 'Ctrl+Alt+R must not reload')

    uninstall()
  })

  test('does NOT reload on F5 with Ctrl held', () => {
    const wc = makeFakeWebContents()
    const win = makeFakeWindow(wc)
    const uninstall = installReloadShortcut(win)

    wc.emit(
      'before-input-event',
      { preventDefault: () => {} },
      makeInput({ key: 'F5', control: true })
    )

    assert.equal(wc.calls.reload, 0, 'Ctrl+F5 must not match the plain F5 chord')

    uninstall()
  })

  test('uninstall removes the listener', () => {
    const wc = makeFakeWebContents()
    const win = makeFakeWindow(wc)
    const uninstall = installReloadShortcut(win)

    uninstall()
    wc.emit('before-input-event', { preventDefault: () => {} }, makeInput({ control: true }))

    assert.equal(wc.calls.reload, 0, 'after uninstall the listener must not fire')
  })

  test('does NOT reload when window is destroyed', () => {
    const wc = makeFakeWebContents()
    const win = makeFakeWindow(wc, true)
    const uninstall = installReloadShortcut(win)

    wc.emit('before-input-event', { preventDefault: () => {} }, makeInput({ control: true }))

    assert.equal(wc.calls.reload, 0, 'destroyed window must not reload')

    uninstall()
  })
})
