import { describe, expect, it } from 'vitest'

import { countCrossProfilePins, windowSessionsIncludingPins } from './session-window'

const row = (id: string, profile: string, pinned = false) => ({ id, pinned, profile })

describe('windowSessionsIncludingPins', () => {
  it('preserves durable pins beyond the recency window', () => {
    const rows = [row('recent', 'default'), row('old-pin', 'default', true), row('old-ordinary', 'default')]

    expect(windowSessionsIncludingPins(rows, 0, 1).map(session => session.id)).toEqual(['recent', 'old-pin'])
  })
})

describe('countCrossProfilePins', () => {
  it('counts pinned sibling rows without conflating cloned session ids', () => {
    const candidates = [row('same-id', 'default', true), row('same-id', 'work', true), row('latest-work', 'work')]

    expect(countCrossProfilePins(candidates, 'default')).toBe(1)
  })
})
