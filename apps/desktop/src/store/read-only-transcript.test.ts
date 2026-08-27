import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $connectionsRegistry } from './connections'
import { $profiles } from './profile'
import {
  $readOnlyStoredTranscripts,
  clearStoredTranscriptReadOnly,
  isReadOnlyRuntimeId,
  isStoredTranscriptReadOnly,
  markStoredTranscriptReadOnly,
  readOnlyRuntimeIdFor,
  resumeWithStoredTranscriptFallback
} from './read-only-transcript'
import { assertSessionOwnerResolved } from './session-owner-resolution'

const registry = (...ids: string[]) =>
  ({
    connections: ids.map(id => ({ id })),
    lastUsed: ids[0] ?? null,
    launchMode: 'primary',
    primary: ids[0] ?? null
  }) as never

beforeEach(() => {
  $connectionsRegistry.set(null)
  $profiles.set([])
  $readOnlyStoredTranscripts.set(new Set())
})

afterEach(() => {
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('read-only stored-transcript resume (#94724 no-owner recovery)', () => {
  it('opens the stored transcript read-only when the owner fails closed on a 2-connection topology', async () => {
    // The reporter's shape: registry topology with two registered
    // connections, a legacy NULL-owner row — the production gate throws
    // SessionOwnerResolutionError for session.resume (Error B).
    $connectionsRegistry.set(registry('gw-a', 'gw-b'))
    $profiles.set([{ name: 'default' }, { name: 'researcher' }] as never)

    const gatewayDispatch = vi.fn()
    const transcript = { messages: [{ content: 'intact history', role: 'user' }], session_id: 'legacy-1' }

    const outcome = await resumeWithStoredTranscriptFallback(
      'legacy-1',
      async () => {
        // The REAL fail-closed gate, not a re-implementation: an unknown
        // owner under registry topology throws before any dispatch.
        assertSessionOwnerResolved(undefined, { method: 'session.resume', sessionId: 'legacy-1' })
        gatewayDispatch()

        return { session_id: 'runtime-1' }
      },
      async () => transcript
    )

    expect(outcome.mode).toBe('read-only')

    if (outcome.mode === 'read-only') {
      expect(outcome.transcript).toBe(transcript)
      expect(outcome.error.name).toBe('SessionOwnerResolutionError')
    }

    // The whole point of the recovery path: NO gateway routing happened.
    expect(gatewayDispatch).not.toHaveBeenCalled()
    expect(isStoredTranscriptReadOnly('legacy-1')).toBe(true)
  })

  it('stays live (and clears the latch) when the owner resolves', async () => {
    $connectionsRegistry.set(registry('gw-a', 'gw-b'))
    $profiles.set([{ name: 'default' }] as never)
    markStoredTranscriptReadOnly('legacy-2')

    const outcome = await resumeWithStoredTranscriptFallback(
      'legacy-2',
      async () => {
        // Backfilled row: a bare profile is a routable owner in every topology.
        assertSessionOwnerResolved('default', { method: 'session.resume', sessionId: 'legacy-2' })

        return { session_id: 'runtime-2' }
      },
      async () => {
        throw new Error('stored read must not run on the live path')
      }
    )

    expect(outcome.mode).toBe('live')
    expect(isStoredTranscriptReadOnly('legacy-2')).toBe(false)
  })

  it('rethrows non-owner-resolution errors without marking read-only', async () => {
    const boom = new Error('backend exploded')

    await expect(
      resumeWithStoredTranscriptFallback(
        'legacy-3',
        async () => {
          throw boom
        },
        async () => ({ messages: [] })
      )
    ).rejects.toBe(boom)

    expect(isStoredTranscriptReadOnly('legacy-3')).toBe(false)
  })

  it('records fallback state only for the exact owner supplied to the resume wrapper', async () => {
    $connectionsRegistry.set(registry('source-a', 'source-b'))
    $profiles.set([{ name: 'worker' }] as never)

    const ownerA = { connectionId: 'source-a', profile: 'worker' }
    const ownerB = { connectionId: 'source-b', profile: 'worker' }

    const outcome = await resumeWithStoredTranscriptFallback(
      'shared-wrapper',
      async () => {
        assertSessionOwnerResolved(undefined, { method: 'session.resume', sessionId: 'shared-wrapper' })

        return { session_id: 'never' }
      },
      async () => ({ messages: [] }),
      ownerA
    )

    expect(outcome.mode).toBe('read-only')
    expect(isStoredTranscriptReadOnly('shared-wrapper', ownerA)).toBe(true)
    expect(isStoredTranscriptReadOnly('shared-wrapper', ownerB)).toBe(false)
    expect(isStoredTranscriptReadOnly('shared-wrapper')).toBe(false)
  })

  it('rethrows the ORIGINAL owner error when even the stored read fails', async () => {
    $connectionsRegistry.set(registry('gw-a', 'gw-b'))
    $profiles.set([{ name: 'default' }, { name: 'researcher' }] as never)

    await expect(
      resumeWithStoredTranscriptFallback(
        'legacy-4',
        async () => {
          assertSessionOwnerResolved(null, { method: 'session.resume', sessionId: 'legacy-4' })

          return {}
        },
        async () => {
          throw new Error('404 stored row missing')
        }
      )
    ).rejects.toMatchObject({ name: 'SessionOwnerResolutionError' })

    expect(isStoredTranscriptReadOnly('legacy-4')).toBe(false)
  })

  it('mints collision-proof synthetic runtime ids and round-trips the latch', () => {
    const id = readOnlyRuntimeIdFor('stored-9')

    expect(isReadOnlyRuntimeId(id)).toBe(true)
    expect(isReadOnlyRuntimeId('stored-9')).toBe(false)
    expect(isReadOnlyRuntimeId(null)).toBe(false)

    markStoredTranscriptReadOnly('stored-9')
    expect(isStoredTranscriptReadOnly('stored-9')).toBe(true)
    clearStoredTranscriptReadOnly('stored-9')
    expect(isStoredTranscriptReadOnly('stored-9')).toBe(false)
  })

  it('isolates read-only latches and synthetic runtimes for duplicate exact owners', () => {
    const ownerA = { connectionId: 'source-a', profile: 'worker' }
    const ownerB = { connectionId: 'source-b', profile: 'worker' }

    markStoredTranscriptReadOnly('shared', ownerA)

    expect(isStoredTranscriptReadOnly('shared', ownerA)).toBe(true)
    expect(isStoredTranscriptReadOnly('shared', ownerB)).toBe(false)
    expect(readOnlyRuntimeIdFor('shared', ownerA)).not.toBe(readOnlyRuntimeIdFor('shared', ownerB))

    clearStoredTranscriptReadOnly('shared', ownerB)
    expect(isStoredTranscriptReadOnly('shared', ownerA)).toBe(true)

    clearStoredTranscriptReadOnly('shared', ownerA)
    expect(isStoredTranscriptReadOnly('shared', ownerA)).toBe(false)
  })
})
