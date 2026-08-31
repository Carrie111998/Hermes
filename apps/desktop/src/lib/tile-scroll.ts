/**
 * Keep a wheel/trackpad gesture inside the tile it started on.
 *
 * Multi-tile layouts stack several overflow surfaces under one layout tree.
 * Without stopping the event at the hovered scroller, the same delta chains
 * into sibling tiles (or a parent of all of them) and every pane moves.
 */
export function isolateTileWheel(event: { stopPropagation: () => void }): void {
  event.stopPropagation()
}
