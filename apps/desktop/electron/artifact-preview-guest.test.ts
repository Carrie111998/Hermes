import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  ARTIFACT_PREVIEW_PARTITION,
  hardenArtifactPreviewWebPreferences,
  installArtifactPreviewGuestIsolation
} from './artifact-preview-guest'

function makeHost() {
  const handlers = new Map<string, (...args: any[]) => void>()

  return {
    handlers,
    host: {
      on(event: string, handler: (...args: any[]) => void) {
        handlers.set(event, handler)
      }
    }
  }
}

test('hardenArtifactPreviewWebPreferences removes preload access and pins isolation', () => {
  const preferences: Record<string, unknown> = {
    nodeIntegration: true,
    preload: '/tmp/unsafe.cjs',
    preloadURL: 'file:///tmp/unsafe.cjs',
    sandbox: false
  }

  hardenArtifactPreviewWebPreferences(preferences)

  assert.equal(preferences.preload, undefined)
  assert.equal(preferences.preloadURL, undefined)
  assert.equal(preferences.contextIsolation, true)
  assert.equal(preferences.nodeIntegration, false)
  assert.equal(preferences.sandbox, true)
  assert.equal(preferences.webSecurity, true)
})

test('installArtifactPreviewGuestIsolation rejects non-data artifact guests', () => {
  const { handlers, host } = makeHost()
  installArtifactPreviewGuestIsolation(host as never)
  let prevented = false

  handlers.get('will-attach-webview')?.(
    { preventDefault: () => (prevented = true) },
    {},
    { partition: ARTIFACT_PREVIEW_PARTITION, src: 'https://example.com' }
  )

  assert.equal(prevented, true)
})

test('installArtifactPreviewGuestIsolation hardens and locks an attached artifact guest', () => {
  const { handlers, host } = makeHost()
  installArtifactPreviewGuestIsolation(host as never)
  const preferences: Record<string, unknown> = { nodeIntegration: true }
  let attachPrevented = false

  handlers.get('will-attach-webview')?.(
    { preventDefault: () => (attachPrevented = true) },
    preferences,
    {
      partition: ARTIFACT_PREVIEW_PARTITION,
      src: 'data:text/html;charset=utf-8,%3Ch1%3Epreview%3C%2Fh1%3E'
    }
  )

  assert.equal(attachPrevented, false)
  assert.equal(preferences.nodeIntegration, false)
  assert.equal(preferences.sandbox, true)

  let navigationHandler: ((event: { preventDefault(): void }, url: string) => void) | null = null
  let openHandler: (() => { action: 'deny' }) | null = null
  let permissionCheck: (() => boolean) | null = null

  let permissionRequest: ((webContents: unknown, permission: unknown, callback: (allowed: boolean) => void) => void) | null =
    null

  const guest = {
    getURL: () => 'data:text/html;charset=utf-8,preview',
    on: (_event: string, handler: typeof navigationHandler) => {
      navigationHandler = handler
    },
    session: {
      getPartition: () => ARTIFACT_PREVIEW_PARTITION,
      setPermissionCheckHandler: (handler: () => boolean) => {
        permissionCheck = handler
      },
      setPermissionRequestHandler: (handler: typeof permissionRequest) => {
        permissionRequest = handler
      }
    },
    setWindowOpenHandler: (handler: typeof openHandler) => {
      openHandler = handler
    }
  }

  handlers.get('did-attach-webview')?.({}, guest)

  assert.deepEqual(openHandler?.(), { action: 'deny' })
  assert.equal(permissionCheck?.(), false)

  let permissionAllowed: boolean | null = null
  permissionRequest?.(null, 'camera', allowed => {
    permissionAllowed = allowed
  })
  assert.equal(permissionAllowed, false)

  let navigationPrevented = false
  navigationHandler?.({ preventDefault: () => (navigationPrevented = true) }, 'https://example.com')
  assert.equal(navigationPrevented, true)
})
