import { describe, expect, it, vi } from 'vitest'

import { startDesktopFirstWindow } from './desktop-startup'

describe('startDesktopFirstWindow', () => {
  it('starts the backend first and defers noncritical integrations until after window creation', () => {
    const order: string[] = []
    const deferred: Array<() => void> = []

    startDesktopFirstWindow({
      createWindow: () => order.push('window'),
      defer: task => deferred.push(task),
      initializeAfterWindow: () => order.push('deferred'),
      initializeRendererDependencies: () => order.push('renderer-dependencies'),
      onBackendError: vi.fn(),
      startBackend: async () => {
        order.push('backend')
      }
    })

    expect(order).toEqual(['backend', 'renderer-dependencies', 'window'])
    expect(deferred).toHaveLength(1)

    deferred[0]()

    expect(order).toEqual(['backend', 'renderer-dependencies', 'window', 'deferred'])
  })

  it('reports an asynchronous backend failure without blocking the first window', async () => {
    const error = new Error('backend failed')
    const onBackendError = vi.fn()
    const createWindow = vi.fn()

    startDesktopFirstWindow({
      createWindow,
      defer: vi.fn(),
      initializeAfterWindow: vi.fn(),
      initializeRendererDependencies: vi.fn(),
      onBackendError,
      startBackend: () => Promise.reject(error)
    })

    expect(createWindow).toHaveBeenCalledOnce()
    await Promise.resolve()
    expect(onBackendError).toHaveBeenCalledWith(error)
  })
})
