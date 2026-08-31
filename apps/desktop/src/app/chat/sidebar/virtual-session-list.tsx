import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { useVirtualizer } from '@tanstack/react-virtual'
import { type FC, useCallback, useRef } from 'react'

import type { SessionInfo } from '@/hermes'
import { cn } from '@/lib/utils'
import { sessionPinId } from '@/store/session'

import { SidebarDateSectionHeader } from './date-section-header'
import type { SidebarSessionListItem } from './session-date-sections'
import { SidebarSessionRow } from './session-row'

interface SessionRowCommonProps {
  branchStem?: string
  isPinned: boolean
  isSelected: boolean
  isWorking: boolean
  onArchive: () => void
  onBranch?: () => void
  onDelete: () => void
  onPin: () => void
  onResume: () => void
  reorderable?: boolean
  showProfile?: boolean
}

interface VirtualSessionListProps {
  activeSessionId: null | string
  className?: string
  items: SidebarSessionListItem[]
  onArchiveSession: (sessionId: string) => void
  onBranchSession?: (sessionId: string, profile?: string) => void
  onDeleteSession: (sessionId: string) => void
  onResumeSession: (sessionId: string) => void
  onTogglePin: (sessionId: string) => void
  pinned: boolean
  showProfileTags?: boolean
  sortable: boolean
  workingSessionIdSet: Set<string>
}

const ROW_ESTIMATE_PX = 28
const HEADER_ESTIMATE_PX = 24
const OVERSCAN_ROWS = 12

export const VirtualSessionList: FC<VirtualSessionListProps> = ({
  activeSessionId,
  className,
  items,
  onArchiveSession,
  onBranchSession,
  onDeleteSession,
  onResumeSession,
  onTogglePin,
  pinned,
  showProfileTags = false,
  sortable,
  workingSessionIdSet
}) => {
  const scrollerRef = useRef<HTMLDivElement | null>(null)

  const virtualizer = useVirtualizer({
    count: items.length,
    estimateSize: index => (items[index]?.kind === 'header' ? HEADER_ESTIMATE_PX : ROW_ESTIMATE_PX),
    getItemKey: index => {
      const item = items[index]

      return (item?.kind === 'header' ? item.id : item?.entry.session.id) ?? index
    },
    getScrollElement: () => scrollerRef.current,
    // jsdom-friendly default; the real rect takes over on first observe.
    initialRect: { height: 600, width: 240 },
    overscan: OVERSCAN_ROWS
  })

  const virtualItems = virtualizer.getVirtualItems()
  const totalSize = virtualizer.getTotalSize()
  const paddingTop = virtualItems[0]?.start ?? 0
  const paddingBottom = Math.max(0, totalSize - (virtualItems[virtualItems.length - 1]?.end ?? 0))

  const rows = virtualItems.map(virtualItem => {
    const item = items[virtualItem.index]

    if (!item) {
      return null
    }

    if (item.kind === 'header') {
      return (
        <SidebarDateSectionHeader
          bucket={item.bucket}
          data-index={virtualItem.index}
          key={item.id}
          ref={virtualizer.measureElement}
        />
      )
    }

    const { branchStem, session } = item.entry
    const reorderable = sortable && !branchStem

    const commonProps: SessionRowCommonProps = {
      branchStem,
      isPinned: pinned,
      isSelected: session.id === activeSessionId,
      isWorking: workingSessionIdSet.has(session.id),
      onArchive: () => onArchiveSession(session.id),
      onBranch: onBranchSession ? () => onBranchSession(session.id, session.profile) : undefined,
      onDelete: () => onDeleteSession(session.id),
      onPin: () => onTogglePin(sessionPinId(session)),
      onResume: () => onResumeSession(session.id),
      reorderable,
      showProfile: showProfileTags
    }

    return reorderable ? (
      <VirtualSortableRow
        index={virtualItem.index}
        key={session.id}
        measureRef={virtualizer.measureElement}
        rowProps={commonProps}
        session={session}
      />
    ) : (
      <SidebarSessionRow
        {...commonProps}
        data-index={virtualItem.index}
        key={session.id}
        ref={virtualizer.measureElement}
        session={session}
      />
    )
  })

  // When sortable, the caller wraps this in a ReorderableList that owns the
  // DndContext + SortableContext (keyed on the same ids); the virtualized rows
  // just consume that context via useSortable.
  return (
    <div
      className={cn('relative min-h-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain', className)}
      ref={scrollerRef}
    >
      <div className="grid gap-px" style={{ paddingBottom: `${paddingBottom}px`, paddingTop: `${paddingTop}px` }}>
        {rows}
      </div>
    </div>
  )
}

interface VirtualSortableRowProps {
  index: number
  measureRef: (node: Element | null) => void
  rowProps: SessionRowCommonProps
  session: SessionInfo
}

function VirtualSortableRow({ index, measureRef, rowProps, session }: VirtualSortableRowProps) {
  const { attributes, isDragging, listeners, setNodeRef, transform, transition } = useSortable({ id: session.id })

  // Merge dnd-kit's setNodeRef with the virtualizer's measureElement so
  // the row participates in both DnD hit-testing and TanStack height
  // measurement.
  const refMerged = useCallback(
    (node: HTMLDivElement | null) => {
      setNodeRef(node)
      measureRef(node)
    },
    [measureRef, setNodeRef]
  )

  return (
    <SidebarSessionRow
      {...rowProps}
      data-index={index}
      dragging={isDragging}
      dragHandleProps={{ ...attributes, ...listeners }}
      ref={refMerged}
      reorderable
      session={session}
      style={{ transform: CSS.Transform.toString(transform), transition }}
    />
  )
}
