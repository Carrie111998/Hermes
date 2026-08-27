import { beforeEach, describe, expect, it } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import type { ChatMessage } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $sessionStates, $sessionTiles, releaseSessionTranscript } from '@/store/session-states'

import { SessionRuntimeIndex, SessionStateCache } from './session-state-cache'

function transcript(id: string, text = id): ChatMessage[] {
  return [
    { id: `${id}-user`, role: 'user', parts: [{ type: 'text', text }] },
    { id: `${id}-assistant`, role: 'assistant', parts: [{ type: 'text', text: `reply ${text}` }] }
  ]
}

function settled(storedSessionId: string, text = storedSessionId): ClientSessionState {
  return { ...createClientSessionState(storedSessionId), messages: transcript(storedSessionId, text) }
}

describe('SessionRuntimeIndex', () => {
  const ownerA = { connectionId: 'connection-a', profile: 'default' }
  const ownerB = { connectionId: 'connection-b', profile: 'default' }

  it('exposes public stored ids through every Map iteration surface', () => {
    const index = new SessionRuntimeIndex()
    const visited: Array<[string, string, Map<string, string>]> = []

    index.setOwned('stored-a', 'runtime-a', ownerA)
    index.setOwned('stored-b', 'runtime-b', ownerB)
    index.forEach((runtimeId, storedSessionId, map) => visited.push([storedSessionId, runtimeId, map]))

    expect(index.size).toBe(2)
    expect([...index.keys()]).toEqual(['stored-a', 'stored-b'])
    expect([...index.values()]).toEqual(['runtime-a', 'runtime-b'])
    expect([...index.entries()]).toEqual([
      ['stored-a', 'runtime-a'],
      ['stored-b', 'runtime-b']
    ])
    expect([...index]).toEqual([...index.entries()])
    expect(visited).toEqual([
      ['stored-a', 'runtime-a', index],
      ['stored-b', 'runtime-b', index]
    ])
  })

  it('keeps duplicate public ids ambiguous while owner-qualified lookup and deletion stay exact', () => {
    const index = new SessionRuntimeIndex()

    index.setOwned('stored-shared', 'runtime-a', ownerA)
    index.setOwned('stored-shared', 'runtime-b', ownerB)

    expect(index.size).toBe(2)
    expect([...index.keys()]).toEqual(['stored-shared', 'stored-shared'])
    expect(index.get('stored-shared')).toBeUndefined()
    expect(index.delete('stored-shared')).toBe(false)
    expect(index.getOwned('stored-shared', ownerA)).toBe('runtime-a')
    expect(index.getOwned('stored-shared', ownerB)).toBe('runtime-b')

    expect(index.deleteOwned('stored-shared', ownerA)).toBe(true)
    expect(index.get('stored-shared')).toBe('runtime-b')
    expect(index.delete('stored-shared')).toBe(true)
    expect(index.size).toBe(0)

    index.setOwned('stored-shared', 'runtime-a', ownerA)
    index.setOwned('stored-shared', 'runtime-b', ownerB)
    expect(index.deleteAll('stored-shared')).toBe(2)
    expect(index.size).toBe(0)
  })

  it('counts direct and owner-qualified entries together for unqualified access', () => {
    const index = new SessionRuntimeIndex()

    index.set('stored-shared', 'runtime-primary')
    index.setOwned('stored-shared', 'runtime-a', ownerA)

    expect(index.size).toBe(2)
    expect([...index.keys()]).toEqual(['stored-shared', 'stored-shared'])
    expect(index.get('stored-shared')).toBeUndefined()
    expect(index.delete('stored-shared')).toBe(false)
    expect(index.size).toBe(2)
    expect(index.getOwned('stored-shared', ownerA)).toBe('runtime-a')

    expect(index.deleteAll('stored-shared')).toBe(2)
    expect(index.size).toBe(0)
  })

  it('allows unqualified access when exactly one direct or owner-qualified entry exists', () => {
    const index = new SessionRuntimeIndex()

    index.set('stored-direct', 'runtime-direct')
    expect(index.get('stored-direct')).toBe('runtime-direct')
    expect(index.delete('stored-direct')).toBe(true)

    index.setOwned('stored-owned', 'runtime-owned', ownerA)
    expect(index.get('stored-owned')).toBe('runtime-owned')
    expect(index.delete('stored-owned')).toBe(true)
    expect(index.size).toBe(0)
  })
})

describe('SessionStateCache', () => {
  beforeEach(() => {
    $sessionStates.set({})
    $sessionTiles.set([])
  })

  it('keeps identical runtime ids independent across connection owners', () => {
    const cache = new SessionStateCache({ isReferenced: () => false, onEvict: () => undefined })
    const ownerA = { ...settled('stored-a'), connectionId: 'connection-a', profile: 'default' }
    const ownerB = { ...settled('stored-b'), connectionId: 'connection-b', profile: 'default' }

    cache.set('runtime-shared', ownerA)
    cache.set('runtime-shared', ownerB)

    expect(cache.size).toBe(2)
    expect(cache.getOwned('runtime-shared', ownerA)).toBe(ownerA)
    expect(cache.getOwned('runtime-shared', ownerB)).toBe(ownerB)
    expect(cache.get('runtime-shared')).toBeUndefined()
  })

  it('bounds warm settled transcripts by LRU count and cleans ownership atomically', () => {
    const owners = new Map<string, string>()
    const evicted: string[] = []

    const cache = new SessionStateCache(
      {
        isReferenced: () => false,
        onEvict: (runtimeId, state) => {
          if (state.storedSessionId && owners.get(state.storedSessionId) === runtimeId) {
            owners.delete(state.storedSessionId)
          }

          evicted.push(runtimeId)
        }
      },
      { maxBytes: Number.POSITIVE_INFINITY, maxCount: 2 }
    )

    for (const id of ['a', 'b', 'c']) {
      owners.set(`stored-${id}`, `runtime-${id}`)
      cache.set(`runtime-${id}`, settled(`stored-${id}`))
    }

    // A read makes A warmer than B, so B is the oldest when pruning.
    cache.get('runtime-a')
    cache.prune()

    expect([...cache.keys()].sort()).toEqual(['runtime-a', 'runtime-c'])
    expect(evicted).toEqual(['runtime-b'])
    expect(owners.has('stored-b')).toBe(false)

    // A recycled reverse mapping is not owned by the evicted runtime and must
    // survive cleanup.
    owners.set('stored-a', 'runtime-new-owner')
    cache.set('runtime-d', settled('stored-d'))
    owners.set('stored-d', 'runtime-d')
    cache.prune()
    expect(owners.get('stored-a')).toBe('runtime-new-owner')
  })

  it('uses transcript bytes as well as count', () => {
    const evicted: string[] = []

    const cache = new SessionStateCache(
      { isReferenced: () => false, onEvict: runtimeId => evicted.push(runtimeId) },
      { maxBytes: 600, maxCount: 10 }
    )

    cache.set('small', settled('small', 'x'))
    cache.set('large', settled('large', 'x'.repeat(500)))
    cache.prune()

    expect(evicted).toEqual(['small', 'large'])
    expect(cache.size).toBe(0)
  })

  it.each([
    ['active', (state: ClientSessionState) => state, true],
    ['tiled', (state: ClientSessionState) => state, true],
    ['busy', (state: ClientSessionState) => ({ ...state, busy: true }), false],
    ['awaiting', (state: ClientSessionState) => ({ ...state, awaitingResponse: true }), false],
    ['needs input', (state: ClientSessionState) => ({ ...state, needsInput: true }), false]
  ])('never evicts %s transcripts', (_label, decorate, referenced) => {
    const protectedState = decorate(settled('protected'))

    const cache = new SessionStateCache(
      {
        isReferenced: runtimeId => referenced && runtimeId === 'protected',
        onEvict: () => undefined
      },
      { maxBytes: 0, maxCount: 0 }
    )

    cache.set('protected', protectedState)
    cache.prune()

    expect(cache.get('protected')).toBe(protectedState)
  })

  it('keeps unsaved drafts and pending messages out of the eviction pool', () => {
    const draft = { ...createClientSessionState(null), messages: transcript('draft') }
    const pending = settled('pending')
    pending.messages = [{ id: 'pending-assistant', role: 'assistant', parts: [], pending: true }]

    const cache = new SessionStateCache(
      { isReferenced: () => false, onEvict: () => undefined },
      { maxBytes: 0, maxCount: 0 }
    )

    cache.set('draft', draft)
    cache.set('pending', pending)
    cache.prune()

    expect(cache.has('draft')).toBe(true)
    expect(cache.has('pending')).toBe(true)
  })

  it('retains lightweight status while releasing an evicted transcript', () => {
    const state = { ...settled('stored'), needsInput: false }
    $sessionStates.set({ runtime: state })

    releaseSessionTranscript('runtime')

    expect($sessionStates.get().runtime).toMatchObject({ storedSessionId: 'stored', busy: false, needsInput: false })
    expect($sessionStates.get().runtime.messages).toEqual([])
  })

  describe('authoritative liveness probe (#95189)', () => {
    function cacheWithAuthority(evicted: string[]): SessionStateCache {
      return new SessionStateCache(
        {
          isReferenced: () => false,
          onEvict: runtimeId => evicted.push(runtimeId),
          isAuthoritativelyActive: runtimeId => {
            const live = $sessionStates.get()[runtimeId]

            return Boolean(live && (live.busy || live.awaitingResponse))
          }
        },
        { maxBytes: 0, maxCount: 0 }
      )
    }

    it.each([
      ['busy', (state: ClientSessionState) => ({ ...state, busy: true })],
      ['awaiting', (state: ClientSessionState) => ({ ...state, awaitingResponse: true })]
    ])('evicts an orphaned %s transcript once the authoritative record settles', (_label, decorate) => {
      const evicted: string[] = []
      const cache = cacheWithAuthority(evicted)
      const orphaned = decorate(settled('orphaned'))

      // Mid-turn the snapshot and the authoritative record agree: protection
      // must hold exactly as it does without the probe.
      $sessionStates.set({ orphaned })
      cache.set('orphaned', orphaned)
      cache.prune()
      expect(cache.get('orphaned')).toBe(orphaned)

      // The minting connection dies mid-turn. Reconnect reconciliation
      // settles the authoritative record, but the respawned backend re-mints
      // runtime ids — no event will ever reach this snapshot again, so its
      // frozen in-flight flags must stop pinning the transcript.
      $sessionStates.set({ orphaned: { ...orphaned, busy: false, awaitingResponse: false } })
      cache.prune()

      expect(cache.has('orphaned')).toBe(false)
      expect(evicted).toEqual(['orphaned'])
    })

    it('evicts an in-flight transcript whose authoritative record was dropped entirely', () => {
      const evicted: string[] = []
      const cache = cacheWithAuthority(evicted)
      const working = { ...settled('working'), busy: true }

      $sessionStates.set({ working })
      cache.set('working', working)
      cache.prune()
      expect(cache.has('working')).toBe(true)

      // A soft gateway-mode apply wipes every authoritative state; surviving
      // snapshots describe dead runtimes (#95189 reconnect churn).
      $sessionStates.set({})
      cache.prune()

      expect(cache.has('working')).toBe(false)
      expect(evicted).toEqual(['working'])
    })

    it('keeps an in-flight transcript pinned while the authoritative store still claims work', () => {
      const evicted: string[] = []
      const cache = cacheWithAuthority(evicted)
      const working = { ...settled('working'), busy: true }

      $sessionStates.set({ working })
      cache.set('working', working)
      cache.prune()

      expect(cache.get('working')).toBe(working)
      expect(evicted).toEqual([])
    })

    it.each([
      ['busy', (state: ClientSessionState) => ({ ...state, busy: true })],
      ['awaiting', (state: ClientSessionState) => ({ ...state, awaitingResponse: true })]
    ])('still never evicts %s transcripts when no authority probe is wired', (_label, decorate) => {
      // Legacy construction: without the probe there is no way to tell a live
      // turn from an orphaned snapshot, so the flags keep blocking eviction.
      const working = decorate(settled('working'))
      $sessionStates.set({ working: { ...working, busy: false, awaitingResponse: false } })

      const cache = new SessionStateCache(
        { isReferenced: () => false, onEvict: () => undefined },
        { maxBytes: 0, maxCount: 0 }
      )

      cache.set('working', working)
      cache.prune()

      expect(cache.get('working')).toBe(working)
    })
  })
})
