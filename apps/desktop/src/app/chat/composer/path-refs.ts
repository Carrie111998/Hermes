/**
 * Bare-path recognition for the composer. Walking into a folder from the `@`
 * popover (Tab) re-types the token as a bare `@apps/desktop/` so the next
 * `complete.path` lists that folder's children. That's right while typing, but
 * a bare token is not a reference: `REFERENCE_PATTERN` only matches
 * `@kind:value`, so a draft sent with one attaches nothing and renders as
 * plain text instead of a chip.
 *
 * So the bare token is promoted to its typed form on the way out — the same
 * shape `url-refs.ts` uses for bare links.
 */
import type { KeyboardEvent } from 'react'

import {
  BARE_PATH_RE,
  barePathRef,
  pathifyRefs
} from '../../../../shared/composer-submit-text'

import { refChipElement, replaceBeforeCaret } from './rich-editor'
import { textBeforeCaret } from './text-utils'

const TYPED_BARE_PATH_RE = new RegExp(`${BARE_PATH_RE.source}$`)

export { barePathRef, pathifyRefs }

/** A plain space finishing a typed `@path` commits it as a chip, so a hand-typed
 *  path chips the same way a picked one does. Returns whether it ran, so a
 *  keydown handler can fall through on anything else. */
export function chipTypedPathOnSpace(event: KeyboardEvent<HTMLDivElement>) {
  if (event.key !== ' ' || event.metaKey || event.ctrlKey || event.altKey) {
    return false
  }

  const editor = event.currentTarget

  // Runs on every space, so bail on the cheap native read before paying for the
  // caret range walk (same guard shape as the trigger detector).
  if (!editor.textContent?.includes('@')) {
    return false
  }

  const before = textBeforeCaret(editor)
  const match = before ? TYPED_BARE_PATH_RE.exec(before) : null
  const token = match?.[0]
  const ref = match?.[1] ? barePathRef(match[1]) : null

  if (!token || !ref) {
    return false
  }

  const directive = ref.match(/^@([^:]+):(.+)$/)

  if (!directive) {
    return false
  }

  const fragment = document.createDocumentFragment()

  fragment.append(refChipElement(directive[1], directive[2]), document.createTextNode(' '))

  return replaceBeforeCaret(editor, token.length, fragment)
}
