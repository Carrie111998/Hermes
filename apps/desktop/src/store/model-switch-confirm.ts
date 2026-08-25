import { atom } from 'nanostores'

import type { ModelSelection } from '@/app/shell/model-menu-panel'

export interface PendingModelSwitchConfirm {
  /** Raw config.set value that bounced on a selection guard (model + flags). */
  rawValue: string
  message: string
  provider: string
  model: string
  /** Session the switch targets — null means the profile draft / global default. */
  sessionId: null | string
  /** True when confirming would rewrite model.default in config.yaml. */
  persistsAsDefault: boolean
}

/**
 * Selection-guard confirmation for composer/picker model switches.
 *
 * The gateway's `config.set model` runs the cost + data-policy guards BEFORE
 * applying, and answers `confirm_required` when a pick needs an explicit OK
 * (e.g. Meta's contributor tier, which trains on your prompts). The picker has
 * no inline way to ask, so the pending switch parks here; the mounted dialog
 * renders the backend's message and re-sends the exact same value flagged as
 * confirmed. Cancel just clears this state — the composer keeps its previous
 * selection.
 */
export const $pendingModelSwitchConfirm = atom<PendingModelSwitchConfirm | null>(null)

export function setPendingModelSwitchConfirm(
  pending: PendingModelSwitchConfirm | null
): void {
  $pendingModelSwitchConfirm.set(pending)
}

/** Convenience constructor for the selection object the retry resends. */
export function selectionFromPending(
  pending: PendingModelSwitchConfirm
): ModelSelection {
  const selection: ModelSelection = { provider: pending.provider, model: pending.model }
  if (pending.sessionId !== null) {
    ;(selection as { sessionId?: null | string }).sessionId = pending.sessionId
  }
  return selection
}
