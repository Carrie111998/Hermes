'use client'

import { type ReactNode, useCallback, useRef, useState } from 'react'

import { useResizeObserver } from '@/hooks/use-resize-observer'
import { ChevronDown } from '@/lib/icons'
import { cn } from '@/lib/utils'

interface ExpandableBlockProps {
  children: ReactNode
  className?: string
  onScrollbarSizeChange?: (size: ScrollbarSize) => void
}

export interface ScrollbarSize {
  inline: number
}

const NO_SCROLLBAR = { inline: 0 } satisfies ScrollbarSize

export function ExpandableBlock({ children, className, onScrollbarSizeChange }: ExpandableBlockProps) {
  const innerRef = useRef<HTMLDivElement>(null)
  const scrollbarSizeRef = useRef<ScrollbarSize>(NO_SCROLLBAR)
  const [expanded, setExpanded] = useState(false)
  const [overflowing, setOverflowing] = useState(false)
  const [scrollbarSize, setScrollbarSize] = useState<ScrollbarSize>(NO_SCROLLBAR)

  // Measure inside ResizeObserver timing only (layout is clean there). A
  // synchronous mount-time scrollHeight read forces a reflow per instance,
  // and a tool-heavy transcript mounts dozens of these on a session switch.
  const measure = useCallback(() => {
    const el = innerRef.current

    if (el) {
      setOverflowing(el.scrollHeight > 121)

      // Overlay scrollbars consume no layout space (0); classic Linux/Windows
      // bars do. Preserve the platform scrollbar exactly as rendered and move
      // controls only by the gutter it actually occupies.
      const next = {
        inline: Math.max(0, el.offsetWidth - el.clientWidth)
      }

      const previous = scrollbarSizeRef.current

      if (next.inline !== previous.inline) {
        scrollbarSizeRef.current = next
        setScrollbarSize(next)
        onScrollbarSizeChange?.(next)
      }
    }
  }, [onScrollbarSizeChange])

  useResizeObserver(measure, innerRef)

  return (
    <div className="relative">
      <div
        className={cn(
          // Keep platform overlay bars (no always-on authored track). The
          // measured gutter below handles platforms that render these as
          // classic scrollbars instead.
          'scrollbar-overlay overflow-y-auto overflow-x-auto',
          expanded ? 'max-h-[40dvh]' : 'max-h-[7.5rem]',
          className
        )}
        ref={innerRef}
      >
        {children}
      </div>
      {overflowing && (
        // The fade is a pure overflow cue and must not intercept pointer events:
        // it spans the full bottom edge (over the horizontal scrollbar of a wide
        // code block AND the block's last line), so making it clickable killed
        // both sideways scrolling and text selection. Keep the fade
        // `pointer-events-none` and pin the only clickable target — a compact
        // toggle — to the right edge, clear of the draggable scrollbar track.
        <div className="pointer-events-none absolute inset-x-0 bottom-0 flex h-7 justify-end bg-linear-to-t from-[var(--expandable-fade-from,var(--ui-chat-surface-background))] to-transparent">
          <button
            aria-expanded={expanded}
            aria-label={expanded ? 'Collapse' : 'Expand'}
            className="pointer-events-auto flex h-7 w-9 cursor-pointer items-end justify-center pb-1 text-muted-foreground/70 transition-colors hover:text-foreground"
            onClick={() => setExpanded(v => !v)}
            style={{ marginRight: scrollbarSize.inline }}
            type="button"
          >
            <ChevronDown className={cn('size-3.5 transition-transform', expanded && 'rotate-180')} />
          </button>
        </div>
      )}
    </div>
  )
}
