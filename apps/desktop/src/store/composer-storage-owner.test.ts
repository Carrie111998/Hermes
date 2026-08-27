import { describe, expect, it } from 'vitest'

import { resolveComposerStorageOwner } from './composer-storage-owner'

const ambientOwner = { connectionId: 'source-a', profile: 'default' }

describe('resolveComposerStorageOwner', () => {
  it('prefers an exact primary-open intent when duplicate stored ids make hints ambiguous', () => {
    expect(
      resolveComposerStorageOwner({
        ambientOwner,
        isPrimary: true,
        knownOwner: undefined,
        primaryIntent: {
          ownerRoute: { connectionId: 'source-b', mode: 'remote', profile: 'profile-b' },
          storedSessionId: 'shared-id'
        },
        selectedSessionId: 'shared-id'
      })
    ).toEqual({ connectionId: 'source-b', profile: 'profile-b' })
  })

  it('uses the exact New Chat create owner instead of ambient chrome', () => {
    expect(
      resolveComposerStorageOwner({
        ambientOwner,
        isPrimary: true,
        newChatOwner: { connectionId: 'source-c', mode: 'remote', profile: 'profile-c' },
        selectedSessionId: null
      })
    ).toEqual({ connectionId: 'source-c', profile: 'profile-c' })
  })

  it('keeps an exact tile owner authoritative', () => {
    expect(
      resolveComposerStorageOwner({
        ambientOwner,
        isPrimary: false,
        selectedSessionId: 'shared-id',
        tileOwner: { connectionId: 'source-tile', mode: 'remote', profile: 'tile-profile' }
      })
    ).toEqual({ connectionId: 'source-tile', profile: 'tile-profile' })
  })
})
