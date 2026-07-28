import { describe, expect, it } from 'vitest'

import type { SessionInfo, SessionSearchResult } from '@/hermes'
import { sessionIdentityKey } from '@/lib/session-identity'

import { mergeSidebarSearchResults } from './search-results'

const session = (id: string, profile: string, title: string): SessionInfo => ({
  archived: false,
  cwd: null,
  ended_at: null,
  id,
  input_tokens: 0,
  is_active: false,
  last_active: 1,
  message_count: 1,
  model: null,
  output_tokens: 0,
  preview: null,
  profile,
  source: null,
  started_at: 1,
  title,
  tool_call_count: 0
})

const match = (sessionId: string): SessionSearchResult => ({
  lineage_root: sessionId,
  model: null,
  role: 'user',
  session_id: sessionId,
  session_started: 1,
  snippet: 'matched',
  source: null
})

describe('mergeSidebarSearchResults', () => {
  it('keeps colliding client and server ids under their owning profiles', () => {
    const personal = session('shared-id', 'default', 'Personal')
    const work = session('shared-id', 'work', 'Work')

    const loaded = new Map([
      [sessionIdentityKey(personal.id, personal.profile), personal],
      [sessionIdentityKey(work.id, work.profile), work]
    ])

    const result = mergeSidebarSearchResults([personal], [match('shared-id')], loaded, 'work')

    expect(result).toEqual([personal, work])
  })

  it('assigns the searched profile to an unloaded result', () => {
    const [result] = mergeSidebarSearchResults([], [match('unloaded')], new Map(), 'work')

    expect(result).toMatchObject({ id: 'unloaded', profile: 'work' })
  })
})
