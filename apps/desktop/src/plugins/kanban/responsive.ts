import { useEffect, useState } from 'react'

export const KANBAN_DESKTOP_BREAKPOINT = 768
export const KANBAN_DRAWER_DESKTOP_WIDTH = 416
export const KANBAN_HORIZONTAL_GUTTER = 32

export interface KanbanViewportGeometry {
  desktop: boolean
  drawerWidth: number
  laneWidth: number
  viewportWidth: number
}

export function kanbanViewportGeometry(viewportWidth: number): KanbanViewportGeometry {
  const width = Math.max(0, viewportWidth)
  const desktop = width >= KANBAN_DESKTOP_BREAKPOINT

  return {
    desktop,
    drawerWidth: desktop ? Math.min(KANBAN_DRAWER_DESKTOP_WIDTH, width) : width,
    laneWidth: desktop ? 256 : Math.max(0, width - KANBAN_HORIZONTAL_GUTTER),
    viewportWidth: width
  }
}

export function useKanbanViewportGeometry(element: HTMLElement | null = null): KanbanViewportGeometry {
  const [geometry, setGeometry] = useState(() => kanbanViewportGeometry(element?.clientWidth ?? window.innerWidth))

  useEffect(() => {
    const update = () => setGeometry(kanbanViewportGeometry(element?.clientWidth ?? window.innerWidth))

    update()
    window.addEventListener('resize', update)
    const observedElement = element
    const observer = observedElement && typeof ResizeObserver !== 'undefined' ? new ResizeObserver(update) : null

    if (observer && observedElement) {observer.observe(observedElement)}

    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', update)
    }
  }, [element])

  return geometry
}
