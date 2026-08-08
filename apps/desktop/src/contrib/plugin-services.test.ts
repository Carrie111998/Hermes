import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createPluginContext } from './plugin'
import { $pluginRecords, publishPlugin } from './plugins-store'

const readModel = {
  rest: [{ id: 'read-model', methods: ['GET'], paths: ['/boards', '/board'] }]
} as const

const requiresReadModel = {
  rest: [{ provider: 'provider', capability: 'read-model' }]
} as const

function provider(status: 'disabled' | 'error' | 'loaded' = 'loaded') {
  publishPlugin({
    id: 'provider',
    name: 'Provider',
    kind: 'bundled',
    status,
    provides: readModel
  })
}

beforeEach(() => {
  $pluginRecords.set({})
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { api: vi.fn(async request => request) }
  })
})

describe('declared plugin REST capabilities', () => {
  it('fails closed when the consumer did not declare the capability', async () => {
    provider()
    const ctx = createPluginContext('consumer')

    await expect(ctx.services.rest('provider', 'read-model', '/boards')).rejects.toMatchObject({
      code: 'consumer_undeclared'
    })
  })

  it('reaches only the declared provider namespace with a granted GET route', async () => {
    provider()
    const ctx = createPluginContext('consumer', undefined, requiresReadModel)

    await ctx.services.rest('provider', 'read-model', '/board?board=alpha')

    expect(window.hermesDesktop.api).toHaveBeenCalledWith(
      expect.objectContaining({ path: '/api/plugins/provider/board?board=alpha', method: 'GET' })
    )
    await expect(ctx.services.rest('other', 'read-model', '/board')).rejects.toMatchObject({
      code: 'consumer_undeclared'
    })
  })

  it('rejects writes and traversal before touching the desktop bridge', async () => {
    provider()
    const ctx = createPluginContext('consumer', undefined, requiresReadModel)

    await expect(
      ctx.services.rest('provider', 'read-model', '/board', { method: 'POST', body: {} })
    ).rejects.toMatchObject({ code: 'method_not_allowed' })
    await expect(ctx.services.rest('provider', 'read-model', '/../other')).rejects.toMatchObject({
      code: 'path_not_allowed'
    })
    expect(window.hermesDesktop.api).not.toHaveBeenCalled()
  })

  it('rejects provider namespace injection before touching the desktop bridge', async () => {
    publishPlugin({
      id: '../provider',
      name: 'Invalid Provider',
      kind: 'disk',
      status: 'loaded',
      provides: readModel
    })
    const ctx = createPluginContext('consumer', undefined, {
      rest: [{ provider: '../provider', capability: 'read-model' }]
    })

    await expect(ctx.services.rest('../provider', 'read-model', '/board')).rejects.toMatchObject({
      code: 'path_not_allowed'
    })
    expect(window.hermesDesktop.api).not.toHaveBeenCalled()
  })

  it('reports missing and disabled providers distinctly', async () => {
    const ctx = createPluginContext('consumer', undefined, requiresReadModel)

    await expect(ctx.services.rest('provider', 'read-model', '/boards')).rejects.toMatchObject({
      code: 'provider_missing'
    })

    provider('disabled')
    await expect(ctx.services.rest('provider', 'read-model', '/boards')).rejects.toMatchObject({
      code: 'provider_disabled'
    })
  })

  it('preserves same-plugin ctx.rest behavior', async () => {
    const ctx = createPluginContext('consumer')

    await ctx.rest('/write', { method: 'POST', body: { ok: true } })

    expect(window.hermesDesktop.api).toHaveBeenCalledWith(
      expect.objectContaining({
        path: '/api/plugins/consumer/write',
        method: 'POST',
        body: { ok: true }
      })
    )
  })
})
