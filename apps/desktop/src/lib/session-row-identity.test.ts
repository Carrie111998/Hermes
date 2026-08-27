import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import { sessionRowIdentity } from './session-row-identity'

const row = (overrides: Partial<SessionInfo>): SessionInfo =>
  ({ id: 'shared', profile: ' worker ', ...overrides }) as SessionInfo

describe('sessionRowIdentity', () => {
  it('qualifies same-profile duplicate ids by owning connection', () => {
    expect(sessionRowIdentity(row({ connection_id: 'source-a' }))).not.toBe(
      sessionRowIdentity(row({ connection_id: 'source-b' }))
    )
  })

  it('normalizes local ownership and profile whitespace', () => {
    expect(sessionRowIdentity(row({ connection_id: undefined }))).toBe(
      sessionRowIdentity(row({ connection_id: ' local ', profile: 'worker' }))
    )
  })

  it('stays stable when a compression tip rotates within one lineage', () => {
    const before = row({ _lineage_root_id: 'root', connection_id: 'source-a', id: 'tip-4' })
    const after = row({ _lineage_root_id: 'root', connection_id: 'source-a', id: 'tip-5' })

    expect(sessionRowIdentity(before)).toBe(sessionRowIdentity(after))
  })
})
