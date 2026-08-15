import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getGlobalModelOptions } from '@/hermes'
import { $activeGatewayProfile } from '@/store/profile'

import {
  _resetLastLoadedProvidersForTests,
  manualPickRemoved,
  modelOptionsQueryKey,
  requestModelOptions,
  sessionProviderAdoptable,
  sessionProviderAdoptableFromCache
} from './model-options'

const globalOptions = { model: 'hermes-4', provider: 'nous', providers: [] }

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn(() => Promise.resolve(globalOptions)),
  setApiRequestProfile: vi.fn()
}))

describe('requestModelOptions', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('uses the connected gateway even before a session exists', async () => {
    const gatewayPayload = { model: 'BeastMode', provider: 'moa', providers: [] }

    const gateway = {
      request: vi.fn(() => Promise.resolve(gatewayPayload))
    }

    await expect(requestModelOptions({ gateway: gateway as never, sessionId: null })).resolves.toBe(gatewayPayload)

    expect(gateway.request).toHaveBeenCalledWith('model.options', { explicit_only: true })
    expect(getGlobalModelOptions).not.toHaveBeenCalled()
  })

  it('passes the active session id and refresh flag through the gateway', async () => {
    const gateway = {
      request: vi.fn(() => Promise.resolve(globalOptions))
    }

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
    { name: 'Nous', slug: 'nous', models: [] } // present but unconfigured / re-auth
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

  it('never clobbers when the provider is absent (ambiguous / deauth)', () => {
    expect(manualPickRemoved(providers, 'anthropic', 'claude-sonnet-4.6')).toBe(false)
  })

  it('never clobbers when the provider has an empty model list (re-auth)', () => {
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

describe('sessionProviderAdoptable', () => {
  const providers = [
    { name: 'DeepSeek', slug: 'deepseek', models: ['deepseek-v4-flash'], authenticated: true },
    // Current-provider skeleton row: present but credentials are gone.
    { name: 'xAI', slug: 'xai-oauth', models: ['grok-4.20-0309-reasoning'], authenticated: false },
    // Legacy row without an explicit flag — treat as usable.
    { name: 'OpenRouter', slug: 'openrouter', models: ['gpt-5.5'] }
  ]

  it('adopts a provider that is in the loaded catalog with credentials', () => {
    expect(sessionProviderAdoptable(providers, 'deepseek')).toBe(true)
  })

  it('adopts a provider whose row carries no explicit authenticated flag', () => {
    expect(sessionProviderAdoptable(providers, 'openrouter')).toBe(true)
  })

  it('matches the provider by name as well as slug', () => {
    expect(sessionProviderAdoptable(providers, 'DeepSeek')).toBe(true)
  })

  it('blocks a provider explicitly marked unauthenticated (re-auth skeleton)', () => {
    expect(sessionProviderAdoptable(providers, 'xai-oauth')).toBe(false)
    expect(sessionProviderAdoptable(providers, 'xAI')).toBe(false)
  })

  it('blocks a provider absent from a loaded catalog (unconfigured)', () => {
    expect(sessionProviderAdoptable(providers, 'anthropic')).toBe(false)
  })

  it('conservatively adopts on a not-yet-loaded catalog or empty provider', () => {
    expect(sessionProviderAdoptable(undefined, 'xai-oauth')).toBe(true)
    expect(sessionProviderAdoptable(providers, '')).toBe(true)
  })

  it('treats an empty loaded catalog as nothing configured (no adoption)', () => {
    expect(sessionProviderAdoptable([], 'xai-oauth')).toBe(false)
  })
})

describe('sessionProviderAdoptableFromCache', () => {
  const cachedCatalog = [
    { name: 'DeepSeek', slug: 'deepseek', models: ['deepseek-v4-flash'], authenticated: true },
    { name: 'xAI', slug: 'xai-oauth', models: ['grok-4.20-0309-reasoning'], authenticated: false }
  ]

  beforeEach(() => {
    _resetLastLoadedProvidersForTests()
    $activeGatewayProfile.set('default')
  })

  afterEach(() => {
    $activeGatewayProfile.set('default')
  })

  it('adopts when no catalog has loaded yet', () => {
    expect(sessionProviderAdoptableFromCache('xai-oauth')).toBe(true)
  })

  it('blocks a provider the last loaded catalog proves unauthenticated', async () => {
    await requestModelOptions({
      gateway: {
        request: vi.fn(() =>
          Promise.resolve({ model: 'deepseek-v4-flash', provider: 'deepseek', providers: cachedCatalog })
        )
      } as never,
      sessionId: null
    })

    expect(sessionProviderAdoptableFromCache('deepseek')).toBe(true)
    expect(sessionProviderAdoptableFromCache('xai-oauth')).toBe(false)
    expect(sessionProviderAdoptableFromCache('anthropic')).toBe(false)
  })

  it('keys the mirror by profile: another profile catalog never leaks in', async () => {
    await requestModelOptions({
      gateway: {
        request: vi.fn(() =>
          Promise.resolve({ model: 'deepseek-v4-flash', provider: 'deepseek', providers: cachedCatalog })
        )
      } as never,
      sessionId: null
    })
    // Profile 'default' now has a loaded catalog that would block xai-oauth.

    $activeGatewayProfile.set('compass')

    // The active profile has no entry yet — conservative, nothing to prove.
    expect(sessionProviderAdoptableFromCache('xai-oauth')).toBe(true)
    expect(sessionProviderAdoptableFromCache('deepseek')).toBe(true)

    // A catalog fetched for 'compass' lands in its own slot and drives its own
    // decisions while active...
    await requestModelOptions({
      gateway: {
        request: vi.fn(() =>
          Promise.resolve({
            model: 'claude-sonnet-4.6',
            provider: 'anthropic',
            providers: [{ name: 'Anthropic', slug: 'anthropic', models: ['claude-sonnet-4.6'] }]
          })
        )
      } as never,
      sessionId: null
    })
    expect(sessionProviderAdoptableFromCache('anthropic')).toBe(true)
    expect(sessionProviderAdoptableFromCache('xai-oauth')).toBe(false)

    // ...while 'default' keeps its own view when re-activated (anthropic is
    // absent from its catalog, so it must NOT inherit compass's verdict).
    $activeGatewayProfile.set('default')
    expect(sessionProviderAdoptableFromCache('anthropic')).toBe(false)
    expect(sessionProviderAdoptableFromCache('xai-oauth')).toBe(false)
    expect(sessionProviderAdoptableFromCache('deepseek')).toBe(true)
  })
})
