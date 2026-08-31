import { describe, expect, it } from 'vitest'

import { collectKeptProfileKeys } from './backend-usage'

describe('collectKeptProfileKeys', () => {
  it('includes profiles of working or attention sessions', () => {
    const sessions = [
      { id: 'a', profile: 'default' },
      { id: 'b', profile: 'repro91050' },
      { id: 'c', profile: 'other' }
    ]
    expect(collectKeptProfileKeys(sessions, ['b'], ['a'])).toEqual(['default', 'repro91050'])
  })

  it('treats a missing profile as default', () => {
    const sessions = [{ id: 'a' }, { id: 'b', profile: '  ' }]
    expect(collectKeptProfileKeys(sessions, ['a', 'b'], [])).toEqual(['default'])
  })

  it('returns empty when nothing is live', () => {
    const sessions = [{ id: 'a', profile: 'default' }]
    expect(collectKeptProfileKeys(sessions, [], [])).toEqual([])
  })

  it('spares default when a live session id is missing from the list', () => {
    expect(collectKeptProfileKeys([{ id: 'named', profile: 'repro91050' }], [], ['orphan'])).toEqual(['default'])
  })

  it('spares the launch profile when a live id is missing and a primary key is passed', () => {
    expect(collectKeptProfileKeys([], [], ['orphan'], 'coder')).toEqual(['coder'])
  })
})
