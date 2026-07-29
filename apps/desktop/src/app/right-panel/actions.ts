import { $terminalTakeover, setTerminalTakeover } from '@/app/right-sidebar/store'
import { findGroupOfPane } from '@/components/pane-shell/tree/model'
import { $layoutTree } from '@/components/pane-shell/tree/store'
import { $fileBrowserOpen, setFileBrowserOpen } from '@/store/layout'
import { $reviewOpen, hideReviewPane, openReview } from '@/store/review'
import { $rightPanelOpen, toggleRightPanelOpen } from '@/store/right-panel'

export { toggleRightPanelOpen }

export function isRightPanelPaneActive(paneId: string): boolean {
  const tree = $layoutTree.get()

  return Boolean($rightPanelOpen.get() && tree && findGroupOfPane(tree, paneId)?.active === paneId)
}

export function toggleTerminalPanel(): void {
  if ($terminalTakeover.get() && isRightPanelPaneActive('terminal')) {
    setTerminalTakeover(false)

    return
  }

  setTerminalTakeover(true)
}

export function toggleFilesPanel(): void {
  // If the entire right side is hidden, the Files owner is still "open" so
  // its tree state survives. Treat the shortcut as a reveal in that case,
  // not as a request to turn the already-invisible owner off.
  if ($fileBrowserOpen.get() && $rightPanelOpen.get()) {
    setFileBrowserOpen(false)

    return
  }

  setFileBrowserOpen(true)
}

export function toggleReviewPanel(): void {
  if ($reviewOpen.get() && isRightPanelPaneActive('review')) {
    hideReviewPane()

    return
  }

  openReview()
}
