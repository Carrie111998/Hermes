import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import type { SidebarListRow } from '@/lib/session-date-groups'

const { lineageCalls } = vi.hoisted(() => ({
  lineageCalls: [] as Array<{ compact: boolean; renderContent?: (control: ReactNode, content: ReactNode) => ReactNode }>
}))

vi.mock('@tanstack/react-virtual', () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getTotalSize: () => count * 28,
    getVirtualItems: () =>
      Array.from({ length: count }, (_, index) => ({ end: (index + 1) * 28, index, start: index * 28 })),
    measureElement: () => undefined
  })
}))

vi.mock('@dnd-kit/sortable', () => ({
  useSortable: () => ({
    attributes: {},
    isDragging: false,
    listeners: {},
    setNodeRef: () => undefined,
    transform: null,
    transition: undefined
  })
}))

vi.mock('@dnd-kit/utilities', () => ({
  CSS: { Transform: { toString: () => undefined } }
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        dateDivider: {}
      }
    }
  })
}))

vi.mock('./context-lineage', () => ({
  SelectedContextLineage: (props: {
    compact: boolean
    renderContent?: (control: ReactNode, content: ReactNode) => ReactNode
  }) => {
    lineageCalls.push(props)

    return props.renderContent?.(
      <span data-testid="lineage-control">14 segments</span>,
      <div data-testid="lineage-compact">Current + previous two</div>
    )
  }
}))

vi.mock('./session-row', () => ({
  SidebarSessionRow: ({ lineageControl, session }: { lineageControl?: ReactNode; session: SessionInfo }) => (
    <div data-testid={`session-${session.id}`}>
      {session.id}
      {lineageControl}
    </div>
  )
}))

import { VirtualSessionList } from './virtual-session-list'

afterEach(() => {
  cleanup()
  lineageCalls.length = 0
})

const noop = () => undefined

describe('VirtualSessionList context lineage', () => {
  it('keeps the selected session badge and compact three-segment view inside the measured sortable item', () => {
    const session = {
      id: 'selected',
      last_active: 100,
      profile: 'default',
      started_at: 100
    } as SessionInfo

    const rows = [{ entry: { session }, kind: 'session' }] as SidebarListRow[]

    render(
      <VirtualSessionList
        activeSessionId="selected"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onTogglePin={noop}
        pinned={false}
        rows={rows}
        sortable
        workingSessionIdSet={new Set()}
      />
    )

    expect(lineageCalls).toHaveLength(1)
    expect(lineageCalls[0]?.compact).toBe(true)
    expect(screen.getByTestId('lineage-control')).toBeTruthy()
    expect(screen.getByTestId('lineage-compact')).toBeTruthy()
    expect(screen.getByTestId('session-selected').parentElement?.contains(screen.getByTestId('lineage-compact'))).toBe(
      true
    )
  })
})
