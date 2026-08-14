import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { useVirtualizer } from '@tanstack/react-virtual'
import type * as React from 'react'
import { type FC, useCallback, useRef } from 'react'

import type { SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { type SidebarListRow } from '@/lib/session-date-groups'
import { sessionBucketLabel } from '@/lib/time'
import { cn } from '@/lib/utils'
import { sessionPinId } from '@/store/session'

import { SidebarDateDivider } from './chrome'
import { SidebarSessionRow } from './session-row'

interface SessionRowCommonProps {
  branchStem?: string
  card?: boolean
  isPinned: boolean
  isSelected: boolean
  onArchive: () => void
  onBranch?: () => void
  onDelete: () => void
  onPin: () => void
  onResume: () => void
  reorderable?: boolean
  showProfile?: boolean
}

export interface VirtualSessionListProps {
  activeSessionId: null | string
  /** Render every session row as the three-line inbox card. */
  card?: boolean
  className?: string
  /** Hover-revealed control for date dividers (the group-level "+"). */
  dividerAction?: React.ReactNode
  rows: SidebarListRow[]
  onArchiveSession: (sessionId: string) => void
  onBranchSession?: (sessionId: string, profile?: string) => void
  onDeleteSession: (sessionId: string) => void
  onResumeSession: (sessionId: string) => void
  onTogglePin: (sessionId: string) => void
  pinned: boolean
  showProfileTags?: boolean
  sortable: boolean
}

// Compact session rows are min-h-[1.625rem] (26px at a 16px root). Measuring
// them is what made the list bounce: CSS gap-px only exists between the
// currently mounted children, so every measure disagreed with the virtualizer
// and paddingTop jumped. Compact mode is estimate-only.
const ROW_ESTIMATE_PX = 26
// Matches the card's typical rendered height (four lines when a preview
// exists) so long card lists don't jump under the scroll thumb before
// self-measurement catches up.
const CARD_ROW_ESTIMATE_PX = 66
const OVERSCAN_ROWS = 12

// Must match SidebarDateDivider's h-8 shell.
const DIVIDER_ESTIMATE_PX = 32
const ROW_GAP_PX = 1

const neverAdjustScrollOnResize = () => false

const measureSidebarRow = (element: Element) => Math.round((element as HTMLElement).offsetHeight)

/** Compact rows are fixed-height; only inbox cards need live measurement. */
export function shouldMeasureSidebarRows(card: boolean): boolean {
  return card
}

/** Estimate the rendered height of one flat sidebar row, divider-aware. */
export function estimateSidebarRowSize(row: SidebarListRow | undefined, card: boolean): number {
  if (row?.kind === 'divider') {
    return DIVIDER_ESTIMATE_PX
  }

  return card ? CARD_ROW_ESTIMATE_PX : ROW_ESTIMATE_PX
}

export const VirtualSessionList: FC<VirtualSessionListProps> = ({
  activeSessionId,
  card = false,
  className,
  dividerAction,
  rows: listRows,
  onArchiveSession,
  onBranchSession,
  onDeleteSession,
  onResumeSession,
  onTogglePin,
  pinned,
  showProfileTags = false,
  sortable
}) => {
  const { t } = useI18n()
  const dividerLabels = t.sidebar.dateDivider
  const scrollerRef = useRef<HTMLDivElement | null>(null)

  const measureRows = shouldMeasureSidebarRows(card)

  const virtualizer = useVirtualizer({
    count: listRows.length,
    estimateSize: index => estimateSidebarRowSize(listRows[index], card),
    gap: ROW_GAP_PX,
    getItemKey: index => {
      const row = listRows[index]

      return row ? (row.kind === 'divider' ? row.key : row.entry.session.id) : index
    },
    getScrollElement: () => scrollerRef.current,
    // jsdom-friendly default; the real rect takes over on first observe.
    initialRect: { height: 600, width: 240 },
    ...(measureRows ? { measureElement: measureSidebarRow } : {}),
    overscan: OVERSCAN_ROWS
  })

  // Not a hook option in 3.14.x. First measure of a taller/shorter divider used
  // to nudge scrollTop, unmount the row, remount it, and flicker the list.
  virtualizer.shouldAdjustScrollPositionOnItemSizeChange = neverAdjustScrollOnResize

  const virtualItems = virtualizer.getVirtualItems()
  const totalSize = virtualizer.getTotalSize()
  const paddingTop = virtualItems[0]?.start ?? 0
  const paddingBottom = Math.max(0, totalSize - (virtualItems[virtualItems.length - 1]?.end ?? 0))

  const rows = virtualItems.map(virtualItem => {
    const row = listRows[virtualItem.index]

    if (!row) {
      return null
    }

    // Dividers are non-sortable, self-measured rows interleaved with sessions.
    if (row.kind === 'divider') {
      return (
        <SidebarDateDivider
          action={dividerAction}
          data-index={virtualItem.index}
          key={row.key}
          label={'label' in row ? row.label : sessionBucketLabel(row.bucket, dividerLabels)}
          ref={measureRows ? virtualizer.measureElement : undefined}
        />
      )
    }

    const { branchStem, session } = row.entry
    const reorderable = sortable && !branchStem

    const commonProps: SessionRowCommonProps = {
      branchStem,
      card,
      isPinned: pinned,
      isSelected: session.id === activeSessionId,
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
        measureRef={measureRows ? virtualizer.measureElement : undefined}
        rowProps={commonProps}
        session={session}
      />
    ) : (
      <SidebarSessionRow
        {...commonProps}
        data-index={virtualItem.index}
        key={session.id}
        ref={measureRows ? virtualizer.measureElement : undefined}
        session={session}
      />
    )
  })

  // When sortable, the caller wraps this in a ReorderableList that owns the
  // DndContext + SortableContext (keyed on the same ids); the virtualized rows
  // just consume that context via useSortable.
  return (
    <div
      // scrollbar-fade, NOT scrollbar-overlay: overlay opts out of the themed
      // thin scrollbar entirely, and on Windows (no native overlay scrollbars)
      // Chromium then paints the classic always-visible gutter. The themed
      // fade bar reserves its 4px on every platform but stays invisible until
      // hover, and the wrapper no longer stacks a second scroller, so the
      // double-gutter this class change was reaching for is already gone.
      className={cn(
        'scrollbar-fade relative min-h-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain [overflow-anchor:none]',
        className
      )}
      ref={scrollerRef}
    >
      <div className="grid" style={{ paddingBottom: `${paddingBottom}px`, paddingTop: `${paddingTop}px` }}>
        {rows}
      </div>
    </div>
  )
}

interface VirtualSortableRowProps {
  index: number
  measureRef?: (node: Element | null) => void
  rowProps: SessionRowCommonProps
  session: SessionInfo
}

function VirtualSortableRow({ index, measureRef, rowProps, session }: VirtualSortableRowProps) {
  const { attributes, isDragging, listeners, setNodeRef, transform, transition } = useSortable({ id: session.id })

  // Merge dnd-kit's setNodeRef with the virtualizer's measureElement so
  // the row participates in both DnD hit-testing and TanStack height
  // measurement. Compact lists skip measureRef so sizes stay estimated.
  const refMerged = useCallback(
    (node: HTMLDivElement | null) => {
      setNodeRef(node)
      measureRef?.(node)
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
