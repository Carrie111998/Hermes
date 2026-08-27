import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import {
  readSessionListSnapshot,
  SESSION_LIST_CACHE_STORAGE_KEY,
  sessionListCacheScopeKey,
  writeSessionListSnapshot
} from './session-list-cache'

const row = (id: string, profile = 'default'): SessionInfo =>
  ({
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: false,
    last_active: 1000,
    message_count: 2,
    model: 'model',
    output_tokens: 0,
    preview: 'preview',
    profile,
    source: 'desktop',
    started_at: 900,
    title: `Chat ${id}`,
    tool_call_count: 0
  }) as SessionInfo

beforeEach(() => {
  window.localStorage.clear()
  vi.useRealTimers()
})

describe('session list cache', () => {
  it('round-trips a bounded snapshot in the exact connection and profile scope', () => {
    const scope = { connectionId: 'connection-a', profile: 'profile-a' }

    writeSessionListSnapshot(scope, {
      cron: [row('cron', 'profile-a')],
      messaging: [row('message', 'profile-a')],
      messagingTruncated: true,
      profilesTruncated: { 'profile-a': true },
      profilesUsage: { 'profile-a': { cost_usd: 1.5, tokens: 42 } },
      recents: [row('recent', 'profile-a')]
    })

    expect(readSessionListSnapshot(scope)).toEqual({
      cron: [row('cron', 'profile-a')],
      messaging: [row('message', 'profile-a')],
      messagingTruncated: true,
      profilesTruncated: { 'profile-a': true },
      profilesUsage: { 'profile-a': { cost_usd: 1.5, tokens: 42 } },
      recents: [row('recent', 'profile-a')]
    })
    expect(readSessionListSnapshot({ connectionId: 'connection-a', profile: 'profile-b' })).toBeNull()
    expect(readSessionListSnapshot({ connectionId: 'connection-b', profile: 'profile-a' })).toBeNull()
  })

  it('rejects malformed or expired persisted data', () => {
    window.localStorage.setItem(SESSION_LIST_CACHE_STORAGE_KEY, '{broken')
    expect(readSessionListSnapshot({ connectionId: 'local', profile: 'default' })).toBeNull()

    const key = sessionListCacheScopeKey({ connectionId: 'local', profile: 'default' })
    window.localStorage.setItem(
      SESSION_LIST_CACHE_STORAGE_KEY,
      JSON.stringify({
        [key]: {
          cron: [],
          messaging: [],
          messagingTruncated: false,
          profilesTruncated: {},
          profilesUsage: {},
          recents: [row('stale')],
          savedAt: Date.now() - 15 * 24 * 60 * 60 * 1000
        }
      })
    )

    expect(readSessionListSnapshot({ connectionId: 'local', profile: 'default' })).toBeNull()
  })
})
