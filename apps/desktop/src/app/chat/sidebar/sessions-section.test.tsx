import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'
import type { SessionInfo } from '@/hermes'
import { notifyError } from '@/store/notifications'

import type { SidebarProjectTree } from './projects'
import { PROJECTS_GROUPING_AREA, type ProjectsGroupingContribution } from './projects-presentation'
import { SidebarSessionsSection, VIRTUALIZE_THRESHOLD } from './sessions-section'
import type { VirtualSessionListProps } from './virtual-session-list'

const contributionDisposers: Array<() => void> = []

afterEach(() => {
  cleanup()

  while (contributionDisposers.length) {
    contributionDisposers.pop()?.()
  }
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: {
        cancel: 'Cancel',
        close: 'Close',
        confirm: 'Confirm',
        done: 'Done',
        loading: 'Loading…'
      },
      errors: { genericFailure: 'Something went wrong' },
      sidebar: {
        dateDivider: {
          earlierThisMonth: 'Earlier this month',
          lastMonth: 'Last month',
          lastWeek: 'Last week',
          older: 'Older',
          today: 'Today',
          yesterday: 'Yesterday'
        },
        projects: {
          deleteGroup: 'Delete group',
          deleteGroupEmptyDescription: 'The empty group will be removed.',
          deleteGroupTitle: (name: string) => `Delete group “${name}”?`,
          enter: (label: string) => `Enter ${label}`,
          groupUpdateFailed: 'Could not update project group',
          menu: 'Actions',
          ungrouped: 'Ungrouped'
        }
      }
    }
  })
}))

vi.mock('@/store/notifications', () => ({ notifyError: vi.fn() }))

vi.mock('./projects/project-menu', () => ({
  ProjectContextMenu: ({ children }: { children: React.ReactNode }) => children,
  ProjectMenu: () => null
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

const sidebarProject = (id: string): SidebarProjectTree =>
  ({
    id,
    isNoProject: false,
    label: id,
    path: `/work/${id}`,
    previewSessions: [],
    repos: [],
    sessionCount: 0
  }) as SidebarProjectTree

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
        onToggleUnread={noop}
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
        onToggleUnread={noop}
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

  it('reports synchronous collapse rejections from a class provider', async () => {
    const rejection = new Error('collapse rejected')

    class RejectingProvider implements ProjectsGroupingContribution {
      readonly snapshot = { groups: [{ id: 'one', label: 'One', projectIds: ['a'] }] }

      getSnapshot = () => this.snapshot
      subscribe = () => () => undefined

      setGroupCollapsed() {
        throw this.rejection
      }

      constructor(private readonly rejection: Error) {}
    }

    const contribution = new RejectingProvider(rejection)
    contributionDisposers.push(
      registry.register({ area: PROJECTS_GROUPING_AREA, data: contribution, id: 'rejecting-provider' })
    )

    render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        projectOverview={[]}
        sessions={[]}
      />
    )

    fireEvent.click(screen.getByRole('button', { name: 'One' }))

    await waitFor(() => expect(notifyError).toHaveBeenCalledWith(rejection, 'Could not update project group'))
  })

  it('places a native Actions/Delete control on deletable group headings', async () => {
    const contribution: ProjectsGroupingContribution = {
      deleteGroup: vi.fn().mockResolvedValue(undefined),
      getSnapshot: () => ({ groups: [{ id: 'one', label: 'One', projectIds: [] }] }),
      subscribe: () => () => undefined
    }

    contributionDisposers.push(
      registry.register({ area: PROJECTS_GROUPING_AREA, data: contribution, id: 'deletable-provider' })
    )

    render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        projectOverview={[sidebarProject('a')]}
        sessions={[]}
      />
    )

    const trigger = screen.getByRole('button', { name: 'Actions' })
    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete group' }))

    expect(await screen.findByRole('heading', { name: 'Delete group “One”?' })).toBeTruthy()
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
        onToggleUnread={noop}
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
        onToggleUnread={noop}
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
        onToggleUnread={noop}
        open={true}
        pinned={false}
        sessions={updatedSessions}
      />
    )

    const thirdRowsRef = mockVirtualListPropsHistory[2].rows
    expect(thirdRowsRef).not.toBe(secondRowsRef)
  })
})
