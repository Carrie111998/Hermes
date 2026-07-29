import { allPaneIds, findGroup, findGroupOfPane, type LayoutNode } from '@/components/pane-shell/tree/model'
import { RIGHT_PANEL_PANES } from '@/store/right-panel'

/**
 * Recognize only Hermes' former stock default. Layout v2 is user data, so an
 * active non-default preset or any explicitly dragged right feature wins.
 */
export function shouldMigrateLegacyRightPanel(
  tree: LayoutNode,
  activePresetId: string,
  userPlacedPanes: ReadonlySet<string>
): boolean {
  // Opening a session tile legitimately marks the layout "custom" even when
  // the user never touched the right side. Keep named alternative presets
  // intact, but allow default/custom trees through when no right pane was
  // explicitly placed.
  if (
    (activePresetId !== 'default' && activePresetId !== 'custom') ||
    RIGHT_PANEL_PANES.some(id => userPlacedPanes.has(id))
  ) {
    return false
  }

  const orderedPaneIds = allPaneIds(tree)
  const paneIds = new Set(orderedPaneIds)

  const knownStockPane = (id: string) =>
    ['sessions', 'workspace', 'files', 'review', 'artifacts-pane', 'preview', 'terminal', 'logs'].includes(id) ||
    id.startsWith('session-tile:') ||
    id.startsWith('route-tile:')

  // A third-party pane is user layout even if it was silently adopted and
  // never dragged. Leave that tree untouched rather than guessing which new
  // group should inherit the plugin.
  if ([...paneIds].some(id => !knownStockPane(id))) {
    return false
  }

  const rightTools = findGroup(tree, 'grp-right-tools')
  const filesGroup = findGroupOfPane(tree, 'files')
  const functionalPanes = ['review', 'artifacts-pane', 'preview', 'terminal']

  // Current target: Files has its own outer sidebar and therefore must not be
  // a tenant of the functional right-tools tab group. Separation alone is not
  // enough: a persisted intermediate tree can still be Workspace | Files |
  // Preview. Files is current only when it follows every functional pane.
  const filesIndex = orderedPaneIds.indexOf('files')

  const rightToolsAreInsideFiles =
    rightTools &&
    !rightTools.panes.includes('files') &&
    functionalPanes.every(id => rightTools.panes.includes(id)) &&
    functionalPanes.every(id => orderedPaneIds.indexOf(id) < filesIndex)

  if (rightToolsAreInsideFiles && filesGroup?.id === 'grp-files') {
    return false
  }

  // Migrate the first unified-right-panel release: it correctly consolidated
  // the tool functions, but also made Files a peer tab of Preview. The revised
  // default keeps the tool group and splits only Files back to the outer edge.
  if (rightTools?.panes.includes('files') && functionalPanes.every(id => rightTools.panes.includes(id))) {
    return true
  }

  // Also repair the short-lived separated-but-reversed layout. A session tile
  // may have made the preset custom; adoptMissingPanes preserves that tile
  // while the default restores only the right-side ordering.
  if (
    rightTools &&
    filesGroup?.id === 'grp-files' &&
    functionalPanes.every(id => rightTools.panes.includes(id)) &&
    !rightToolsAreInsideFiles
  ) {
    return true
  }

  const hasOldCore = ['sessions', 'workspace', 'files', 'review', 'preview', 'terminal'].every(id => paneIds.has(id))
  const oldTerminalGroup = findGroupOfPane(tree, 'terminal')

  return (
    hasOldCore &&
    findGroup(tree, 'grp-main') !== null &&
    filesGroup?.id === 'grp-files' &&
    oldTerminalGroup?.id === 'grp-terminal'
  )
}
