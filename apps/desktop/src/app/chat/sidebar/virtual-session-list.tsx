import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { useVirtualizer } from '@tanstack/react-virtual'
import type * as React from 'react'
import { type FC, useCallback, useLayoutEffect, useRef, useState } from 'react'

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
  /** When set, the virtualizer scrolls this external element instead of
   *  owning an overflow port (shared-scroll mode). Required to keep the
   *  sessions sidebar to one vertical scrollport. */
  getScrollElement?: () => HTMLElement | null
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

const ROW_ESTIMATE_PX = 28
// Matches the card's typical rendered height (four lines when a preview
// exists) so long card lists don't jump under the scroll thumb before
// self-measurement catches up.
const CARD_ROW_ESTIMATE_PX = 66
const OVERSCAN_ROWS = 12
// The row grid renders with `gap-px`; the virtualizer must be told or every
// row's start drifts 1px further from reality (N-1 px across the list).
const ROW_GAP_PX = 1

export const VirtualSessionList: FC<VirtualSessionListProps> = ({
  activeSessionId,
  card = false,
  className,
  dividerAction,
  getScrollElement: getScrollElementProp,
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
  const containerRef = useRef<HTMLDivElement | null>(null)
  const sharedScroll = Boolean(getScrollElementProp)
  const resolvedGetScrollElement = getScrollElementProp ?? (() => containerRef.current)

  // Shared-scroll offset (TanStack `scrollMargin`): pinned / headers sit
  // above this list in the same scroller. Re-measured after every commit
  // because the offset is sibling layout, not a prop of this component.
  const [sharedScrollMargin, setSharedScrollMargin] = useState(0)
  const [measureRetry, setMeasureRetry] = useState(0)
  const retriedRef = useRef(false)

  // eslint-disable-next-line react-hooks/exhaustive-deps -- sibling layout, not own props
  useLayoutEffect(() => {
    if (!sharedScroll) {
      return
    }

    const el = containerRef.current
    const scrollEl = getScrollElementProp?.()

    if (!el || !scrollEl) {
      // Same-commit mount: ancestor ref is not attached yet. One rAF retry.
      if (!retriedRef.current) {
        retriedRef.current = true
        requestAnimationFrame(() => setMeasureRetry(n => n + 1))
      }

      return
    }

    const margin = Math.round(
      el.getBoundingClientRect().top - scrollEl.getBoundingClientRect().top + scrollEl.scrollTop
    )

    setSharedScrollMargin(prev => (Math.abs(prev - margin) < 1 ? prev : margin))
  })

  const scrollMargin = sharedScroll ? sharedScrollMargin : 0

  const virtualizer = useVirtualizer({
    count: listRows.length,
    estimateSize: () => (card ? CARD_ROW_ESTIMATE_PX : ROW_ESTIMATE_PX),
    gap: ROW_GAP_PX,
    getItemKey: index => {
      const row = listRows[index]

      return row ? (row.kind === 'divider' ? row.key : row.entry.session.id) : index
    },
    getScrollElement: resolvedGetScrollElement,
    // virtual-core scrollToOffset(initialOffset=0) on attach would yank the
    // shared container to the top on remount. Seed from live scrollTop.
    initialOffset: () => resolvedGetScrollElement()?.scrollTop ?? 0,
    // jsdom-friendly default; the real rect takes over on first observe.
    initialRect: { height: 600, width: 240 },
    overscan: OVERSCAN_ROWS,
    scrollMargin
  })

  void measureRetry

  const virtualItems = virtualizer.getVirtualItems()
  const totalSize = virtualizer.getTotalSize()
  // Item start/end include scrollMargin; getTotalSize() does not.
  const firstStart = (virtualItems[0]?.start ?? scrollMargin) - scrollMargin
  const lastEnd = (virtualItems[virtualItems.length - 1]?.end ?? scrollMargin) - scrollMargin
  const paddingTop = Math.max(0, firstStart)
  const paddingBottom = Math.max(0, totalSize - lastEnd)

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
          ref={virtualizer.measureElement}
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
  const ownsScroll = !sharedScroll

  return (
    <div
      // Shared-scroll mode must carry NO overflow property at all — not even
      // overflow-x-hidden. One-axis overflow computes the other axis from
      // `visible` to `auto` (CSS Overflow 3) and silently recreates the
      // nested port that latches the wheel. Owned mode keeps the themed
      // fade bar (not overlay: overlay opts out of the thin scrollbar and
      // on Windows Chromium paints the classic always-visible gutter).
      className={cn(
        'relative min-h-0',
        ownsScroll &&
          'scrollbar-fade flex-1 overflow-x-hidden overflow-y-auto overscroll-contain',
        className
      )}
      ref={containerRef}
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
