import { beforeEach, describe, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $activeSessionId, $selectedStoredSessionId, $sessions, $unreadFinishedSessionIds } from '@/store/session'
import {
  $sessionStates,
  $sessionTiles,
  closeSessionTile,
  discardSessionTile,
  publishSessionState
} from '@/store/session-states'

/**
 * The closed-tile leak: gateway events keep publishing for sessions whose
 * surface is gone, and every parked transcript taxes every later publish (map
 * spread + the status projections run per entry per message delta). A settled
 * state nothing references must release its transcript; lightweight status
 * stays so sidebar projections remain available.
 */

const state = (storedId: string, patch: Partial<ReturnType<typeof createClientSessionState>> = {}) => ({
  ...createClientSessionState(storedId),
  messages: [{ id: `${storedId}-m`, role: 'assistant' as const, parts: [{ type: 'text' as const, text: 'hi' }] }],
  ...patch
})

beforeEach(() => {
  $sessionStates.set({})
  $sessionTiles.set([])
  $sessions.set([])
  $activeSessionId.set(null)
  $selectedStoredSessionId.set(null)
  $unreadFinishedSessionIds.set([])
})

describe('publish-time eviction', () => {
  it('releases an unreferenced settled transcript while keeping status and its unread dot', () => {
    publishSessionState('rt-1', state('stored-1', { busy: true }))
    expect($sessionStates.get()['rt-1']).toBeDefined()

    publishSessionState('rt-1', state('stored-1', { busy: false }))

    expect($sessionStates.get()['rt-1']?.messages).toEqual([])
    expect($sessionStates.get()['rt-1']).toMatchObject({ storedSessionId: 'stored-1', busy: false })
    // The settle transition still fired: the sidebar's unread marker landed.
    expect($unreadFinishedSessionIds.get()).toContain('stored-1')
  })

  it('keeps a busy session with no surface — its background turn feeds the sidebar dot', () => {
    publishSessionState('rt-1', state('stored-1', { busy: true }))
    publishSessionState('rt-1', state('stored-1', { busy: true, awaitingResponse: true }))

    expect($sessionStates.get()['rt-1']).toBeDefined()
  })

  it('keeps a needsInput session with no surface — the attention dot reads it', () => {
    publishSessionState('rt-1', state('stored-1', { busy: true }))
    publishSessionState('rt-1', state('stored-1', { busy: false, needsInput: true }))

    expect($sessionStates.get()['rt-1']).toBeDefined()
  })

  it('keeps a settled session an open tile references, by runtime or stored id', () => {
    $sessionTiles.set([{ runtimeId: 'rt-1', storedSessionId: 'stored-1' }])
    publishSessionState('rt-1', state('stored-1', { busy: true }))
    publishSessionState('rt-1', state('stored-1', { busy: false }))
    expect($sessionStates.get()['rt-1']).toBeDefined()

    // Mid-resume a tile holds only the stored id (runtime binding not patched
    // in yet) — that reference must count too.
    $sessionTiles.set([{ storedSessionId: 'stored-2' }])
    publishSessionState('rt-2', state('stored-2', { busy: true }))
    publishSessionState('rt-2', state('stored-2', { busy: false }))
    expect($sessionStates.get()['rt-2']).toBeDefined()
  })

  it("keeps the primary view's settled session", () => {
    $activeSessionId.set('rt-1')
    publishSessionState('rt-1', state('stored-1', { busy: true }))
    publishSessionState('rt-1', state('stored-1', { busy: false }))

    expect($sessionStates.get()['rt-1']).toBeDefined()
  })

  it('always lands a FIRST publish — resume can publish before the surface points at the runtime', () => {
    publishSessionState('rt-1', state('stored-1', { busy: false }))

    expect($sessionStates.get()['rt-1']).toBeDefined()
  })
})

describe('closeSessionTile eviction', () => {
  it("drops a settled session's state on close — no later publish may come", () => {
    $sessionTiles.set([{ runtimeId: 'rt-1', storedSessionId: 'stored-1' }])
    publishSessionState('rt-1', state('stored-1', { busy: false }))

    closeSessionTile('stored-1')

    expect($sessionStates.get()['rt-1']).toBeUndefined()
  })

  it("keeps a busy session's state on close — the background turn is still running", () => {
    $sessionTiles.set([{ runtimeId: 'rt-1', storedSessionId: 'stored-1' }])
    publishSessionState('rt-1', state('stored-1', { busy: true }))

    closeSessionTile('stored-1')

    expect($sessionStates.get()['rt-1']).toBeDefined()

    // ... and its settle publish releases only the heavy transcript.
    publishSessionState('rt-1', state('stored-1', { busy: false }))
    expect($sessionStates.get()['rt-1']?.messages).toEqual([])
    expect($sessionStates.get()['rt-1']).toMatchObject({ storedSessionId: 'stored-1', busy: false })
  })

  // Two DIFFERENT bot workspaces can persist a tile with the SAME
  // storedSessionId (a restored backup, a copied state.db) — the shared
  // __bots_workspace__ tile bucket is never filtered by profile, so both
  // twins are simultaneously present in $sessionTiles (#92454-class).
  it("closing one twin by workspaceOwnerKey never touches its twin's state or tile", () => {
    $sessionTiles.set([
      { runtimeId: 'rt-a', storedSessionId: 'twin', workspaceMode: 'bots', workspaceOwnerKey: 'bot:a' },
      { runtimeId: 'rt-b', storedSessionId: 'twin', workspaceMode: 'bots', workspaceOwnerKey: 'bot:b' }
    ])
    publishSessionState('rt-a', state('twin', { busy: false }))
    publishSessionState('rt-b', state('twin', { busy: false }))

    closeSessionTile('twin', 'bot:a')

    // The pre-fix bare-id `.find()`/`.filter()` could resolve to either
    // twin's tile and dropped every bare-id match at once — closing one
    // twin silently evicted/removed the unrelated twin's live cached
    // session too. workspaceOwnerKey-scoped removal must never touch it.
    expect($sessionStates.get()['rt-b']).toBeDefined()

    const remaining = $sessionTiles.get()

    expect(remaining).toHaveLength(1)
    expect(remaining[0]).toMatchObject({ runtimeId: 'rt-b', workspaceOwnerKey: 'bot:b' })

    // Note: rt-a's own state isn't dropped by this close either, but for an
    // orthogonal, pre-existing reason unrelated to this fix — evictable()'s
    // runtimeReferenced() still matches twin B's tile by bare storedSessionId
    // (it isn't workspaceOwnerKey-aware), so it "protects" rt-a from
    // eviction as long as any OTHER tile shares that stored id, even one
    // belonging to a different bot workspace. That's a distinct,
    // lower-severity gap (retains a state that could be freed, never drops
    // the wrong one) left for a follow-up — see the PR's Scope note.
  })
})

describe('discardSessionTile eviction', () => {
  it("drops a session's state on discard", () => {
    $sessionTiles.set([{ runtimeId: 'rt-1', storedSessionId: 'stored-1' }])
    publishSessionState('rt-1', state('stored-1', { busy: false }))

    discardSessionTile('stored-1')

    expect($sessionStates.get()['rt-1']).toBeUndefined()
    expect($sessionTiles.get()).toHaveLength(0)
  })

  it("discarding one twin by workspaceOwnerKey drops only that twin's state, keeping its twin's tile and session live", () => {
    $sessionTiles.set([
      { runtimeId: 'rt-a', storedSessionId: 'twin', workspaceMode: 'bots', workspaceOwnerKey: 'bot:a' },
      { runtimeId: 'rt-b', storedSessionId: 'twin', workspaceMode: 'bots', workspaceOwnerKey: 'bot:b' }
    ])
    publishSessionState('rt-a', state('twin', { busy: false }))
    publishSessionState('rt-b', state('twin', { busy: false }))

    discardSessionTile('twin', 'bot:a')

    expect($sessionStates.get()['rt-a']).toBeUndefined()
    expect($sessionStates.get()['rt-b']).toBeDefined()

    const remaining = $sessionTiles.get()

    expect(remaining).toHaveLength(1)
    expect(remaining[0]).toMatchObject({ runtimeId: 'rt-b', workspaceOwnerKey: 'bot:b' })
  })
})
