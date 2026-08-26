/** Phone-only layout policy kept pure so touch regressions have fast coverage. */
// Keep preview splits out of Fold/tablet-width mobile renderers too. A wide
// physical screen can still be a touch-first 900–1000 CSS-pixel WebView.
export const MOBILE_PREVIEW_MAX_VIEWPORT_PX = 1024

export type MobileDrawer = 'files' | 'sessions' | null

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
