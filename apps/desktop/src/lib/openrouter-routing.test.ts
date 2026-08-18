import { describe, expect, it } from 'vitest'

import { isOpenRouterProvider, openRouterRoutingDraft, updateOpenRouterRoutingConfig } from './openrouter-routing'

describe('OpenRouter routing state', () => {
  describe('isOpenRouterProvider', () => {
    it('matches the canonical slug regardless of case', () => {
      expect(isOpenRouterProvider('openrouter')).toBe(true)
      expect(isOpenRouterProvider('OpenRouter')).toBe(true)
      expect(isOpenRouterProvider('OPENROUTER')).toBe(true)
      expect(isOpenRouterProvider('  openrouter  ')).toBe(true)
    })

    it('does not match a user-defined custom: provider even if it fronts OpenRouter', () => {
      // A custom:-prefixed slug is a user-defined OpenAI-compatible endpoint.
      // The backend's OpenRouter-specific provider_routing resolution
      // (agent_init.py _resolve_openrouter_provider_routing) gates on the
      // literal provider slug "openrouter" — a custom:openrouter request
      // never receives those fields server-side even if its base_url happens
      // to be openrouter.ai. Showing OpenRouter-only routing controls for it
      // would let a user configure a lock/route that silently never applies.
      expect(isOpenRouterProvider('custom:openrouter')).toBe(false)
      expect(isOpenRouterProvider('custom:OpenRouter')).toBe(false)
    })

    it('does not match unrelated provider slugs', () => {
      expect(isOpenRouterProvider('anthropic')).toBe(false)
      expect(isOpenRouterProvider('openai')).toBe(false)
      expect(isOpenRouterProvider('')).toBe(false)
      expect(isOpenRouterProvider(undefined)).toBe(false)
    })
  })

  it('canonicalizes configured tags when they enter the shared draft', () => {
    const draft = openRouterRoutingDraft(
      {
        provider_routing: {
          model_overrides: {
            openrouter: {
              'deepseek/model': { only: ['  baidu/fp8  '], ignore: [' digitalocean ', '', 'digitalocean'] }
            }
          }
        }
      },
      'deepseek/model'
    )

    expect(draft.providerTag).toBe('baidu/fp8')
    expect(draft.blockedTags).toEqual(['digitalocean'])
  })
  it('strips the selected tag from ignore even when a colliding draft bypasses the UI guard', () => {
    const config = updateOpenRouterRoutingConfig({}, 'deepseek/model', {
      allowFallbacks: false,
      blockedTags: ['baidu/fp8', 'digitalocean'],
      providerTag: 'baidu/fp8',
      quantization: 'fp8'
    })

    expect((config.provider_routing as any).model_overrides.openrouter['deepseek/model']).toEqual({
      only: ['baidu/fp8'],
      allow_fallbacks: false,
      quantizations: ['fp8'],
      ignore: ['digitalocean']
    })
  })
})
