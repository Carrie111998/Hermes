import { beforeEach, describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/hermes'

import { $archivedSessions, dropArchivedSession, restoreArchivedSession } from './sidebar-archive'

const row = (id: string): SessionInfo => ({ id }) as SessionInfo

beforeEach(() => {
  $archivedSessions.set([])
})

describe('dropArchivedSession', () => {
  it('evicts the row and reports the index it held', () => {
    $archivedSessions.set([row('a'), row('b'), row('c')])

    expect(dropArchivedSession('b')).toMatchObject({ index: 1, session: { id: 'b' } })
    expect($archivedSessions.get().map(s => s.id)).toEqual(['a', 'c'])
  })

  it('returns null for a row the archived view never held', () => {
    $archivedSessions.set([row('a')])

    expect(dropArchivedSession('stranger')).toBeNull()
    expect($archivedSessions.get().map(s => s.id)).toEqual(['a'])
  })
})

describe('restoreArchivedSession', () => {
  it('puts a rolled-back row back where it was', () => {
    $archivedSessions.set([row('a'), row('b'), row('c')])
    restoreArchivedSession(dropArchivedSession('b'))

    expect($archivedSessions.get().map(s => s.id)).toEqual(['a', 'b', 'c'])
  })

  it('does not resurrect a sibling deleted while its RPC was in flight', () => {
    // Same contract as restoreSessionPins: a whole-snapshot restore would undo
    // a parallel row's eviction as well as its own.
    $archivedSessions.set([row('keep'), row('fails'), row('succeeds')])

    const failing = dropArchivedSession('fails')

    dropArchivedSession('succeeds')
    restoreArchivedSession(failing)

    expect($archivedSessions.get().map(s => s.id)).toEqual(['keep', 'fails'])
  })

  it('is a no-op for null and for a row already back in the list', () => {
    $archivedSessions.set([row('a')])
    restoreArchivedSession(null)
    restoreArchivedSession({ index: 0, session: row('a') })

    expect($archivedSessions.get().map(s => s.id)).toEqual(['a'])
  })
})
