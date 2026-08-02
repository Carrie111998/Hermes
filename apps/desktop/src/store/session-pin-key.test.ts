import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import {
  createSessionPinKey,
  parseSessionPinKey,
  sessionMatchesPinKey,
  sessionPinIdsForScope,
  sessionPinKey
} from './session-pin-key'

const row = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 1, source: 'desktop', started_at: 0, title: id, ...extra }) as SessionInfo

describe('profile-qualified session pin keys', () => {
  it('round-trips profile and durable lineage id', () => {
    const key = createSessionPinKey('work', 'root')

    expect(parseSessionPinKey(key)).toEqual({ id: 'root', profile: 'work' })
    expect(sessionPinKey(row('tip', { _lineage_root_id: 'root', profile: 'work' }))).toBe(key)
  })

  it('treats an absent profile as default', () => {
    expect(parseSessionPinKey(createSessionPinKey(undefined, 'chat'))).toEqual({
      id: 'chat',
      profile: 'default'
    })
  })

  it('does not match a cloned id owned by another profile', () => {
    const workKey = createSessionPinKey('work', 'shared')

    expect(sessionMatchesPinKey(row('shared', { profile: 'work' }), workKey)).toBe(true)
    expect(sessionMatchesPinKey(row('shared', { profile: 'default' }), workKey)).toBe(false)
  })

  it('unwraps only keys in the requested scope while retaining unresolved legacy ids', () => {
    const keys = [createSessionPinKey('default', 'default-pin'), createSessionPinKey('work', 'work-pin'), 'legacy-pin']

    expect(sessionPinIdsForScope(keys, 'work')).toEqual(['work-pin', 'legacy-pin'])
    expect(sessionPinIdsForScope(keys)).toEqual(['default-pin', 'work-pin', 'legacy-pin'])
  })
})
