import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import { flattenSessionsWithBranches, isDelegateChildSession } from './session-branch-tree'

const session = (id: string, overrides: Partial<SessionInfo> = {}): SessionInfo =>
  ({
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: false,
    last_active: 0,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    source: 'cli',
    started_at: 0,
    title: id,
    tool_call_count: 0,
    ...overrides
  }) as SessionInfo

describe('flattenSessionsWithBranches', () => {
  it('nests branch rows under their parent with tree stems', () => {
    const parent = session('parent', { last_active: 20 })
    const branchA = session('branch-a', { last_active: 15, parent_session_id: 'parent' })
    const branchB = session('branch-b', { last_active: 10, parent_session_id: 'parent' })

    expect(flattenSessionsWithBranches([parent, branchA, branchB])).toEqual([
      { session: parent },
      { branchStem: '├─ ', session: branchA },
      { branchStem: '└─ ', session: branchB }
    ])
  })

  it('follows a compressed parent via lineage root id', () => {
    const tip = session('tip', { _lineage_root_id: 'root', last_active: 30 })
    const branch = session('branch', { parent_session_id: 'root', last_active: 10 })

    expect(flattenSessionsWithBranches([tip, branch])).toEqual([
      { session: tip },
      { branchStem: '└─ ', session: branch }
    ])
  })

  it('keeps orphan branches at the top level when the parent is missing', () => {
    const branch = session('branch', { parent_session_id: 'missing' })

    expect(flattenSessionsWithBranches([branch])).toEqual([{ session: branch }])
  })

  it('re-sorts roots by group recency by default (pinned-style jumps without preserveOrder)', () => {
    // Stale important chat first in the caller's array; a recently-active
    // background task second. Default path must lift the fresher root — that
    // is what was scrambling the Pinned section before preserveOrder.
    const important = session('important', { last_active: 10 })
    const background = session('background', { last_active: 99 })

    expect(flattenSessionsWithBranches([important, background]).map(e => e.session.id)).toEqual([
      'background',
      'important'
    ])
  })

  it("preserveOrder keeps the caller's root order even when activity is newer lower down", () => {
    const important = session('important', { last_active: 10 })
    const background = session('background', { last_active: 99 })
    const branch = session('branch', { last_active: 50, parent_session_id: 'important' })

    expect(
      flattenSessionsWithBranches([important, background, branch], { preserveOrder: true }).map(e => ({
        id: e.session.id,
        stem: e.branchStem
      }))
    ).toEqual([
      { id: 'important', stem: undefined },
      { id: 'branch', stem: '└─ ' },
      { id: 'background', stem: undefined }
    ])
  })

  it('filters out delegated subagent children while preserving user branches and top-level sessions', () => {
    const parent = session('parent', { last_active: 20 })

    const userBranch = session('user-branch', {
      last_active: 15,
      parent_session_id: 'parent',
      is_delegate_child: false
    })

    const subagentChild = session('subagent-child', {
      last_active: 18,
      parent_session_id: 'parent',
      delegate_from: 'parent',
      is_delegate_child: true
    })

    const kanbanTopLevel = session('kanban-task', { last_active: 25, source: 'kanban', is_delegate_child: false })
    const toolTopLevel = session('tool-session', { last_active: 5, source: 'tool', is_delegate_child: false })

    const result = flattenSessionsWithBranches([parent, userBranch, subagentChild, kanbanTopLevel, toolTopLevel])

    expect(result).toEqual([
      { session: kanbanTopLevel },
      { session: parent },
      { branchStem: '└─ ', session: userBranch },
      { session: toolTopLevel }
    ])
  })

  it('also filters a child whose payload contradicts itself (delegate_from set, flag false)', () => {
    const parent = session('parent', { last_active: 20 })

    const userBranch = session('user-branch', {
      last_active: 15,
      parent_session_id: 'parent',
      is_delegate_child: false
    })

    const contradictory = session('subagent-child', {
      last_active: 18,
      parent_session_id: 'parent',
      delegate_from: 'parent',
      is_delegate_child: false
    })

    // Without OR semantics this row survives the filter and renders as a
    // nested branch of `parent` -- the leak the sidebar exists to prevent.
    expect(flattenSessionsWithBranches([parent, userBranch, contradictory])).toEqual([
      { session: parent },
      { branchStem: '└─ ', session: userBranch }
    ])
  })
})

describe('isDelegateChildSession', () => {
  it('returns true when is_delegate_child is true or delegate_from is non-null', () => {
    expect(isDelegateChildSession(session('child', { is_delegate_child: true, delegate_from: 'parent' }))).toBe(true)
    expect(isDelegateChildSession(session('child', { delegate_from: 'parent' }))).toBe(true)
    // The flag alone, with no parent named — still a child.
    expect(isDelegateChildSession(session('child', { is_delegate_child: true }))).toBe(true)
  })

  it('treats an inconsistent payload as a child: delegate_from wins over is_delegate_child false', () => {
    // Neither key takes precedence over the other. A row naming a delegating
    // parent while flagging false contradicts itself, and the two errors are
    // not symmetric: reading it as top-level leaks a background subagent into
    // the sidebar, reading it as a child only nests a row under its parent.
    // Today's backend never emits this shape -- this pins the behaviour for
    // when a future one does.
    expect(isDelegateChildSession(session('child', { delegate_from: 'parent', is_delegate_child: false }))).toBe(true)

    expect(
      isDelegateChildSession(
        session('child', { delegate_from: 'parent', is_delegate_child: false, parent_session_id: 'parent' })
      )
    ).toBe(true)
  })

  it('returns false for an empty or whitespace-only delegate_from', () => {
    // No parent is actually named, so there is no delegation to infer -- the
    // non-empty requirement is what keeps a blank column value from hiding a
    // legitimate top-level row.
    expect(isDelegateChildSession(session('blank', { delegate_from: '' }))).toBe(false)
    expect(isDelegateChildSession(session('blank', { delegate_from: '   ' }))).toBe(false)
    expect(isDelegateChildSession(session('null-parent', { delegate_from: null }))).toBe(false)
  })

  it('returns false for user branches with parent_session_id alone', () => {
    expect(isDelegateChildSession(session('branch', { parent_session_id: 'parent', is_delegate_child: false }))).toBe(
      false
    )
    expect(isDelegateChildSession(session('branch', { parent_session_id: 'parent' }))).toBe(false)
  })

  it('returns false for top-level kanban and tool sessions', () => {
    expect(isDelegateChildSession(session('kanban', { source: 'kanban' }))).toBe(false)
    expect(isDelegateChildSession(session('tool', { source: 'tool' }))).toBe(false)
  })

  it('returns false for older payloads lacking delegation metadata', () => {
    const legacySession = session('legacy', { parent_session_id: 'parent' })
    delete legacySession.is_delegate_child
    delete legacySession.delegate_from
    expect(isDelegateChildSession(legacySession)).toBe(false)

    // Half-migrated backends emit one key without the other. Neither partial
    // shape may start hiding ordinary rows.
    const flagOnly = session('flag-only', { is_delegate_child: false, parent_session_id: 'parent' })
    delete flagOnly.delegate_from
    expect(isDelegateChildSession(flagOnly)).toBe(false)

    const parentOnly = session('parent-only', { delegate_from: null, parent_session_id: 'parent' })
    delete parentOnly.is_delegate_child
    expect(isDelegateChildSession(parentOnly)).toBe(false)
  })
})
