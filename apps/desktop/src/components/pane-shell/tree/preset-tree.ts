/**
 * User layout presets are geometry (splits, rails, weights), not a snapshot of
 * live conversations. Saving `session-tile:<id>` / `preview-tile:*` / `route-tile:*`
 * into `hermes.desktop.layoutPresets.v2` remounts those chats on apply
 * (`session.resume` → agent init) across profiles, then the tile WS drops and
 * the gateway `ws_orphan_reap`s the runtime (#94260).
 *
 * #92818 / PRs that strip preview tiles from the *persisted live tree* do not
 * cover this path: presets clone the live tree via `saveLayoutPresetTree`.
 */

import { type LayoutNode, normalize } from './model'

/** Panes that must never ride along in a named layout preset. */
export const PRESET_EXCLUDED_PANE_PREFIXES = ['session-tile:', 'preview-tile:', 'route-tile:'] as const

export const isPresetExcludedPaneId = (paneId: string): boolean =>
  PRESET_EXCLUDED_PANE_PREFIXES.some(prefix => paneId.startsWith(prefix))

/**
 * Pure copy of `tree` without live/ephemeral tile panes. Empty groups collapse
 * via `normalize`. Returns `null` only when nothing structural remains.
 */
export function stripPresetLivePanes(tree: LayoutNode): LayoutNode | null {
  const walk = (node: LayoutNode): LayoutNode => {
    if (node.type === 'group') {
      const panes = node.panes.filter(paneId => !isPresetExcludedPaneId(paneId))

      if (panes.length === node.panes.length) {
        return node
      }

      return { ...node, panes, active: panes.includes(node.active) ? node.active : (panes[0] ?? '') }
    }

    const children = node.children.map(walk)

    if (children.every((child, i) => child === node.children[i])) {
      return node
    }

    return { ...node, children }
  }

  return normalize(walk(tree))
}
