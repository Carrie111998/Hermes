/**
 * Conversation-only text size.
 *
 * This is deliberately separate from Electron zoom: people can enlarge the
 * transcript without making sidebars, settings, buttons, and window chrome
 * consume more space. The value is persisted in renderer-local storage and
 * applied through one root CSS custom property used only by human/assistant
 * prose.
 */

import { atom } from 'nanostores'

import { persistString, storedString } from '@/lib/storage'

export const CONVERSATION_FONT_SIZE_STORAGE_KEY = 'hermes.desktop.conversation-font-size.v1'
export const CONVERSATION_FONT_SIZE_CSS_VAR = '--conversation-prose-font-size'

export const MIN_CONVERSATION_FONT_SIZE = 13
export const MAX_CONVERSATION_FONT_SIZE = 24
export const DEFAULT_CONVERSATION_FONT_SIZE = 13

export function clampConversationFontSize(value: number): number {
  if (!Number.isFinite(value)) {
    return DEFAULT_CONVERSATION_FONT_SIZE
  }

  return Math.min(MAX_CONVERSATION_FONT_SIZE, Math.max(MIN_CONVERSATION_FONT_SIZE, Math.round(value)))
}

function readConversationFontSize(): number {
  const stored = Number(storedString(CONVERSATION_FONT_SIZE_STORAGE_KEY))

  return Number.isFinite(stored) && stored > 0
    ? clampConversationFontSize(stored)
    : DEFAULT_CONVERSATION_FONT_SIZE
}

export const $conversationFontSize = atom<number>(
  typeof window === 'undefined' ? DEFAULT_CONVERSATION_FONT_SIZE : readConversationFontSize()
)

export function setConversationFontSize(size: number): void {
  $conversationFontSize.set(clampConversationFontSize(size))
}

if (typeof window !== 'undefined') {
  $conversationFontSize.subscribe(size => {
    persistString(CONVERSATION_FONT_SIZE_STORAGE_KEY, String(size))
    document.documentElement.style.setProperty(CONVERSATION_FONT_SIZE_CSS_VAR, `${size}px`)
  })
}
