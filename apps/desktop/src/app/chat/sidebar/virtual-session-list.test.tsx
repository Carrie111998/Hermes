import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SidebarListRow } from '@/lib/session-date-groups'
import type { SessionInfo } from '@/types/hermes'

import { VirtualSessionList } from './virtual-session-list'

const virtualizer = {
  getTotalSize: () => 68,
  getVirtualItems: () => [
    { end: 26, index: 0, start: 0 },
    { end: 68, index: 1, start: 26 }
  ],
  measure: vi.fn(),
  measureElement: vi.fn()
}

const virtualizerOptions = vi.hoisted(() => ({ current: null as null | { getItemKey: (index: number) => unknown } }))

const useSortable = vi.hoisted(() =>
  vi.fn((_options: { id: string }) => ({
    attributes: {},
    isDragging: false,
    listeners: {},
    setNodeRef: vi.fn(),
    transform: null,
    transition: ''
  }))
)

const virtualRowPropsHistory: Array<{
  onArchive: () => void
  onBranch?: () => void
  onToggleUnread: () => void
}> = []

vi.mock('@dnd-kit/sortable', () => ({ useSortable }))
vi.mock('@dnd-kit/utilities', () => ({ CSS: { Transform: { toString: vi.fn() } } }))
vi.mock('@tanstack/react-virtual', () => ({
  useVirtualizer: (options: { getItemKey: (index: number) => unknown }) => {
    virtualizerOptions.current = options

    return virtualizer
  }
}))

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

vi.mock('./chrome', () => ({
  SidebarDateDivider: ({ label }: { label: string }) => <div data-testid={`divider-${label}`} />
}))

vi.mock('./session-row', () => ({
  SidebarSessionRow: (props: {
    isSelected: boolean
    onArchive: () => void
    onBranch?: () => void
    onDelete: () => void
    onToggleUnread: () => void
  }) => {
    virtualRowPropsHistory.push(props)
    const { isSelected, onDelete } = props

    return (
      <button
        data-selected={isSelected ? 'true' : 'false'}
        data-testid="virtual-delete"
        onClick={onDelete}
        type="button"
      />
    )
  }
}))

afterEach(cleanup)

const rows: SidebarListRow[] = [
  { key: 'today', kind: 'divider', label: 'Today' },
  { key: 'older', kind: 'divider', label: 'Older' }
]

const noop = () => {}

describe('VirtualSessionList', () => {
  it('carries the virtual duplicate row exact owner through archive, branch, and unread actions', () => {
    virtualRowPropsHistory.length = 0
    const onArchiveSession = vi.fn()
    const onBranchSession = vi.fn()
    const onToggleUnread = vi.fn()
    const session = { connection_id: 'source-b', id: 'shared', profile: 'worker-b' } as SessionInfo

    render(
      <VirtualSessionList
        activeSessionId={null}
        onArchiveSession={onArchiveSession}
        onBranchSession={onBranchSession}
        onDeleteSession={noop}
        onResumeSession={noop}
        onTogglePin={noop}
        onToggleUnread={onToggleUnread}
        pinned={false}
        rows={[
          { key: 'today', kind: 'divider', label: 'Today' },
          { entry: { session }, kind: 'session' }
        ]}
        sortable={false}
      />
    )

    const props = virtualRowPropsHistory.at(-1)!
    props.onArchive()
    props.onBranch?.()
    props.onToggleUnread()

    const ownerRoute = {
      connectionId: 'source-b',
      profile: 'worker-b',
      targetProfile: 'worker-b'
    }

    expect(onArchiveSession).toHaveBeenCalledWith('shared', ownerRoute)
    expect(onBranchSession).toHaveBeenCalledWith('shared', ownerRoute)
    expect(onToggleUnread).toHaveBeenCalledWith('shared', ownerRoute)
  })

  it('uses distinct owner-qualified virtual and sortable ids for duplicate rows', () => {
    const sessionRow = (connection_id: string): SidebarListRow => ({
      entry: { session: { connection_id, id: 'shared', profile: 'worker' } as SessionInfo },
      kind: 'session'
    })

    const duplicateRows = [sessionRow('source-a'), sessionRow('source-b')]

    useSortable.mockClear()
    render(
      <VirtualSessionList
        activeSessionId={null}
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        pinned={false}
        rows={duplicateRows}
        sortable
      />
    )

    const virtualIds = duplicateRows.map((_, index) => virtualizerOptions.current?.getItemKey(index))
    const sortableIds = useSortable.mock.calls.map(call => call[0].id)

    expect(new Set(virtualIds).size).toBe(2)
    expect(new Set(sortableIds).size).toBe(2)
    expect(sortableIds).toEqual(virtualIds)
  })

  it('treats explicit null identity as selecting no virtual row', () => {
    const session = { id: 'shared', profile: 'default' } as SessionInfo

    const sessionRows: SidebarListRow[] = [
      { key: 'today', kind: 'divider', label: 'Today' },
      { entry: { session }, kind: 'session' }
    ]

    const { getByTestId } = render(
      <VirtualSessionList
        activeSessionId="shared"
        activeSessionIdentity={null}
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        pinned={false}
        rows={sessionRows}
        sortable={false}
      />
    )

    expect(getByTestId('virtual-delete').getAttribute('data-selected')).toBe('false')
  })

  it('passes a virtualized row exact owner to deletion', () => {
    const onDeleteSession = vi.fn()

    const session = {
      connection_id: 'source-b',
      id: 'shared',
      profile: 'worker-b'
    } as SessionInfo

    const sessionRows: SidebarListRow[] = [
      { key: 'today', kind: 'divider', label: 'Today' },
      { entry: { session }, kind: 'session' }
    ]

    const { getByTestId } = render(
      <VirtualSessionList
        activeSessionId={null}
        onArchiveSession={noop}
        onDeleteSession={onDeleteSession}
        onResumeSession={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        pinned={false}
        rows={sessionRows}
        sortable={false}
      />
    )

    fireEvent.click(getByTestId('virtual-delete'))

    expect(onDeleteSession).toHaveBeenCalledWith('shared', {
      connectionId: 'source-b',
      profile: 'worker-b',
      targetProfile: 'worker-b'
    })
  })

  it('positions measured rows independently within a total-size spacer', () => {
    const { getByTestId } = render(
      <VirtualSessionList
        activeSessionId={null}
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        pinned={false}
        rows={rows}
        sortable={false}
      />
    )

    const firstItem = getByTestId('divider-Today').parentElement
    const secondItem = getByTestId('divider-Older').parentElement
    const spacer = firstItem?.parentElement

    expect(firstItem?.dataset.index).toBe('0')
    expect(firstItem?.style.position).toBe('absolute')
    expect(firstItem?.style.transform).toBe('translateY(0px)')
    expect(secondItem?.dataset.index).toBe('1')
    expect(secondItem?.style.transform).toBe('translateY(26px)')
    expect(spacer?.className).toBe('relative')
    expect(spacer?.style.height).toBe('68px')
    expect(spacer?.style.paddingTop).toBe('')
    expect(spacer?.style.paddingBottom).toBe('')
  })

  it('lets wheel overscroll chain to the outer sidebar scroller (#84964)', () => {
    const { getByTestId } = render(
      <VirtualSessionList
        activeSessionId={null}
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        pinned={false}
        rows={rows}
        sortable={false}
      />
    )

    const scroller = getByTestId('divider-Today').parentElement?.parentElement?.parentElement

    // The inner virtualized scroller must NOT contain overscroll: it is nested
    // inside the sidebar's own scroll container, and containing it swallowed
    // wheel events at the inner scroll boundary — the mid-list wheel dead-zone
    // at 25+ sessions. Chaining stays inside the sidebar because the OUTER
    // scroller keeps overscroll-contain.
    expect(scroller?.className).toContain('overflow-y-auto')
    expect(scroller?.className).not.toContain('overscroll-contain')
  })
})
