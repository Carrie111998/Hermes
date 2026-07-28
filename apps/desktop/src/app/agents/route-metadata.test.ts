import { describe, expect, it } from 'vitest'

import { routeMetadataLabels } from './route-metadata'

describe('routeMetadataLabels', () => {
  it('orders the semantic profile before the actual route', () => {
    expect(
      routeMetadataLabels({
        model: 'gpt-verifier-canary',
        provider: 'openai-codex',
        reasoningEffort: 'max',
        workerProfile: 'verifier'
      })
    ).toEqual(['verifier', 'openai-codex', 'gpt-verifier-canary', 'max'])
  })

  it('omits unavailable route metadata', () => {
    expect(routeMetadataLabels({ model: 'legacy-model' })).toEqual(['legacy-model'])
  })
})
