import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'

import { SidebarSessionsSection, VIRTUALIZE_THRESHOLD } from '../sessions-section'
import type { VirtualSessionListProps } from '../virtual-session-list'

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

vi.mock('../virtual-session-list', () => ({
  VirtualSessionList: (props: VirtualSessionListProps) => {
    mockVirtualListPropsHistory.push(props)

    return <div data-testid="virtual-session-list">Virtual List ({props.rows.length} rows)</div>
  }
}))

vi.mock('../session-row', () => ({
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

describe('SidebarSessionsSection shared-scroll virtualization', () => {
  it('does not mount an owned virtual scroller without a shared scroll element', () => {
    mockVirtualListPropsHistory.length = 0

    const { queryByTestId, getByTestId } = render(
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
        sessions={generateSessions(VIRTUALIZE_THRESHOLD + 5)}
      />
    )

    // A long list without an outer scroll owner must stay in document flow.
    // Mounting VirtualSessionList here would create the nested overflow-y-auto
    // + overscroll-contain port that latches the wheel mid-list (#84964).
    expect(queryByTestId('virtual-session-list')).toBeNull()
    expect(mockVirtualListPropsHistory).toHaveLength(0)
    expect(getByTestId('session-row-session-1')).toBeTruthy()
  })

  it('virtualizes only into a provided shared scroll element', () => {
    mockVirtualListPropsHistory.length = 0

    const host = { id: 'sidebar-scroll' } as HTMLElement
    const getScrollElement = () => host

    const { getByTestId } = render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        getScrollElement={getScrollElement}
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        open={true}
        pinned={false}
        sessions={generateSessions(VIRTUALIZE_THRESHOLD + 5)}
      />
    )

    expect(getByTestId('virtual-session-list')).toBeTruthy()
    expect(mockVirtualListPropsHistory).toHaveLength(1)
    expect(
      (mockVirtualListPropsHistory[0] as { getScrollElement?: () => HTMLElement | null }).getScrollElement
    ).toBe(getScrollElement)
  })
})
