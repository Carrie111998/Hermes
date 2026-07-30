import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $pinnedSessionIds, unpinSession } from '@/store/layout'
import { $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { toggleTilePin } from './session-tile'

const STORED = 'stored-tile'
const LINEAGE = 'lineage-root'

function row(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    cwd: null,
    ended_at: null,
    id: STORED,
    input_tokens: 0,
    is_active: true,
    last_active: 1,
    message_count: 1,
    model: null,
    output_tokens: 0,
    parent_session_id: null,
    preview: null,
    source: 'desktop',
    started_at: 1,
    title: null,
    tool_call_count: 0,
    ...overrides
  }
}

describe('toggleTilePin', () => {
  beforeEach(() => {
    $sessions.set([])
    $pinnedSessionIds.set([])
  })
  afterEach(() => {
    $sessions.set([])
    $pinnedSessionIds.set([])
  })

  it('pins a tile whose session is loaded, on its lineage root', () => {
    $sessions.set([row({ _lineage_root_id: LINEAGE })])

    toggleTilePin(STORED)

    expect($pinnedSessionIds.get()).toEqual([LINEAGE])
  })

  it('unpins on a second toggle', () => {
    $sessions.set([row()])

    toggleTilePin(STORED)
    expect($pinnedSessionIds.get()).toEqual([STORED])

    toggleTilePin(STORED)
    expect($pinnedSessionIds.get()).toEqual([])
  })

  it('still pins when the session row has not loaded yet', () => {
    toggleTilePin(STORED)

    expect($pinnedSessionIds.get()).toEqual([STORED])
    unpinSession(STORED)
  })

  it('leaves other pins alone', () => {
    $pinnedSessionIds.set(['other'])
    $sessions.set([row()])

    toggleTilePin(STORED)

    expect($pinnedSessionIds.get()).toEqual(['other', STORED])
  })
})
