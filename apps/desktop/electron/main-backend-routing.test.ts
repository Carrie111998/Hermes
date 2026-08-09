import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterAll, beforeAll, test, vi } from 'vitest'

const electronMock = vi.hoisted(() => {
  const handlers = new Map<string, (...args: any[]) => any>()
  const listeners = new Map<string, (...args: any[]) => any>()
  let userDataPath = ''

  class FakeBrowserWindow {
    static fromWebContents = vi.fn(() => null)
    static getAllWindows = vi.fn(() => [])
    static getFocusedWindow = vi.fn(() => null)

    readonly webContents = {
      on: vi.fn(),
      send: vi.fn(),
      session: {
        addWordToSpellCheckerDictionary: vi.fn()
      }
    }

    isDestroyed() {
      return false
    }

    on() {
      return this
    }

    once() {
      return this
    }
  }

  const session = {
    defaultSession: {
      availableSpellCheckerLanguages: ['en-US'],
      setSpellCheckerLanguages: vi.fn(),
      setPermissionCheckHandler: vi.fn(),
      setPermissionRequestHandler: vi.fn()
    },
    fromPartition: vi.fn(() => ({
      cookies: {
        get: vi.fn(async () => [])
      },
      protocol: {
        handle: vi.fn()
      },
      setPermissionCheckHandler: vi.fn(),
      setPermissionRequestHandler: vi.fn(),
      webRequest: {
        onBeforeSendHeaders: vi.fn()
      }
    }))
  }

  return {
    BrowserWindow: FakeBrowserWindow,
    handlers,
    listeners,
    setUserDataPath(pathname: string) {
      userDataPath = pathname
    },
    module: {
      app: {
        commandLine: {
          appendSwitch: vi.fn()
        },
        disableHardwareAcceleration: vi.fn(),
        exit: vi.fn(),
        getAppPath: vi.fn(() => path.resolve(__dirname, '..')),
        getLocale: vi.fn(() => 'en-US'),
        getPath: vi.fn((name: string) => (name === 'userData' ? userDataPath : path.join(userDataPath, name))),
        getVersion: vi.fn(() => '0.0.0-test'),
        isPackaged: false,
        isReady: vi.fn(() => false),
        on: vi.fn((event: string, listener: (...args: any[]) => any) => {
          listeners.set(event, listener)
        }),
        quit: vi.fn(),
        relaunch: vi.fn(),
        requestSingleInstanceLock: vi.fn(() => true),
        setAboutPanelOptions: vi.fn(),
        setAsDefaultProtocolClient: vi.fn(),
        setName: vi.fn(),
        whenReady: vi.fn(() => new Promise(() => {}))
      },
      BrowserWindow: FakeBrowserWindow,
      clipboard: {
        readImage: vi.fn(),
        readText: vi.fn(() => ''),
        writeImage: vi.fn(),
        writeText: vi.fn()
      },
      dialog: {
        showErrorBox: vi.fn(),
        showMessageBox: vi.fn(async () => ({ response: 0 })),
        showOpenDialog: vi.fn(async () => ({ canceled: true, filePaths: [] })),
        showSaveDialog: vi.fn(async () => ({ canceled: true, filePath: undefined }))
      },
      globalShortcut: {
        isRegistered: vi.fn(() => false),
        register: vi.fn(() => true),
        unregister: vi.fn(),
        unregisterAll: vi.fn()
      },
      ipcMain: {
        handle: vi.fn((channel: string, handler: (...args: any[]) => any) => {
          handlers.set(channel, handler)
        }),
        on: vi.fn((channel: string, handler: (...args: any[]) => any) => {
          listeners.set(channel, handler)
        })
      },
      Menu: {
        buildFromTemplate: vi.fn(() => ({ popup: vi.fn() })),
        setApplicationMenu: vi.fn()
      },
      nativeImage: {
        createFromPath: vi.fn(() => ({ isEmpty: () => true })),
        createFromDataURL: vi.fn(() => ({ isEmpty: () => true })),
        createFromBuffer: vi.fn(() => ({ isEmpty: () => true }))
      },
      nativeTheme: {
        on: vi.fn(),
        shouldUseDarkColors: false,
        themeSource: 'system'
      },
      net: {
        fetch: vi.fn(),
        request: vi.fn()
      },
      Notification: Object.assign(
        vi.fn(function Notification(this: any) {
          this.show = vi.fn()
        }),
        { isSupported: vi.fn(() => false) }
      ),
      powerMonitor: {
        isOnBatteryPower: vi.fn(() => false),
        on: vi.fn()
      },
      powerSaveBlocker: {
        isStarted: vi.fn(() => false),
        start: vi.fn(() => 1),
        stop: vi.fn()
      },
      protocol: {
        handle: vi.fn(),
        registerSchemesAsPrivileged: vi.fn()
      },
      safeStorage: {
        decryptString: vi.fn((buffer: Buffer) => buffer.toString('utf8')),
        encryptString: vi.fn((value: string) => Buffer.from(value, 'utf8')),
        isEncryptionAvailable: vi.fn(() => true)
      },
      screen: {
        getAllDisplays: vi.fn(() => []),
        getPrimaryDisplay: vi.fn(() => ({ bounds: { height: 768, width: 1024, x: 0, y: 0 } })),
        on: vi.fn()
      },
      session,
      shell: {
        openExternal: vi.fn(async () => undefined),
        openPath: vi.fn(async () => ''),
        showItemInFolder: vi.fn(),
        trashItem: vi.fn(async () => undefined)
      },
      systemPreferences: {
        askForMediaAccess: vi.fn(async () => false),
        getMediaAccessStatus: vi.fn(() => 'denied')
      }
    }
  }
})

vi.mock('electron', () => electronMock.module)

vi.mock('node-pty', () => ({
  default: {
    spawn: vi.fn()
  }
}))

let tempDir = ''

beforeAll(() => {
  tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-main-routing-'))
  electronMock.setUserDataPath(tempDir)
})

afterAll(() => {
  if (tempDir) {
    fs.rmSync(tempDir, { force: true, recursive: true })
  }
})

async function importMainForTest() {
  return (await import('./main')) as any
}

test('hermes:backend:touch refreshes pooled explicit root rails through the main IPC handler', async () => {
  const main = await importMainForTest()
  const hooks = main.__backendRoutingTestHooks

  assert.ok(hooks, 'main.ts must expose its backend routing test hooks')

  hooks.backendPool.clear()
  hooks.backendPool.set('default', { lastActiveAt: 1 })
  hooks.backendPool.set('local:default', { lastActiveAt: 2 })
  hooks.backendPool.set('remote:default', { lastActiveAt: 3 })
  hooks.backendPool.set('writer', { lastActiveAt: 4 })

  const handler = electronMock.handlers.get('hermes:backend:touch')

  assert.equal(typeof handler, 'function')
  assert.deepEqual(await handler?.({}, 'default'), { ok: true })

  const touchedAt = hooks.backendPool.get('default')?.lastActiveAt

  assert.ok(touchedAt > 4)
  assert.equal(hooks.backendPool.get('local:default')?.lastActiveAt, touchedAt)
  assert.equal(hooks.backendPool.get('remote:default')?.lastActiveAt, touchedAt)
  assert.equal(hooks.backendPool.get('writer')?.lastActiveAt, 4)
})

test('hermes:backend:touch keeps explicit root rail touches isolated through the main IPC handler', async () => {
  const main = await importMainForTest()
  const hooks = main.__backendRoutingTestHooks

  assert.ok(hooks, 'main.ts must expose its backend routing test hooks')

  hooks.backendPool.clear()
  hooks.backendPool.set('default', { lastActiveAt: 1 })
  hooks.backendPool.set('local:default', { lastActiveAt: 2 })
  hooks.backendPool.set('remote:default', { lastActiveAt: 3 })
  hooks.backendPool.set('writer', { lastActiveAt: 4 })

  const handler = electronMock.handlers.get('hermes:backend:touch')

  assert.equal(typeof handler, 'function')
  assert.deepEqual(await handler?.({}, 'default', { remoteOnly: true }), { ok: true })

  const touchedAt = hooks.backendPool.get('remote:default')?.lastActiveAt

  assert.ok(touchedAt > 4)
  assert.equal(hooks.backendPool.get('default')?.lastActiveAt, 1)
  assert.equal(hooks.backendPool.get('local:default')?.lastActiveAt, 2)
  assert.equal(hooks.backendPool.get('remote:default')?.lastActiveAt, touchedAt)
  assert.equal(hooks.backendPool.get('writer')?.lastActiveAt, 4)
})

test('main resolveRemoteBackend remoteOnly uses the saved global remote while the primary mode is local', async () => {
  const main = await importMainForTest()
  const hooks = main.__backendRoutingTestHooks

  assert.ok(hooks, 'main.ts must expose its backend routing test hooks')

  fs.writeFileSync(
    path.join(tempDir, 'connection.json'),
    JSON.stringify({
      mode: 'local',
      remote: {
        authMode: 'token',
        mode: 'remote',
        token: { encoding: 'plain', value: 'redacted-token' },
        url: 'https://remote.example.test/hermes/'
      },
      profiles: {}
    })
  )

  assert.deepEqual(await hooks.resolveRemoteBackend('default', { remoteOnly: true }), {
    authMode: 'token',
    baseUrl: 'https://remote.example.test/hermes',
    mode: 'remote',
    remoteHost: 'remote.example.test',
    remoteIdentity: undefined,
    remoteKind: 'url',
    source: 'settings',
    token: 'redacted-token',
    wsUrl: 'wss://remote.example.test/hermes/api/ws?token=redacted-token'
  })
})
