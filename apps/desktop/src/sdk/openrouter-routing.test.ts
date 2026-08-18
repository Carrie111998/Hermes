import { describe, expect, it } from 'vitest'

import {
  getHermesConfigRecord,
  getOpenRouterEndpoints,
  isOpenRouterProvider,
  OpenRouterModelInput,
  openRouterRoutingDraft,
  OpenRouterRoutingField,
  saveHermesConfig,
  updateOpenRouterRoutingConfig
} from '@/sdk'

// The SDK is the only surface a plugin (e.g. hermes-bots/plugin.js) may import
// from — see eslint.config.mjs's plugin fence. This pins that the reviewed
// OpenRouter routing UI + persistence + boundary helpers are actually
// reachable through it, so Bot Mode's New Agent dialog can wire the same
// picker Settings > Model and Profiles > New Profile use instead of
// reimplementing it.
describe('OpenRouter routing surface is exported from the plugin SDK', () => {
  it('exports the reviewed routing components', () => {
    expect(typeof OpenRouterRoutingField).toBe('function')
    expect(typeof OpenRouterModelInput).toBe('function')
  })

  it('exports the pure boundary helpers', () => {
    expect(typeof isOpenRouterProvider).toBe('function')
    expect(typeof openRouterRoutingDraft).toBe('function')
    expect(typeof updateOpenRouterRoutingConfig).toBe('function')
  })

  it('exports the REST calls needed to discover endpoints and persist the override', () => {
    expect(typeof getOpenRouterEndpoints).toBe('function')
    expect(typeof getHermesConfigRecord).toBe('function')
    expect(typeof saveHermesConfig).toBe('function')
  })
})
