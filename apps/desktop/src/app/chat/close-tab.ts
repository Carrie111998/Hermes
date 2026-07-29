import { closeActiveTerminal } from '@/app/right-sidebar/terminal/terminals'
import { findGroupOfPane } from '@/components/pane-shell/tree/model'
import {
  $activeTreeGroup,
  $hiddenTreePanes,
  $layoutTree,
  closeFocusedSessionTab,
  closeTreePane
} from '@/components/pane-shell/tree/store'
import { isFocusWithin } from '@/lib/keybinds/combo'
import { $previewTabs, closeActiveRightRailTab } from '@/store/preview'
import { $rightPanelOpen, RIGHT_PANEL_PANES } from '@/store/right-panel'
import { closeSessionTile, nextSessionTileForWorkspace } from '@/store/session-states'

/**
 * ⌘W — close the tab of the context you're in, by precedence:
 *   1. a focused terminal → its active terminal tab,
 *   2. the focused right-tools function (Preview/Terminal close one child tab;
 *      the other functions close their top-level tab),
 *   3. the focused chat zone → its active tab (a session tile stacked into it).
 *   4. the workspace tab itself, when session tabs are stacked with it:
 *      the workspace can't close, so ⌘W shifts the NEXT session tab into main
 *      (loads it as the primary + drops its now-redundant tile).
 * Returns false when nothing closes, so ⌘W is a no-op — it never closes the
 * window (a bare workspace stays put). Shared by the keyboard path (Win/Linux)
 * and the macOS menu-accelerator IPC.
 *
 * Steps 3-4 follow the same focused zone ⌘1…⌘9 indexes, so a second chat zone
 * with its own tab strip closes ITS tab instead of main's.
 *
 * `loadSessionIntoWorkspace` carries the app's route-based "load this session
 * into main" (the two call sites have router access); omitting it disables the
 * step-4 promotion (⌘W stays the pre-existing no-op on the main tab).
 */
export function closeActiveTab(loadSessionIntoWorkspace?: (storedSessionId: string) => void): boolean {
  if (isFocusWithin('[data-terminal]')) {
    closeActiveTerminal()

    return true
  }

  const tree = $layoutTree.get()

  const rightGroups = tree
    ? RIGHT_PANEL_PANES.map(paneId => findGroupOfPane(tree, paneId)).filter(
        (group, index, groups) => group && groups.findIndex(candidate => candidate?.id === group.id) === index
      )
    : []

  const rightGroup = rightGroups.find(
    group => group && ($activeTreeGroup.get() === group.id || isFocusWithin(`[data-tree-group="${group.id}"]`))
  )

  const rightGroupOwnsFocus = Boolean($rightPanelOpen.get() && rightGroup)

  if (rightGroupOwnsFocus && rightGroup) {
    const hidden = $hiddenTreePanes.get()

    const activeRightPane = hidden.has(rightGroup.active)
      ? rightGroup.panes.find(paneId => !hidden.has(paneId))
      : rightGroup.active

    if (!activeRightPane) {
      return false
    }

    if (activeRightPane === 'terminal') {
      closeActiveTerminal()

      return true
    }

    if (activeRightPane === 'preview' && $previewTabs.get().length > 0) {
      return closeActiveRightRailTab()
    }

    closeTreePane(activeRightPane)

    return true
  }

  // A closeable tab in the focused chat zone (a session tile that's the active
  // tab) closes outright; the uncloseable workspace tab falls through.
  if (closeFocusedSessionTab()) {
    return true
  }

  // The main (workspace) tab is active and can't be closed — but if session
  // tabs are stacked with it, ⌘W shifts the next one into the main tab: drop
  // its tile (the session stays alive, no busy-close prompt) and load it into
  // main. Order matters — close the tile FIRST so the selection homes to the
  // workspace instead of re-fronting the tile.
  if (loadSessionIntoWorkspace) {
    const next = nextSessionTileForWorkspace()

    if (next) {
      closeSessionTile(next)
      loadSessionIntoWorkspace(next)

      return true
    }
  }

  return false
}
