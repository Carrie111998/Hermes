export interface MobileWorkspacePaneSource {
  data?: unknown
  id: string
  title?: string
}

function isCollapsible(data: unknown): boolean {
  return typeof data === 'object' && data !== null && (data as { collapsible?: unknown }).collapsible === true
}

export interface MobileWorkspacePane {
  id: string
  title: string
}

/** Collapsible panes are the complete touch workspace menu, not only sessions. */
export function mobileWorkspacePanes(
  panes: readonly MobileWorkspacePaneSource[],
  paneIdsInLayout: ReadonlySet<string>,
  hiddenPaneIds: ReadonlySet<string>,
): MobileWorkspacePane[] {
  return panes
    .filter(pane => isCollapsible(pane.data) && paneIdsInLayout.has(pane.id) && !hiddenPaneIds.has(pane.id))
    .map(pane => ({ id: pane.id, title: pane.title ?? pane.id }))
}
