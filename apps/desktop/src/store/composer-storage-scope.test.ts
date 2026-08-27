import { describe, expect, it } from 'vitest'

import {
  decodeComposerStorageScopeKey,
  encodeComposerStorageScopeKey,
  legacyComposerStorageScopeKey
} from './composer-storage-scope'

const remoteOwner = { connectionId: ' source-a ', profile: ' omar ' }

describe('composer storage scope codec', () => {
  it('round-trips the exact owner and raw stored identity without delimiter collisions', () => {
    const key = encodeComposerStorageScopeKey(remoteOwner, 'stored::shared\0-looking')

    expect(decodeComposerStorageScopeKey(key)).toEqual({
      format: 'canonical',
      newChatGeneration: 0,
      owner: { connectionId: 'source-a', profile: 'omar' },
      storedSessionId: 'stored::shared\0-looking'
    })
  })

  it('normalizes local/default and represents New Chat as a null raw identity', () => {
    const key = encodeComposerStorageScopeKey({ connectionId: '', profile: '' }, null)

    expect(decodeComposerStorageScopeKey(key)).toEqual({
      format: 'canonical',
      newChatGeneration: 0,
      owner: { connectionId: 'local', profile: 'default' },
      storedSessionId: null
    })
    expect(encodeComposerStorageScopeKey({ connectionId: 'local', profile: 'default' }, null)).toBe(key)
  })

  it('fails closed on malformed and non-canonical keys', () => {
    expect(decodeComposerStorageScopeKey('omar\0stored-a')).toBeNull()
    expect(decodeComposerStorageScopeKey('')).toBeNull()
    expect(decodeComposerStorageScopeKey('["composer",1,"local","default",""]')).toBeNull()
  })

  it('decodes the profileScopeKey compatibility shape only with its exact expected owner', () => {
    const legacy = legacyComposerStorageScopeKey(remoteOwner, 'stored-a')

    expect(decodeComposerStorageScopeKey(legacy)).toBeNull()
    expect(decodeComposerStorageScopeKey(legacy, { legacyOwner: remoteOwner })).toEqual({
      format: 'legacy',
      newChatGeneration: 0,
      owner: { connectionId: 'source-a', profile: 'omar' },
      storedSessionId: 'stored-a'
    })
    expect(
      decodeComposerStorageScopeKey(legacy, { legacyOwner: { connectionId: 'source-b', profile: 'omar' } })
    ).toBeNull()
  })

  it('normalizes the legacy New Chat sentinel to a null raw identity', () => {
    const owner = { connectionId: 'local', profile: 'default' }
    const legacy = legacyComposerStorageScopeKey(owner, null)

    expect(decodeComposerStorageScopeKey(legacy, { legacyOwner: owner })).toMatchObject({
      format: 'legacy',
      storedSessionId: null
    })
  })

  it('round-trips New Chat generations but ignores them for stored sessions', () => {
    const owner = { connectionId: 'local', profile: 'default' }
    const newChat = encodeComposerStorageScopeKey(owner, null, 7)

    expect(decodeComposerStorageScopeKey(newChat)).toMatchObject({
      newChatGeneration: 7,
      storedSessionId: null
    })
    expect(encodeComposerStorageScopeKey(owner, 'stored-a', 7)).toBe(
      encodeComposerStorageScopeKey(owner, 'stored-a', 0)
    )
  })
})
