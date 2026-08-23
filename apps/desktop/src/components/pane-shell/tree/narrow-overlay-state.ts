import { atom, computed, type ReadableAtom } from 'nanostores'

import { $registryVersion, registry } from '@/contrib/registry'

import { allPaneIds } from './model'
import { $hiddenTreePanes, $layoutTree, $narrowViewport, $paneVisible } from './store'

export interface NarrowOverlayReveal {
  id: string
  pinned: boolean
}

/** The single authority for the transient pane currently rendered over the
 * narrow layout. Keeping this outside React lets shell chrome describe what is
 * actually on screen without mirroring component state through an effect. */
export const $narrowOverlayReveal = atom<NarrowOverlayReveal | null>(null)

export function setNarrowOverlayReveal(reveal: NarrowOverlayReveal | null) {
  $narrowOverlayReveal.set(reveal)
}

export function updateNarrowOverlayReveal(update: (current: NarrowOverlayReveal | null) => NarrowOverlayReveal | null) {
  $narrowOverlayReveal.set(update($narrowOverlayReveal.get()))
}

const effectiveVisibilityCache = new Map<string, ReadableAtom<boolean>>()

const paneContribution = (paneId: string) => registry.getArea('panes').find(pane => pane.id === paneId)

const paneIsNarrowOverlayEligible = (
  paneId: string,
  tree: ReturnType<typeof $layoutTree.get>,
  hiddenPanes: ReadonlySet<string>
) => Boolean(tree && !hiddenPanes.has(paneId) && allPaneIds(tree).includes(paneId))

/** Reactive on-screen visibility. Collapsible panes leave the docked grid on a
 * narrow viewport, so their eligible revealed overlay — not their docked tree
 * slot — is authoritative there. Wide and non-collapsible panes retain tree
 * semantics. */
export function $paneEffectivelyVisible(paneId: string): ReadableAtom<boolean> {
  let cached = effectiveVisibilityCache.get(paneId)

  if (!cached) {
    cached = computed(
      [$narrowViewport, $narrowOverlayReveal, $paneVisible(paneId), $registryVersion, $layoutTree, $hiddenTreePanes],
      (narrow, reveal, dockedVisible, _registryVersion, tree, hiddenPanes) => {
        if (!narrow) {
          return dockedVisible
        }

        const contribution = paneContribution(paneId)

        if (!contribution) {
          return false
        }

        const collapsible = Boolean((contribution.data as { collapsible?: boolean } | undefined)?.collapsible)

        return collapsible
          ? reveal?.id === paneId && paneIsNarrowOverlayEligible(paneId, tree, hiddenPanes)
          : dockedVisible
      }
    )
    effectiveVisibilityCache.set(paneId, cached)
  }

  return cached
}
