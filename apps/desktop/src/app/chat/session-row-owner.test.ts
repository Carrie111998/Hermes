import { describe, expect, it } from 'vitest'

import { sessionRowForOwner, sessionRowOwnerRoute } from './session-row-owner'

describe('sessionRowOwnerRoute', () => {
  it('preserves exact remote ownership', () => {
    expect(
      sessionRowOwnerRoute({
        connection_id: 'source-b',
        id: 'shared-id',
        profile: 'worker'
      } as never)
    ).toEqual({
      connectionId: 'source-b',
      profile: 'worker',
      targetProfile: 'worker'
    })
  })

  it('marks ownerless-connection profile rows as local', () => {
    expect(sessionRowOwnerRoute({ id: 'local-id', profile: 'default' } as never)).toEqual({
      connectionId: 'local',
      mode: 'local',
      profile: 'default',
      targetProfile: 'default'
    })
  })

  it('fails closed when a legacy row has no profile proof', () => {
    expect(sessionRowOwnerRoute({ id: 'legacy-id' } as never)).toBeUndefined()
  })
})

describe('sessionRowForOwner', () => {
  const rows = [
    { connection_id: 'source-a', id: 'shared-id', profile: 'worker', title: 'Owner A' },
    { connection_id: 'source-b', id: 'shared-id', profile: 'worker', title: 'Owner B' }
  ] as never

  it('selects the exact owner row when duplicate ids exist', () => {
    expect(
      sessionRowForOwner(rows, 'shared-id', {
        connectionId: 'source-b',
        mode: 'remote',
        profile: 'worker'
      })?.title
    ).toBe('Owner B')
  })

  it('fails closed on duplicate ids without owner proof', () => {
    expect(sessionRowForOwner(rows, 'shared-id')).toBeUndefined()
  })
})
