import { describe, expect, it } from 'vitest'

import { inputWithCacheWrite, totalProcessedTokens } from './analytics'

describe('inputWithCacheWrite', () => {
  it('adds cache-write tokens back onto the residual input bucket', () => {
    // Mirrors issue #95707: residual 477 + 598,311 cache-write tokens.
    expect(inputWithCacheWrite({ input_tokens: 477, cache_write_tokens: 598_311 })).toBe(598_788)
  })

  it('tolerates missing cache-write counts', () => {
    expect(inputWithCacheWrite({ input_tokens: 42, cache_write_tokens: undefined as unknown as number })).toBe(42)
    expect(inputWithCacheWrite({ input_tokens: 0, cache_write_tokens: 0 })).toBe(0)
  })
})

describe('totalProcessedTokens', () => {
  it('sums newly-processed input, cache reads, and output', () => {
    expect(
      totalProcessedTokens({
        input_tokens: 477,
        cache_write_tokens: 598_311,
        cache_read_tokens: 12_953_064,
        output_tokens: 30_392,
      }),
    ).toBe(13_582_244)
  })

  it('degrades to plain input + output when cache buckets are missing', () => {
    expect(
      totalProcessedTokens({
        input_tokens: 100,
        cache_write_tokens: undefined as unknown as number,
        cache_read_tokens: undefined as unknown as number,
        output_tokens: 30,
      }),
    ).toBe(130)
  })
})
