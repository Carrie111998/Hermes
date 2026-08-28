/**
 * PIN TYPES — the shape a user's comment travels in, from the guest page to
 * the composer to the model.
 *
 * Kept apart from anchor.ts because anchor.ts is stringified into the guest
 * page and must stay free of anything it does not need.
 */

import type { PinAnchor } from './anchor'

/** What the pin is fastened to. A region pin has no element — it is a box the
 *  user dragged over an image, a chart, or a canvas, where there is no
 *  meaningful node to name. */
export type PinKind = 'element' | 'region'

export interface PreviewPin {
  /** What the user wrote. Empty until they finish the bubble. */
  comment: string
  /** When it was placed, for stable ordering in the list. */
  createdAt: number
  id: string
  kind: PinKind
  /** Which rung of the ladder found it last, e.g. `selector`, `role+label`.
   *  Surfaced in the list so a weak re-attach is visible rather than silent. */
  matchedBy?: string
  /** True when the ladder refused to place it after a reload. The comment is
   *  kept — it is the user's writing — but it no longer points anywhere. */
  orphaned?: boolean
  /** The page it was placed on. A pin does not follow the user to another URL. */
  pageUrl: string
  /** Absent for a region pin. */
  anchor?: PinAnchor
  /** Region pins only: fractions of the document box, like PinAnchor.rect. */
  region?: { h: number; w: number; x: number; y: number }
  resolved: boolean
  /** Short human label for the list: the element's accessible name, or the
   *  region's size. */
  target: string
}

/** What the in-page engine reports back after a placement or a re-attach. */
export interface PinEngineReport {
  /** Whether annotation mode is on. The engine owns this, not the panel: a
   *  navigation resets it, and the panel would otherwise show a toggle that no
   *  longer reflects the page. */
  armed: boolean
  pins: PreviewPin[]
  url: string
}
