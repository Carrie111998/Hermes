import { describe, expect, it } from 'vitest'

import { activeMemoryProviders } from './helpers'

describe('activeMemoryProviders (FR-1 resolver mirror)', () => {
  it('reads the ordered providers list', () => {
    expect(activeMemoryProviders(['honcho', 'mem0'])).toEqual(['honcho', 'mem0'])
  })

  it('preserves priority order exactly', () => {
    expect(activeMemoryProviders(['mem0', 'honcho', 'hindsight'])).toEqual(['mem0', 'honcho', 'hindsight'])
  })

  it('de-dups order-preservingly (first occurrence wins)', () => {
    expect(activeMemoryProviders(['honcho', 'mem0', 'honcho'])).toEqual(['honcho', 'mem0'])
  })

  it('drops built-in sentinels and blanks', () => {
    expect(activeMemoryProviders(['honcho', 'built-in', '', 'builtin', 'none', '  ', 'mem0'])).toEqual([
      'honcho',
      'mem0'
    ])
  })

  it('coerces a legacy singular string into a one-element list', () => {
    expect(activeMemoryProviders('honcho')).toEqual(['honcho'])
  })

  it('returns [] for an empty string (built-in only)', () => {
    expect(activeMemoryProviders('')).toEqual([])
  })

  it('returns [] for undefined / non-list / non-string', () => {
    expect(activeMemoryProviders(undefined)).toEqual([])
    expect(activeMemoryProviders(null)).toEqual([])
    expect(activeMemoryProviders({ providers: ['honcho'] })).toEqual([])
  })
})
