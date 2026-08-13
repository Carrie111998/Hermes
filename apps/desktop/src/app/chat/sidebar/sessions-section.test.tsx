import { cleanup, render } from '@testing-library/react'
import type * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'

import type { SidebarSessionGroup } from './projects/workspace-groups'
import { SidebarSessionsSection, VIRTUALIZE_THRESHOLD } from './sessions-section'
import type { VirtualSessionListProps } from './virtual-session-list'

afterEach(cleanup)

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        dateDivider: {
          earlierThisMonth: 'Earlier this month',
          lastMonth: 'Last month',
          lastWeek: 'Last week',
          older: 'Older',
          today: 'Today',
          yesterday: 'Yesterday'
        }
      }
    }
  })
}))

const mockVirtualListPropsHistory: VirtualSessionListProps[] = []

vi.mock('./virtual-session-list', () => ({
  VirtualSessionList: (props: VirtualSessionListProps) => {
    mockVirtualListPropsHistory.push(props)

    return <div data-testid="virtual-session-list">Virtual List ({props.rows.length} rows)</div>
  }
}))

vi.mock('./session-row', () => ({
  SidebarSessionRow: ({ session }: { session: SessionInfo }) => (
    <div data-testid={`session-row-${session.id}`}>{session.id}</div>
  )
}))

// The group branch exercises the real sortable wiring, so stub the heavy group
// renderer and record whether each group was handed the sortable bindings.
vi.mock('./projects', () => ({
  EnteredProjectContent: () => null,
  ProjectOverviewRow: () => null,
  SidebarWorkspaceGroup: ({ group, reorderable }: { group: SidebarSessionGroup; reorderable?: boolean }) => (
    <div data-group-id={group.id} data-reorderable={String(Boolean(reorderable))}>
      {group.id}
    </div>
  )
}))

function makeSession(id: string, startedAt = 1000): SessionInfo {
  return {
    handoff_platform: null,
    handoff_state: null,
    id,
    last_active: startedAt,
    profile: 'default',
    started_at: startedAt
  } as unknown as SessionInfo
}

function generateSessions(count: number): SessionInfo[] {
  return Array.from({ length: count }, (_, i) => makeSession(`session-${i + 1}`, 10000 - i * 100))
}

const noop = () => {}

describe('SidebarSessionsSection memoization & virtualizer stability', () => {
  it('memoizes flatRows and passes the exact same rows array reference across parent re-renders', () => {
    mockVirtualListPropsHistory.length = 0

    const sessions = generateSessions(VIRTUALIZE_THRESHOLD + 5)

    const { rerender } = render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        open={true}
        pinned={false}
        sessions={sessions}
      />
    )

    expect(mockVirtualListPropsHistory.length).toBe(1)
    const initialRowsRef = mockVirtualListPropsHistory[0].rows
    expect(initialRowsRef.length).toBeGreaterThan(VIRTUALIZE_THRESHOLD)

    // Re-render parent with the exact same sessions array and props
    rerender(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        open={true}
        pinned={false}
        sessions={sessions}
      />
    )

    expect(mockVirtualListPropsHistory.length).toBe(2)
    const nextRowsRef = mockVirtualListPropsHistory[1].rows

    // Confirm that the flatRows array reference remains strictly identical across renders (useMemo proof)
    expect(nextRowsRef).toBe(initialRowsRef)
  })

  it('re-computes flatRows reference when grouping or sessions change', () => {
    mockVirtualListPropsHistory.length = 0

    const initialSessions = generateSessions(VIRTUALIZE_THRESHOLD + 2)

    const { rerender } = render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        grouping="none"
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        open={true}
        pinned={false}
        sessions={initialSessions}
      />
    )

    const firstRowsRef = mockVirtualListPropsHistory[0].rows

    // Switch on date dividers
    rerender(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        grouping="date"
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        open={true}
        pinned={false}
        sessions={initialSessions}
      />
    )

    const secondRowsRef = mockVirtualListPropsHistory[1].rows
    expect(secondRowsRef).not.toBe(firstRowsRef)

    // Change sessions array identity
    const updatedSessions = generateSessions(VIRTUALIZE_THRESHOLD + 4)
    rerender(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        grouping="date"
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        open={true}
        pinned={false}
        sessions={updatedSessions}
      />
    )

    const thirdRowsRef = mockVirtualListPropsHistory[2].rows
    expect(thirdRowsRef).not.toBe(secondRowsRef)
  })
})

describe('SidebarSessionsSection profile-group reorder', () => {
  const group = (id: string): SidebarSessionGroup => ({
    id,
    label: id,
    mode: 'profile',
    path: null,
    sessions: [makeSession(`session-${id}`)]
  })

  const renderGroups = (groups: SidebarSessionGroup[], onReorderGroups?: (ids: string[]) => void) =>
    render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        groups={groups}
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onReorderGroups={onReorderGroups}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        open={true}
        pinned={false}
        sessions={[]}
      />
    )

  it('makes named profile groups sortable and keeps default a static fixture on top', () => {
    const { container } = renderGroups(
      [group('beta'), group('default'), group('alpha')],
      vi.fn()
    )

    const rendered = Array.from(container.querySelectorAll('[data-group-id]')).map(
      node => [node.getAttribute('data-group-id'), node.getAttribute('data-reorderable')]
    )

    expect(rendered).toEqual([
      ['default', 'false'],
      ['beta', 'true'],
      ['alpha', 'true']
    ])
  })

  it('renders every group static when no reorder handler is wired', () => {
    const { container } = renderGroups([group('default'), group('alpha'), group('beta')])

    const rendered = Array.from(container.querySelectorAll('[data-group-id]')).map(node =>
      node.getAttribute('data-reorderable')
    )

    expect(rendered).toEqual(['false', 'false', 'false'])
  })

  it('renders a single named group static — one item has nothing to reorder', () => {
    const { container } = renderGroups([group('default'), group('alpha')], vi.fn())

    const rendered = Array.from(container.querySelectorAll('[data-group-id]')).map(node =>
      node.getAttribute('data-reorderable')
    )

    expect(rendered).toEqual(['false', 'false'])
  })

  it('leaves non-profile groups static even with a reorder handler wired', () => {
    const { container } = renderGroups(
      [{ ...group('alpha'), mode: 'workspace' }, { ...group('beta'), mode: 'source' }],
      vi.fn()
    )

    const rendered = Array.from(container.querySelectorAll('[data-group-id]')).map(node =>
      node.getAttribute('data-reorderable')
    )

    expect(rendered).toEqual(['false', 'false'])
  })
})
