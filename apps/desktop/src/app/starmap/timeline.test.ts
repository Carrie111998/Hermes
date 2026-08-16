import { describe, expect, it } from 'vitest'

import { allocateStarKinds } from './time-axis'

describe('allocateStarKinds', () => {
  it('conserves the rendered count without inventing an absent kind', () => {
    const kinds = allocateStarKinds({ memory: 0, skill: 1, total: 2, wiki: 1 }, 3)

    expect(kinds).toHaveLength(3)
    expect(kinds).not.toContain('memory')
    expect(kinds.filter(kind => kind === 'skill')).toHaveLength(2)
    expect(kinds.filter(kind => kind === 'wiki')).toHaveLength(1)
  })

  it('preserves all stars for a single populated kind', () => {
    expect(allocateStarKinds({ memory: 4, skill: 0, total: 4, wiki: 0 }, 3)).toEqual(['memory', 'memory', 'memory'])
  })
})
