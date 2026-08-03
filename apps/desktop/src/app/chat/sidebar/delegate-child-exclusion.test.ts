import { describe, expect, it } from 'vitest'

import { flattenSessionsWithBranches, isDelegateChildSession } from '@/lib/session-branch-tree'
import type { SessionInfo } from '@/types/hermes'

// The sidebar hides delegated (background subagent) children everywhere it
// lists sessions, but the rows it must KEEP look structurally identical: a
// user `/branch` fork also carries `parent_session_id`, and kanban/tool rows
// are ordinary top-level sessions. The only safe discriminator is the
// backend's explicit `is_delegate_child` / `delegate_from` metadata, derived
// from `model_config._delegate_from` (see hermes_state_common).
//
// This file guards the sidebar's side of that contract across the states the
// per-list unit tests don't reach: every lifecycle state a delegated child
// passes through, and the Pinned section's preserve-order path.

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

const delegateChild = (id: string, overrides: Partial<SessionInfo> = {}): SessionInfo =>
  session(id, { delegate_from: 'parent', is_delegate_child: true, parent_session_id: 'parent', ...overrides })

// Every state a delegated child is observed in: spawned and running, finished
// but unread (the "background" row), ended, and archived. A filter keyed on
// anything but the explicit flag (e.g. `ended_at`/`is_active`) lets the child
// resurface in one of these.
const LIFECYCLE_STATES: Array<[string, Partial<SessionInfo>]> = [
  ['running', { ended_at: null, is_active: true, last_active: 30 }],
  ['finished unread', { ended_at: 40, is_active: false, last_active: 40 }],
  ['ended', { ended_at: 50, is_active: false, last_active: 10 }],
  ['archived', { archived: true, ended_at: 60, is_active: false, last_active: 5 }]
]

describe('sidebar delegate-child exclusion', () => {
  it.each(LIFECYCLE_STATES)('drops a delegated child that is %s', (_label, state) => {
    const parent = session('parent', { last_active: 20 })
    const child = delegateChild('subagent', state)

    expect(isDelegateChildSession(child)).toBe(true)
    expect(flattenSessionsWithBranches([parent, child])).toEqual([{ session: parent }])
  })

  it('keeps the parent and its user branches when a delegated sibling is dropped', () => {
    const parent = session('parent', { last_active: 20 })
    const branchA = session('branch-a', { last_active: 15, parent_session_id: 'parent' })
    const branchB = session('branch-b', { last_active: 10, parent_session_id: 'parent' })
    const child = delegateChild('subagent', { is_active: true, last_active: 18 })

    // Stems must be recomputed over the surviving branches only — a dropped
    // delegate must not leave a '├─ ' dangling on the last visible branch.
    expect(flattenSessionsWithBranches([parent, branchA, child, branchB])).toEqual([
      { session: parent },
      { branchStem: '├─ ', session: branchA },
      { branchStem: '└─ ', session: branchB }
    ])
  })

  it('keeps top-level kanban and tool rows in every lifecycle state', () => {
    const rows = [
      session('kanban-running', { is_active: true, last_active: 30, source: 'kanban' }),
      session('tool-ended', { ended_at: 20, last_active: 20, source: 'tool' })
    ]

    for (const row of rows) {
      expect(isDelegateChildSession(row)).toBe(false)
    }

    expect(flattenSessionsWithBranches(rows)).toEqual([{ session: rows[0] }, { session: rows[1] }])
  })

  it('drops delegated children from the Pinned section without reordering it', () => {
    // The Pinned section renders with preserveOrder so a finishing background
    // turn can't float rows over the user's fixed ranking; filtering must not
    // re-sort what survives.
    const first = session('pin-1', { last_active: 1 })
    const second = session('pin-2', { last_active: 99 })
    const child = delegateChild('pinned-subagent', { is_active: true, last_active: 50 })

    expect(flattenSessionsWithBranches([first, child, second], { preserveOrder: true })).toEqual([
      { session: first },
      { session: second }
    ])
  })

  it('keeps a lone delegated child from being the only row rendered', () => {
    // The short-circuit for lists under two entries must filter first,
    // otherwise a single background child renders as a top-level session.
    expect(flattenSessionsWithBranches([delegateChild('subagent', { is_active: true })])).toEqual([])
  })

  it('leaves rows from a backend predating the flag alone', () => {
    // Older payloads carry neither field; treating "unknown" as delegated
    // would empty the sidebar against an un-upgraded gateway.
    const legacyParent = session('legacy-parent', { last_active: 20 })
    const legacyBranch = session('legacy-branch', { last_active: 10, parent_session_id: 'legacy-parent' })

    expect(flattenSessionsWithBranches([legacyParent, legacyBranch])).toEqual([
      { session: legacyParent },
      { branchStem: '└─ ', session: legacyBranch }
    ])
  })
})
