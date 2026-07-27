import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getGlobalModelOptions } from '@/hermes'
import type { ModelOptionsResponse } from '@/types/hermes'

import {
  manualPickRemoved,
  modelOptionsQueryKey,
  readModelOptionsCache,
  requestModelOptions,
  writeModelOptionsCache
} from './model-options'

const catalog = (model: string): ModelOptionsResponse => ({
  model,
  provider: 'novacode',
  providers: [{ name: 'NovaCode', slug: 'novacode', models: [model] }]
})

const globalOptions = { model: 'hermes-4', provider: 'nous', providers: [] }

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn(() => Promise.resolve(globalOptions))
}))

describe('model options local cache', () => {
  beforeEach(() => localStorage.clear())

  it('uses stable profile/session keys and restores a successful catalog', () => {
    writeModelOptionsCache('default', 'session-1', catalog('gpt-5.6-sol'))

    const cached = readModelOptionsCache('default', 'session-1')

    expect(modelOptionsQueryKey('default', 'session-1')).toEqual(['model-options', 'default', 'session-1'])
    expect(cached?.data).toEqual(catalog('gpt-5.6-sol'))
    expect(cached?.updatedAt).toEqual(expect.any(Number))
  })

  it('reuses the latest profile catalog immediately for a new session id', () => {
    writeModelOptionsCache('default', 'old-session', catalog('gpt-5.6-sol'))

    expect(readModelOptionsCache('default', 'new-session')?.data).toEqual(catalog('gpt-5.6-sol'))
  })

  it('keeps the cache bounded across profiles', () => {
    for (let index = 0; index < 20; index += 1) {
      writeModelOptionsCache(`profile-${index}`, `session-${index}`, catalog(`model-${index}`))
    }

    const raw = localStorage.getItem('hermes.desktop.model-options.v1')
    expect(raw ? Object.keys(JSON.parse(raw)).length : 0).toBe(12)
  })

  it('treats malformed or incomplete entries as a cache miss', () => {
    localStorage.setItem(
      'hermes.desktop.model-options.v1',
      JSON.stringify({ 'default:global': { data: { providers: 'not-an-array' }, updatedAt: 1 } })
    )

    expect(readModelOptionsCache('default')).toBeUndefined()
  })
})

describe('requestModelOptions', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('uses the connected gateway even before a session exists', async () => {
    const gatewayPayload = { model: 'BeastMode', provider: 'moa', providers: [] }
    const gateway = { request: vi.fn(() => Promise.resolve(gatewayPayload)) }

    await expect(requestModelOptions({ gateway: gateway as never, sessionId: null })).resolves.toBe(gatewayPayload)

    expect(gateway.request).toHaveBeenCalledWith('model.options', { explicit_only: true })
    expect(getGlobalModelOptions).not.toHaveBeenCalled()
  })

  it('passes the active session id and refresh flag through the gateway', async () => {
    const gateway = { request: vi.fn(() => Promise.resolve(globalOptions)) }

    await requestModelOptions({ gateway: gateway as never, refresh: true, sessionId: 'session-1' })

    expect(gateway.request).toHaveBeenCalledWith('model.options', {
      explicit_only: true,
      refresh: true,
      session_id: 'session-1'
    })
  })

  it('falls back to REST when no gateway is connected', async () => {
    await requestModelOptions({ refresh: true })

    expect(getGlobalModelOptions).toHaveBeenCalledWith({ explicitOnly: true, refresh: true })
  })
})

describe('modelOptionsQueryKey', () => {
  it('isolates new-chat catalogs by active gateway profile', () => {
    expect(modelOptionsQueryKey('default')).toEqual(['model-options', 'default', 'global'])
    expect(modelOptionsQueryKey('compass')).toEqual(['model-options', 'compass', 'global'])
    expect(modelOptionsQueryKey('default')).not.toEqual(modelOptionsQueryKey('compass'))
  })

  it('keeps session catalogs inside the owning profile namespace', () => {
    expect(modelOptionsQueryKey(' compass ', 'session-1')).toEqual(['model-options', 'compass', 'session-1'])
  })
})

describe('manualPickRemoved', () => {
  const providers = [
    { name: 'OpenRouter', slug: 'openrouter', models: ['owl-alpha', 'gpt-5.5'] },
    { name: 'Nous', slug: 'nous', models: [] }
  ]

  it('flags a pick whose model was dropped from a populated provider', () => {
    expect(manualPickRemoved(providers, 'openrouter', 'nemotron-removed')).toBe(true)
  })

  it('keeps a pick that is still in the catalog', () => {
    expect(manualPickRemoved(providers, 'openrouter', 'gpt-5.5')).toBe(false)
  })

  it('matches the provider by name as well as slug', () => {
    expect(manualPickRemoved(providers, 'OpenRouter', 'gpt-5.5')).toBe(false)
    expect(manualPickRemoved(providers, 'OpenRouter', 'gone')).toBe(true)
  })

  it('never clobbers when the provider is absent', () => {
    expect(manualPickRemoved(providers, 'anthropic', 'claude-sonnet-4.6')).toBe(false)
  })

  it('never clobbers when the provider has an empty model list', () => {
    expect(manualPickRemoved(providers, 'nous', 'hermes-4')).toBe(false)
  })

  it('never clobbers on a not-yet-loaded or empty catalog', () => {
    expect(manualPickRemoved(undefined, 'openrouter', 'gpt-5.5')).toBe(false)
    expect(manualPickRemoved([], 'openrouter', 'gpt-5.5')).toBe(false)
  })

  it('never clobbers when there is no pick', () => {
    expect(manualPickRemoved(providers, '', '')).toBe(false)
  })
})
