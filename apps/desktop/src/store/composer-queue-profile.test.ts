/**
 * Layer 6c — durable queue ownership (v2). Profile-less (v1 / legacy) buckets
 * must be re-attributed by an aggregate-FIRST ownership probe that never guesses:
 * exactly one owner attaches the bucket to that profile; zero / ambiguous /
 * indeterminate leave it queued (never routed through the active gateway).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { getSession, listAllProfileSessions } from '@/hermes'
import {
  $queuedPromptsBySession,
  profileQueueKey,
  resolveLegacyQueueBucket,
  resolveUniqueSessionProfile
} from '@/store/composer-queue'

vi.mock('@/hermes', async importOriginal => {
  const actual = await importOriginal<typeof import('@/hermes')>()

  return {
    ...actual,
    getSession: vi.fn(async () => {
      throw new Error('not found')
    }),
    listAllProfileSessions: vi.fn(async () => ({ sessions: [] }))
  }
})

const listSessions = vi.mocked(listAllProfileSessions)
const getSessionMock = vi.mocked(getSession)

const entry = (id: string, text = 'queued') => ({ attachments: [], id, queuedAt: 1, text })

beforeEach(() => {
  vi.clearAllMocks()
  $queuedPromptsBySession.set({})
  listSessions.mockResolvedValue({ sessions: [] } as never)
})

describe('resolveUniqueSessionProfile (test 57)', () => {
  it('collapses a single owning profile to {one}', async () => {
    listSessions.mockResolvedValue({
      sessions: [
        { id: 'stored-1', profile: 'apollo' },
        { id: 'stored-1', profile: 'apollo' }
      ]
    } as never)

    expect(await resolveUniqueSessionProfile('stored-1')).toEqual({ kind: 'one', profile: 'apollo' })
    // Aggregate-first: one request, archived INCLUDED.
    expect(listSessions).toHaveBeenCalledWith(500, 0, 'include', 'recent', 'all')
  })

  it('matches on the lineage root id too', async () => {
    listSessions.mockResolvedValue({
      sessions: [{ _lineage_root_id: 'root-1', id: 'tip-1', profile: 'nova' }]
    } as never)

    expect(await resolveUniqueSessionProfile('root-1')).toEqual({ kind: 'one', profile: 'nova' })
  })

  it('reports ambiguous when two profiles own the id', async () => {
    listSessions.mockResolvedValue({
      sessions: [
        { id: 'stored-1', profile: 'apollo' },
        { id: 'stored-1', profile: 'nova' }
      ]
    } as never)

    expect(await resolveUniqueSessionProfile('stored-1')).toEqual({ kind: 'ambiguous' })
  })

  it('reports none when a successful aggregate has no match', async () => {
    listSessions.mockResolvedValue({ sessions: [{ id: 'other', profile: 'apollo' }] } as never)

    expect(await resolveUniqueSessionProfile('stored-1')).toEqual({ kind: 'none' })
  })

  it('reports indeterminate when the aggregate request fails (never probe blindly)', async () => {
    listSessions.mockRejectedValue(new Error('backend down'))

    expect(await resolveUniqueSessionProfile('stored-1')).toEqual({ kind: 'indeterminate' })
    expect(getSessionMock).not.toHaveBeenCalled()
  })
})

describe('resolveLegacyQueueBucket (tests 45, 46)', () => {
  it('attaches a single-owner bucket to its profile and stamps every entry (test 45)', async () => {
    listSessions.mockResolvedValue({ sessions: [{ id: 'stored-1', profile: 'apollo' }] } as never)
    $queuedPromptsBySession.set({ 'stored-1': [entry('a'), entry('b')] })

    const ownership = await resolveLegacyQueueBucket('stored-1')

    expect(ownership).toEqual({ kind: 'one', profile: 'apollo' })

    const state = $queuedPromptsBySession.get()

    // Moved off the bare key onto the composite profile bucket, profile stamped.
    expect(state['stored-1']).toBeUndefined()
    const moved = state[profileQueueKey('apollo', 'stored-1')]

    expect(moved).toHaveLength(2)
    expect(moved.every(e => e.profile === 'apollo')).toBe(true)
    expect(moved.map(e => e.id)).toEqual(['a', 'b'])
  })

  it('leaves an ambiguous bucket queued and unmodified (test 46)', async () => {
    listSessions.mockResolvedValue({
      sessions: [
        { id: 'stored-1', profile: 'apollo' },
        { id: 'stored-1', profile: 'nova' }
      ]
    } as never)
    $queuedPromptsBySession.set({ 'stored-1': [entry('a')] })

    const ownership = await resolveLegacyQueueBucket('stored-1')

    expect(ownership).toEqual({ kind: 'ambiguous' })

    const state = $queuedPromptsBySession.get()

    // Untouched: still under the bare key, no profile stamped.
    expect(state['stored-1']).toHaveLength(1)
    expect(state['stored-1'][0].profile).toBeUndefined()
    expect(state[profileQueueKey('apollo', 'stored-1')]).toBeUndefined()
  })

  it('leaves an indeterminate bucket queued (never guess)', async () => {
    listSessions.mockRejectedValue(new Error('backend down'))
    $queuedPromptsBySession.set({ 'stored-1': [entry('a')] })

    const ownership = await resolveLegacyQueueBucket('stored-1')

    expect(ownership).toEqual({ kind: 'indeterminate' })
    expect($queuedPromptsBySession.get()['stored-1']).toHaveLength(1)
  })
})
