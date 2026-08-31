import { describe, expect, it, vi } from 'vitest'

import { createPreviewUblockController } from './preview-ublock'

function fakeSession() {
  const extensions = new Map<string, any>()
  let nextId = 0

  return {
    extensions: {
      getAllExtensions: () => [...extensions.values()],
      getExtension: (id: string) => extensions.get(id) ?? null,
      loadExtension: async (extensionPath: string) => {
        const extension = {
          id: `ublock-${++nextId}`,
          manifest: { name: 'uBlock Origin Lite', version: '2026.825.1619' },
          path: extensionPath,
          url: `chrome-extension://ublock-${nextId}`
        }

        extensions.set(extension.id, extension)

        return extension
      },
      removeExtension: (id: string) => extensions.delete(id)
    }
  }
}

const cached = { path: '/user-data/preview-ublock/current', version: '2026.825.1619' }

function installer(resolve = vi.fn().mockResolvedValue(cached)) {
  return { resolve }
}

describe('preview uBlock lifecycle', () => {
  it('does not resolve or load an extension on default startup', async () => {
    const resolve = vi.fn()
    const previewSession = fakeSession()
    const controller = createPreviewUblockController({ installer: installer(resolve), session: previewSession })

    await expect(controller.initialize()).resolves.toMatchObject({ enabled: false, available: false })
    expect(resolve).not.toHaveBeenCalled()
    expect(previewSession.extensions.getAllExtensions()).toEqual([])
  })

  it('loads a cached extension into the supplied Preview session', async () => {
    const previewSession = fakeSession()
    const controller = createPreviewUblockController({ enabled: true, installer: installer(), session: previewSession })

    await expect(controller.initialize()).resolves.toEqual({
      enabled: true,
      available: true,
      dashboardUrl: 'chrome-extension://ublock-1/dashboard.html',
      extensionId: 'ublock-1',
      rulesetsReady: true,
      version: '2026.825.1619'
    })
  })

  it('uses latest on explicit enable and unloads without deleting the cache', async () => {
    const previewSession = fakeSession()
    const resolve = vi.fn().mockResolvedValue(cached)
    const controller = createPreviewUblockController({ installer: installer(resolve), session: previewSession })

    await expect(controller.setEnabled(true)).resolves.toMatchObject({ enabled: true, available: true })
    await expect(controller.setEnabled(false)).resolves.toMatchObject({ enabled: false, available: false })
    expect(resolve).toHaveBeenCalledWith('latest')
    expect(previewSession.extensions.getAllExtensions()).toEqual([])
  })

  it('falls back to a valid cached release when latest resolution fails', async () => {
    const previewSession = fakeSession()
    const resolve = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(cached)
    const onStaleCache = vi.fn()

    const controller = createPreviewUblockController({
      installer: installer(resolve),
      onStaleCache,
      session: previewSession
    })

    await expect(controller.setEnabled(true)).resolves.toMatchObject({ enabled: true, available: true })
    expect(resolve).toHaveBeenNthCalledWith(1, 'latest')
    expect(resolve).toHaveBeenNthCalledWith(2, 'cached')
    expect(onStaleCache).toHaveBeenCalledOnce()
  })

  it('rejects first enable failures and leaves the state disabled', async () => {
    const previewSession = fakeSession()
    const resolve = vi.fn().mockRejectedValue(new Error('offline'))
    const controller = createPreviewUblockController({ installer: installer(resolve), session: previewSession })

    await expect(controller.setEnabled(true)).rejects.toThrow('offline')
    expect(controller.getState()).toMatchObject({ enabled: false, available: false })
    expect(previewSession.extensions.getAllExtensions()).toEqual([])
  })

  it('serializes concurrent toggles', async () => {
    const previewSession = fakeSession()
    let releaseLatest!: () => void

    const resolve = vi.fn().mockImplementation((intent: string) =>
      intent === 'latest'
        ? new Promise(resolvePromise => {
            releaseLatest = () => resolvePromise(cached)
          })
        : Promise.resolve(cached)
    )

    const controller = createPreviewUblockController({ installer: installer(resolve), session: previewSession })

    const enabling = controller.setEnabled(true)
    const disabling = controller.setEnabled(false)
    await Promise.resolve()
    releaseLatest()
    await expect(enabling).resolves.toMatchObject({ enabled: true })
    await expect(disabling).resolves.toMatchObject({ enabled: false })
    expect(previewSession.extensions.getAllExtensions()).toEqual([])
  })
})
