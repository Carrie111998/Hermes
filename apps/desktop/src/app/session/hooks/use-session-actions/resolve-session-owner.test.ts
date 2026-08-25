import { afterEach, describe, expect, it } from 'vitest'

import { $sessions, setSessionOwnerHint, setSessions } from '@/store/session'

import { resolveSessionOwner } from './utils'

afterEach(() => {
  setSessions([])
})

describe('resolveSessionOwner', () => {
  it('prefers an explicit owner hint with connectionId', async () => {
    setSessionOwnerHint('20260825_183656_9a1342', {
      connectionId: 'remote-a',
      mode: 'remote',
      profile: 'default'
    })

    await expect(resolveSessionOwner('20260825_183656_9a1342')).resolves.toMatchObject({
      connectionId: 'remote-a',
      profile: 'default'
    })
  })

  it('uses the cached row connection_id when no hint exists', async () => {
    $sessions.set([
      {
        id: '20260825_999999_aaaaaa',
        title: 'remote chat',
        connection_id: 'remote-a',
        profile: 'default'
      } as never
    ])

    await expect(resolveSessionOwner('20260825_999999_aaaaaa')).resolves.toMatchObject({
      connectionId: 'remote-a',
      profile: 'default'
    })
  })

  it('returns undefined for a local session so submit stays on the ambient socket', async () => {
    $sessions.set([
      {
        id: 'stored-db-xyz789',
        title: 'local work',
        profile: 'work'
      } as never
    ])

    await expect(resolveSessionOwner('stored-db-xyz789')).resolves.toBeUndefined()
  })
})
