/** Phone-only layout policy kept pure so touch regressions have fast coverage. */
// Keep preview splits out of Fold/tablet-width mobile renderers too. A wide
// physical screen can still be a touch-first 900–1000 CSS-pixel WebView.
export const MOBILE_PREVIEW_MAX_VIEWPORT_PX = 1024

export type MobileDrawer = 'files' | 'sessions' | null

// Keep an inner gutter free for Android's system-back gesture. A swipe must
// begin just inside that gutter and travel far enough horizontally to be an
// intentional drawer gesture, never a diagonal scroll accident.
const SWIPE_SYSTEM_GESTURE_GUTTER_PX = 16
const SWIPE_DRAWER_EDGE_PX = 64
const SWIPE_MIN_HORIZONTAL_DISTANCE_PX = 72
const SWIPE_MAX_VERTICAL_DISTANCE_PX = 48

export interface MobileEdgeSwipe {
  endX: number
  endY: number
  startX: number
  startY: number
  viewportWidth: number
}

/** Resolve a deliberate inner-edge swipe to its drawer, or null for ordinary
 * scrolling / Android system-back territory. */
export function mobileDrawerForEdgeSwipe({ endX, endY, startX, startY, viewportWidth }: MobileEdgeSwipe): MobileDrawer {
  const deltaX = endX - startX
  const deltaY = endY - startY

  if (Math.abs(deltaY) > SWIPE_MAX_VERTICAL_DISTANCE_PX || Math.abs(deltaX) < SWIPE_MIN_HORIZONTAL_DISTANCE_PX) {
    return null
  }

  const leftGestureZone = startX >= SWIPE_SYSTEM_GESTURE_GUTTER_PX && startX <= SWIPE_DRAWER_EDGE_PX
  const rightGestureZone = startX <= viewportWidth - SWIPE_SYSTEM_GESTURE_GUTTER_PX && startX >= viewportWidth - SWIPE_DRAWER_EDGE_PX

  if (leftGestureZone && deltaX > 0) return 'sessions'
  if (rightGestureZone && deltaX < 0) return 'files'
  return null
}

/** A revealed drawer closes only when the swipe travels back toward its edge. */
export function shouldCloseMobileDrawerFromSwipe(drawer: MobileDrawer, deltaX: number, deltaY: number): boolean {
  if (!drawer || Math.abs(deltaY) > SWIPE_MAX_VERTICAL_DISTANCE_PX) return false
  if (drawer === 'sessions') return deltaX <= -SWIPE_MIN_HORIZONTAL_DISTANCE_PX
  return deltaX >= SWIPE_MIN_HORIZONTAL_DISTANCE_PX
}

/** Map the tree's real pane IDs and titlebar aliases back to mobile drawers. */
export function mobileDrawerForPane(paneId: string | undefined): MobileDrawer {
  if (paneId === 'sessions' || paneId === 'chat-sidebar') return 'sessions'
  if (paneId === 'files' || paneId === 'file-browser') return 'files'
  return null
}

/** A session change means the drawer has served its purpose: reveal the chat. */
export function shouldDismissDrawerAfterSessionChange(drawerOpen: boolean): boolean {
  return drawerOpen
}

/** A hidden drawer must stay hidden; only its open transition reveals the pane. */
export function shouldRevealPaneForDrawerChange(drawerOpen: boolean): boolean {
  return drawerOpen
}

/** Desktop split previews cannot stay readable on a handset-sized renderer. */
export function shouldSuppressPreviewOnMobile(viewportWidth: number, previewCount: number): boolean {
  return viewportWidth <= MOBILE_PREVIEW_MAX_VIEWPORT_PX && previewCount > 0
}
