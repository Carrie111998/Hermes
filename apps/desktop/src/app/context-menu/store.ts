import { atom } from 'nanostores'

import type { TerminalMenuHandle } from '@/app/right-sidebar/terminal/terminal-context-menu'

import type { ContextMenuDomTarget } from './target'

/** Spell-check facts for the open editable menu. They can arrive AFTER the
 *  menu opens by two routes: (a) the main-process `context-menu` event, which
 *  fires after the DOM gesture (native path — flaky on Linux for
 *  contenteditable), or (b) the renderer-side engine, which computes the
 *  click word and suggestions directly. `anchor` rides only the renderer
 *  path and is never sent over IPC. */
export interface SpellcheckContext {
  misspelledWord: string
  suggestions: string[]
  /** Where the click word lives, for the replace action after the menu
   *  closes. Kept out of IPC: main only ever sees the serializable fields. */
  anchor?: {
    editable: HTMLElement
    start: number
    end: number
    textNode?: Text | null
  }
}

/** What the guest page reported for the click, straight off the webview's
 *  `context-menu` event. Unlike the DOM shape, spell-check facts ride along
 *  immediately — the guest event IS the Chromium report. */
export interface GuestMenuParams {
  /** Chromium's own availability verdict for the edit verbs at the click
   *  point. This is what grays out cut/copy/paste/select-all — the same
   *  source the native menu used. */
  editFlags: {
    canCopy: boolean
    canCut: boolean
    canPaste: boolean
    canSelectAll: boolean
  }
  dictionarySuggestions: string[]
  hasImageContents: boolean
  isEditable: boolean
  linkURL: string
  misspelledWord: string
  selectionText: string
  srcURL: string
}

/** Verbs the preview pane binds over its webview element (and the guest IPC
 *  for the two things the tag cannot do: image bytes and the dictionary). */
export interface GuestMenuHandle {
  addToDictionary: (word: string) => void
  copyImage: () => void
  editCommand: (command: 'copy' | 'cut' | 'paste' | 'selectAll') => void
  inspectElement: () => void
  replaceMisspelling: (word: string) => void
}

export type OpenContextMenu =
  | {
      kind: 'dom'
      x: number
      y: number
      target: ContextMenuDomTarget
      spellcheck: SpellcheckContext | null
    }
  | {
      kind: 'guest'
      x: number
      y: number
      params: GuestMenuParams
      guest: GuestMenuHandle
    }
  | {
      kind: 'terminal'
      x: number
      y: number
      terminal: TerminalMenuHandle
      /** Whether the clipboard held text when the menu opened (grays out
       *  Paste). Arrives async right after open; false until then. Unlike
       *  the dom menu — whose paste runs webContents.paste() in main and
       *  must NOT depend on this probe (#91553) — the terminal paste item
       *  inserts the readClipboard() text itself, so its gate and its
       *  action share one mechanism. */
      clipboardHasText: boolean
    }

/** The one open context menu, or null. A single atom because two context
 *  menus can never be open at once. */
export const $contextMenu = atom<null | OpenContextMenu>(null)

/** Read the clipboard and flag the OPEN terminal menu when text is
 *  available. The read is an IPC round-trip, so the menu opens first
 *  (empty-clipboard verdict) and the flag lands a tick later — same
 *  late-fact pattern as spellcheck. Guarded by identity: a stale read
 *  never flags a newer menu. */
function probeClipboard(opened: Extract<OpenContextMenu, { kind: 'terminal' }>): void {
  void window.hermesDesktop
    ?.readClipboard?.()
    .then((text: string) => {
      const current = $contextMenu.get()

      if (current === opened && current.kind === 'terminal' && text) {
        $contextMenu.set({ ...current, clipboardHasText: true })
      }
    })
    .catch(() => undefined)
}

export function openDomContextMenu(x: number, y: number, target: ContextMenuDomTarget): void {
  $contextMenu.set({ kind: 'dom', x, y, target, spellcheck: null })
}

export function openGuestContextMenu(x: number, y: number, params: GuestMenuParams, guest: GuestMenuHandle): void {
  $contextMenu.set({ kind: 'guest', x, y, params, guest })
}

export function openTerminalContextMenu(x: number, y: number, terminal: TerminalMenuHandle): void {
  const opened: OpenContextMenu = { kind: 'terminal', x, y, terminal, clipboardHasText: false }

  $contextMenu.set(opened)

  if (terminal.paste) {
    probeClipboard(opened)
  }
}

export function closeContextMenu(): void {
  $contextMenu.set(null)
}

/** Attach late-arriving spell-check facts to the open editable menu. Ignored
 *  when the menu already closed or the click was not in an editable. A
 *  `isCurrent` guard ties the async renderer-side compute to the exact open
 *  gesture (a fast second right-click must not pollute a newer menu).
 *  When the open menu already carries richer facts (renderer engine with
 *  suggestions + anchor), a bare native payload never clobbers it. */
export function augmentSpellcheck(payload: SpellcheckContext, isCurrent?: () => boolean): void {
  const open = $contextMenu.get()

  if (isCurrent && !isCurrent()) {
    return
  }

  if (!open || open.kind !== 'dom' || !open.target.editable || !payload.misspelledWord) {
    return
  }

  const existing = open.spellcheck

  if (existing?.suggestions?.length) {
    // Keep the richer renderer-side facts (suggestions + anchor).
    return
  }

  $contextMenu.set({ ...open, spellcheck: payload })
}
