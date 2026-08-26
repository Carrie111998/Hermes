import { describe, expect, it } from 'vitest'

import { profilesWithBreathing } from './profile-breath'

describe('profilesWithBreathing', () => {
  it('marks a profile whose listed row resolved to unread', () => {
    const byId = { s1: 'unread', s2: 'idle' }

    const rows = [
      { archived: false, id: 's1', profile: 'coder' },
      { id: 's2', profile: 'design' }
    ]

    expect(profilesWithBreathing(byId, rows)).toEqual({ coder: true })
  })

  it('falls back to default when the row carries no profile', () => {
    const byId = { s1: 'unread' }

    expect(profilesWithBreathing(byId, [{ id: 's1' }])).toEqual({ default: true })
  })

  it('ignores archived rows and non-unread states', () => {
    const byId = { s1: 'unread', s2: 'unread', s3: 'working' }

    const rows = [
      { archived: true, id: 's1', profile: 'coder' },
      { id: 's2', profile: 'coder' },
      { id: 's3', profile: 'design' }
    ]

    expect(profilesWithBreathing(byId, rows)).toEqual({ coder: true })
  })

  it('aggregates across multiple lists (sessions + messaging)', () => {
    const byId = { s1: 'unread', m1: 'unread' }

    expect(
      profilesWithBreathing(
        byId,
        [{ id: 's1', profile: 'quant' }],
        [{ id: 'm1', profile: 'media' }]
      )
    ).toEqual({ media: true, quant: true })
  })

  it('returns empty when nothing is unread', () => {
    expect(profilesWithBreathing({}, [{ id: 's1', profile: 'coder' }])).toEqual({})
    expect(profilesWithBreathing({ s1: 'working' }, [{ id: 's1', profile: 'coder' }])).toEqual({})
  })
})
