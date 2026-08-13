/**
 * When a lone pane must keep its tab strip (name card + close).
 *
 * Default: a single pane isn't a "tab", so the header auto-hides. Exceptions
 * force it on so a closeable surface never becomes an unclosable dead zone:
 *  - a closeable `placement: 'main'` pane — every mirrored TILE (a session, a
 *    page, a preview) is one, so dragging a tile into a zone of its own keeps
 *    its tab and its ✕
 *  - a pane with an app-owned close action
 *  - a collapse tool panel dragged into its own zone
 */

export interface LoneHeaderChrome {
  placement?: string
  uncloseable?: boolean
}

export function forceLoneHeaderForPanes(
  shown: readonly string[],
  chromeOf: (id: string) => LoneHeaderChrome,
  isCollapsePane: (id: string) => boolean,
  hasPaneCloser: (id: string) => boolean = () => false
): boolean {
  // "This pane can be closed, so it must expose the ✕." Only the uncloseable
  // workspace is exempt; standing side chrome (files / sessions) has neither
  // main placement nor an app-owned close action.
  if (
    shown.some(id => {
      const chrome = chromeOf(id)

      return !chrome.uncloseable && (chrome.placement === 'main' || hasPaneCloser(id))
    })
  ) {
    return true
  }

  return shown.length === 1 && isCollapsePane(shown[0])
}
