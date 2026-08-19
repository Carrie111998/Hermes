import { describe, expect, it } from 'vitest'

import { type OpenRouterRoutingSummaryCopy, summarizeOpenRouterRoute } from './openrouter-routing-summary'

const copy: OpenRouterRoutingSummaryCopy = {
  automatic: 'OpenRouter chooses the best available provider.',
  selectedOnly: 'Requests use {endpoint} only.',
  selectedPrefer: 'Prefers {endpoint}; may use other providers if unavailable.',
  blockedOnly: 'OpenRouter chooses any provider except {blocked}.',
  selectedPreferBlocked: 'Prefers {endpoint}; may fall back to others except {blocked}.',
  endpointWithQuantization: '{provider} ({quantization})',
  blockedJoinTwo: '{first} and {second}',
  blockedJoinMany: '{items}, and {last}',
  blockedListSeparator: ', '
}

describe('summarizeOpenRouterRoute', () => {
  it.each([
    [
      'nothing selected, nothing blocked',
      { selectedTag: '', selectedProviderName: '', quantization: '', allowFallbacks: true, blockedTags: [], blockedProviderNames: [] },
      'OpenRouter chooses the best available provider.'
    ],
    [
      'selected with fallback off',
      { selectedTag: 'baidu/fp8', selectedProviderName: 'Baidu Qianfan', quantization: 'FP8', allowFallbacks: false, blockedTags: [], blockedProviderNames: [] },
      'Requests use Baidu Qianfan (FP8) only.'
    ],
    [
      'selected with fallback on',
      { selectedTag: 'baidu/fp8', selectedProviderName: 'Baidu Qianfan', quantization: 'FP8', allowFallbacks: true, blockedTags: [], blockedProviderNames: [] },
      'Prefers Baidu Qianfan (FP8); may use other providers if unavailable.'
    ],
    [
      'nothing selected with one blocked provider',
      { selectedTag: '', selectedProviderName: '', quantization: '', allowFallbacks: true, blockedTags: ['fireworks'], blockedProviderNames: ['Fireworks'] },
      'OpenRouter chooses any provider except Fireworks.'
    ],
    [
      'selected with fallback and one blocked provider',
      { selectedTag: 'baidu/fp8', selectedProviderName: 'Baidu Qianfan', quantization: 'FP8', allowFallbacks: true, blockedTags: ['fireworks'], blockedProviderNames: ['Fireworks'] },
      'Prefers Baidu Qianfan (FP8); may fall back to others except Fireworks.'
    ],
    [
      'selected with fallback and multiple blocked providers',
      { selectedTag: 'baidu/fp8', selectedProviderName: 'Baidu Qianfan', quantization: 'FP8', allowFallbacks: true, blockedTags: ['fireworks', 'together', 'deepinfra'], blockedProviderNames: ['Fireworks', 'Together AI', 'DeepInfra'] },
      'Prefers Baidu Qianfan (FP8); may fall back to others except Fireworks, Together AI, and DeepInfra.'
    ],
    [
      'missing quantization',
      { selectedTag: 'baidu', selectedProviderName: 'Baidu Qianfan', quantization: '', allowFallbacks: false, blockedTags: [], blockedProviderNames: [] },
      'Requests use Baidu Qianfan only.'
    ]
  ] as const)('%s', (_name, route, expected) => {
    expect(summarizeOpenRouterRoute(route, copy)).toBe(expected)
  })
})
