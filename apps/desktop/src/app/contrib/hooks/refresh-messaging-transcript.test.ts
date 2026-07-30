import { describe, expect, it, vi } from 'vitest'

import {
  resolveMessagingTranscriptTarget,
  resolveOpenMessagingStoredSessionIds
} from './refresh-messaging-transcript'

const session = (id: string, source: null | string = null) => ({ id, profile: 'default', source })

describe('resolveMessagingTranscriptTarget', () => {
  it('uses the target runtime state instead of an unrelated busy primary runtime', () => {
    const busyByRuntimeId = new Map([
      ['runtime-primary', true],
      ['runtime-messaging', false]
    ])

    const isRuntimeBusy = vi.fn((runtimeId: string) => busyByRuntimeId.get(runtimeId) ?? false)

    const target = resolveMessagingTranscriptTarget({
      getRuntimeIdForStoredSession: storedId => (storedId === 'stored-messaging' ? 'runtime-messaging' : null),
      isRuntimeBusy,
      messagingSessions: [{ id: 'stored-messaging', profile: 'default', source: 'qqbot' }],
      selectedStoredSessionId: 'stored-messaging'
    })

    expect(target).toEqual({
      profile: 'default',
      runtimeSessionId: 'runtime-messaging',
      storedSessionId: 'stored-messaging'
    })
    expect(isRuntimeBusy).toHaveBeenCalledWith('runtime-messaging')
    expect(isRuntimeBusy).not.toHaveBeenCalledWith('runtime-primary')
  })
})

describe('resolveOpenMessagingStoredSessionIds', () => {
  it('includes a messaging tile when the primary view is a non-messaging session', () => {
    const primary = session('stored-primary')
    const messaging = session('stored-messaging', 'qqbot')

    expect(
      resolveOpenMessagingStoredSessionIds({
        messagingSessions: [messaging],
        selectedStoredSessionId: primary.id,
        sessionTiles: [{ storedSessionId: messaging.id }]
      })
    ).toEqual([messaging.id])
  })

  it('deduplicates a messaging session shown in both the primary view and a tile', () => {
    const messaging = session('stored-messaging', 'qqbot')

    expect(
      resolveOpenMessagingStoredSessionIds({
        messagingSessions: [messaging],
        selectedStoredSessionId: messaging.id,
        sessionTiles: [{ storedSessionId: messaging.id }]
      })
    ).toEqual([messaging.id])
  })
})
