import { type PointerEvent as ReactPointerEvent, useCallback, useRef, useState } from 'react'

/** Components-area width bounds (px). The tree is a persistent navigation
 *  surface, so it keeps a floor; the preview port will push against the max. */
export const COMPONENTS_MIN_WIDTH = 200
export const COMPONENTS_DEFAULT_WIDTH = 320
export const COMPONENTS_MAX_WIDTH = 560

/**
 * Drag-resizable components-area width, shared by the session tile and the
 * primary workspace pane. The seam sits between the chat surface and the
 * components area; dragging it adjusts the components width within bounds.
 *
 * Module-level drag bookkeeping instead of per-callbacks: the window
 * listeners are registered once per drag, so the seam stays responsive even
 * while the width state churns on every pointermove.
 */
export function useComponentsWidth() {
  const [componentsWidth, setComponentsWidth] = useState(COMPONENTS_DEFAULT_WIDTH)
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null)

  const startResize = useCallback((e: ReactPointerEvent<HTMLElement>) => {
    e.preventDefault()
    dragRef.current = { startX: e.clientX, startWidth: componentsWidth }

    const onMove = (ev: PointerEvent) => {
      const drag = dragRef.current

      if (!drag) {
        return
      }

      const next = drag.startWidth + (drag.startX - ev.clientX)
      setComponentsWidth(Math.min(COMPONENTS_MAX_WIDTH, Math.max(COMPONENTS_MIN_WIDTH, next)))
    }

    const onUp = () => {
      dragRef.current = null
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }

    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }, [componentsWidth])

  return { componentsWidth, startResize }
}

/**
 * The drag seam between chat and components area. Same visual language as the
 * layout tree's sashes: a persistent hairline + hover band. `role="separator"`
 * + `cursor-col-resize` advertise draggability; the generous 11px hit area
 * (9px band + 1px hairlines) makes the target easy to catch.
 */
export function ComponentsResizeSeam({ onPointerDown }: { onPointerDown: (e: ReactPointerEvent<HTMLElement>) => void }) {
  return (
    <div
      aria-hidden
      className="group relative z-20 w-[11px] shrink-0 cursor-col-resize [-webkit-app-region:no-drag]"
      onPointerDown={onPointerDown}
      role="separator"
    >
      {/* Persistent hairline: same token as PaneShell's divider sash so every
          seam reads identically. Sits at 0.1, comes to full on hover. */}
      <span className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-(--ui-stroke-secondary) opacity-10 transition-opacity duration-100 group-hover:opacity-100" />
      <span className="absolute inset-y-0 left-1/2 w-(--vscode-sash-hover-size,0.25rem) -translate-x-1/2 bg-(--ui-sash-hover-border) opacity-0 transition-opacity duration-100 group-hover:opacity-100" />
    </div>
  )
}
