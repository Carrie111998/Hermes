import { describe, expect, it } from 'vitest'

import { isDelegateChildSession } from '@/lib/session-branch-tree'
import type { SessionInfo, SessionSearchResult } from '@/types/hermes'

import { mergeSearchResults, searchResultToSession } from './index'

// Server-side search is the one sidebar surface that can show a session the
// client never paged in. When `sessionByAnyId` has no entry for a hit, the
// sidebar synthesizes a SessionInfo from the raw search payload via
// `searchResultToSession` -- so any delegation metadata the synthesizer drops
// is metadata `isDelegateChildSession` can never see, and the background
// subagent child renders as an ordinary top-level conversation.
//
// These payloads are the production shape: exactly the keys
// `hermes_cli/web_routers/sessions.py::search_sessions` writes into a result
// when `get_session_rich_row` hydrates it.

const searchHit = (sessionId: string, overrides: Partial<SessionSearchResult> = {}): SessionSearchResult =>
  ({
    archived: false,
    ended_at: null,
    id: sessionId,
    input_tokens: 0,
    is_active: false,
    last_active: 100,
    lineage_root: sessionId,
    message_count: 2,
    model: 'hermes',
    output_tokens: 0,
    parent_session_id: null,
    preview: 'preview',
    role: 'user',
    session_id: sessionId,
    session_started: 100,
    snippet: 'matched text',
    source: 'cli',
    started_at: 100,
    title: sessionId,
    tool_call_count: 0,
    // The endpoint always emits both keys for a hydrated row.
    delegate_from: null,
    is_delegate_child: false,
    ...overrides
  }) as SessionSearchResult

describe('unloaded search hits carry delegation metadata', () => {
  it('excludes a delegated child while keeping top-level and user-branch hits', () => {
    const topLevel = searchHit('top-level')
    const userBranch = searchHit('user-branch', { parent_session_id: 'top-level' } as Partial<SessionSearchResult>)

    const delegated = searchHit('subagent', {
      delegate_from: 'top-level',
      is_delegate_child: true,
      parent_session_id: 'top-level'
    } as Partial<SessionSearchResult>)

    // The unloaded path: no loaded row exists, so the sidebar synthesizes one.
    const synthesized = [topLevel, userBranch, delegated].map(searchResultToSession)

    expect(synthesized.filter(s => !isDelegateChildSession(s)).map(s => s.id)).toEqual(['top-level', 'user-branch'])
  })

  it('propagates delegate_from onto the synthesized session', () => {
    const synthesized = searchResultToSession(
      searchHit('subagent', { delegate_from: 'top-level', is_delegate_child: true })
    )

    expect(synthesized.delegate_from).toBe('top-level')
    expect(synthesized.is_delegate_child).toBe(true)
    expect(isDelegateChildSession(synthesized)).toBe(true)
  })

  it('treats a hit from a backend predating the flag as not delegated', () => {
    // An older gateway omits both keys entirely; the sidebar must not start
    // hiding every server-only search hit against it.
    const legacy = { ...searchHit('legacy') } as Record<string, unknown>
    delete legacy.delegate_from
    delete legacy.is_delegate_child

    const synthesized: SessionInfo = searchResultToSession(legacy as unknown as SessionSearchResult)

    expect(synthesized.is_delegate_child).toBeUndefined()
    expect(isDelegateChildSession(synthesized)).toBe(false)
  })
})

// The mixed-freshness surface. A session that was paged in BEFORE it became a
// delegated child (or before the gateway grew the flag) carries stale/absent
// delegation metadata locally, while the server's FTS hit for the same id
// carries the current truth. Both can match one query at once, so the two
// sources have to be reconciled rather than raced by insertion order.

const loadedSession = (id: string, overrides: Partial<SessionInfo> = {}): SessionInfo =>
  ({
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: false,
    last_active: 100,
    message_count: 2,
    model: null,
    output_tokens: 0,
    preview: 'loaded preview',
    source: 'cli',
    started_at: 100,
    title: id,
    tool_call_count: 0,
    ...overrides
  }) as SessionInfo

/** A hit from a gateway predating the delegation flag: neither key present. */
const legacyHit = (sessionId: string, overrides: Partial<SessionSearchResult> = {}): SessionSearchResult => {
  const hit = { ...searchHit(sessionId, overrides) } as Record<string, unknown>
  delete hit.delegate_from
  delete hit.is_delegate_child

  return hit as unknown as SessionSearchResult
}

const byAnyId = (...sessions: SessionInfo[]): Map<string, SessionInfo> =>
  new Map(sessions.map(session => [session.id, session]))

const ids = (sessions: SessionInfo[]): string[] => sessions.map(session => session.id)

describe('mergeSearchResults reconciles local and server delegation metadata', () => {
  it('drops a preinserted local row when the server hit says it is a delegated child', () => {
    // The local row matched the query and went in first with NO delegation
    // metadata at all, so nothing about it looks like a child.
    const local = loadedSession('subagent')

    const results = mergeSearchResults(
      [local],
      [searchHit('subagent', { delegate_from: 'top-level', is_delegate_child: true })],
      byAnyId(local)
    )

    expect(ids(results)).toEqual([])
  })

  it('drops a loaded row whose stale metadata says false against a server child hit', () => {
    // Explicit stale falsehood, the worse case: the loaded row actively claims
    // it is not a child. Server presence must still win.
    const stale = loadedSession('subagent', { delegate_from: null, is_delegate_child: false })

    const results = mergeSearchResults(
      [stale],
      [searchHit('subagent', { delegate_from: 'top-level', is_delegate_child: true })],
      byAnyId(stale)
    )

    expect(ids(results)).toEqual([])
  })

  it('drops a stale-false loaded row reached through the fallback, not the local set', () => {
    // Same staleness, but the row never matched locally — it is only reachable
    // via sessionByAnyId. Both paths must classify identically.
    const stale = loadedSession('subagent', { delegate_from: null, is_delegate_child: false })

    const results = mergeSearchResults(
      [],
      [searchHit('subagent', { delegate_from: 'top-level', is_delegate_child: true })],
      byAnyId(stale)
    )

    expect(ids(results)).toEqual([])
  })

  it('keeps a loaded child excluded when an older server omits the fields', () => {
    // Nothing to merge, so the loaded row's own metadata decides — and it says
    // child. The local pass never inserts it and the server pass must not
    // resurrect it.
    const child = loadedSession('subagent', { delegate_from: 'top-level', is_delegate_child: true })

    const results = mergeSearchResults([child], [legacyHit('subagent')], byAnyId(child))

    expect(ids(results)).toEqual([])
  })

  it('keeps a loaded top-level row visible when an older server omits the fields', () => {
    // The backward-compatibility direction: an absent key must not be read as
    // "delegated", or every hit against a legacy gateway disappears.
    const topLevel = loadedSession('top-level')

    expect(ids(mergeSearchResults([topLevel], [legacyHit('top-level')], byAnyId(topLevel)))).toEqual(['top-level'])

    // Also via the fallback path, with no local match.
    expect(ids(mergeSearchResults([], [legacyHit('top-level')], byAnyId(topLevel)))).toEqual(['top-level'])
  })

  it('stays conservative when server and loaded metadata contradict each other', () => {
    // Server says "not a child" but the loaded row names a delegating parent.
    // Merging by presence leaves delegate_from intact, so the OR predicate in
    // isDelegateChildSession still reads child. Wholesale replacement would
    // have shown it.
    const loaded = loadedSession('subagent', { delegate_from: 'top-level' })

    const results = mergeSearchResults(
      [loaded],
      [searchHit('subagent', { delegate_from: undefined, is_delegate_child: false } as Partial<SessionSearchResult>)],
      byAnyId(loaded)
    )

    expect(ids(results)).toEqual([])
  })

  it('keeps top-level and user-branch results, from every source', () => {
    const topLevel = loadedSession('top-level')
    const userBranch = loadedSession('user-branch', { parent_session_id: 'top-level' })
    const unloaded = searchHit('server-only')

    const results = mergeSearchResults(
      [topLevel, userBranch],
      [
        searchHit('top-level'),
        searchHit('user-branch', { parent_session_id: 'top-level' } as Partial<SessionSearchResult>),
        unloaded
      ],
      byAnyId(topLevel, userBranch)
    )

    expect(ids(results)).toEqual(['top-level', 'user-branch', 'server-only'])
  })

  it('merges only delegation fields and keeps the local row otherwise intact', () => {
    // The server payload carries a different preview/title; reconciliation is
    // about delegation, not about replacing the displayed row.
    const local = loadedSession('top-level', { preview: 'loaded preview', title: 'loaded title' })

    const [row] = mergeSearchResults(
      [local],
      [searchHit('top-level', { delegate_from: null, is_delegate_child: false, snippet: 'server snippet' })],
      byAnyId(local)
    )

    expect(row.preview).toBe('loaded preview')
    expect(row.title).toBe('loaded title')
    expect(row.is_delegate_child).toBe(false)
  })

  it('preserves result order when a server hit re-sets an already-inserted row', () => {
    const first = loadedSession('first')
    const second = loadedSession('second')

    const results = mergeSearchResults(
      [first, second],
      [searchHit('first', { delegate_from: null, is_delegate_child: false })],
      byAnyId(first, second)
    )

    expect(ids(results)).toEqual(['first', 'second'])
  })
})
