import { atom } from 'nanostores'

import { PANE_TOGGLE_REVEAL_EVENT } from '@/components/pane-shell'
import { Codecs, persistentAtom } from '@/lib/persisted'

export const RIGHT_PANEL_PANES = ['files', 'review', 'artifacts-pane', 'preview', 'terminal'] as const

export type RightPanelPaneId = (typeof RIGHT_PANEL_PANES)[number]

/**
 * Presentation-only visibility for the whole right tools workspace.
 *
 * Individual features still own whether their tab is open; this flag only
 * remembers whether the shared right-side group is on screen. Keeping those
 * two concerns separate lets a user hide the workspace without closing a
 * preview target or killing a PTY.
 */
export const $rightPanelOpen = persistentAtom('hermes.desktop.rightPanelOpen.v1', true, Codecs.bool)

export const $rightPanelRevealRequest = atom<{ paneId: RightPanelPaneId; sequence: number }>({
  paneId: 'files',
  sequence: 0
})

export function setRightPanelOpen(open: boolean): void {
  $rightPanelOpen.set(open)

  if (!open && typeof window !== 'undefined') {
    for (const paneId of RIGHT_PANEL_PANES) {
      window.dispatchEvent(new CustomEvent(PANE_TOGGLE_REVEAL_EVENT, { detail: { id: paneId, mode: 'close' } }))
    }
  }
}

export function toggleRightPanelOpen(): void {
  setRightPanelOpen(!$rightPanelOpen.get())
}

/** Explicit user/product intent to show one tool. The layout controller is the
 * only layer that translates this request into a tree-tab activation. */
export function requestRightPanelPane(paneId: RightPanelPaneId): void {
  setRightPanelOpen(true)

  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(PANE_TOGGLE_REVEAL_EVENT, { detail: { id: paneId, mode: 'open' } }))
  }

  $rightPanelRevealRequest.set({
    paneId,
    sequence: $rightPanelRevealRequest.get().sequence + 1
  })
}
