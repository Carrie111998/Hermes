import { describe, expect, it } from 'vitest'

import { resolvePaletteSearchStatus } from './search-status'

describe('resolvePaletteSearchStatus', () => {
  it('reports loading while a cold search has no result yet', () => {
    expect(
      resolvePaletteSearchStatus(true, [
        { hasData: false, isError: false, isPending: true },
        { hasData: false, isError: false, isPending: false }
      ])
    ).toBe('loading')
  })

  it('reports an error instead of a misleading empty result', () => {
    expect(
      resolvePaletteSearchStatus(true, [
        { hasData: false, isError: true, isPending: false },
        { hasData: false, isError: false, isPending: true }
      ])
    ).toBe('error')
  })

  it('does not replace usable scoped data with a loading message during refetch', () => {
    expect(resolvePaletteSearchStatus(true, [{ hasData: true, isError: false, isPending: false }])).toBeUndefined()
    expect(resolvePaletteSearchStatus(false, [{ hasData: false, isError: false, isPending: true }])).toBeUndefined()
  })
})
