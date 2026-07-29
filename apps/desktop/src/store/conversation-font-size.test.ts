import { beforeEach, describe, expect, it } from 'vitest'

import {
  $conversationFontSize,
  clampConversationFontSize,
  CONVERSATION_FONT_SIZE_CSS_VAR,
  CONVERSATION_FONT_SIZE_STORAGE_KEY,
  setConversationFontSize
} from './conversation-font-size'

describe('conversation font size', () => {
  beforeEach(() => {
    window.localStorage.clear()
    document.documentElement.style.removeProperty(CONVERSATION_FONT_SIZE_CSS_VAR)
  })

  it('clamps and rounds values to the supported readable range', () => {
    expect(clampConversationFontSize(8)).toBe(13)
    expect(clampConversationFontSize(16.6)).toBe(17)
    expect(clampConversationFontSize(30)).toBe(24)
    expect(clampConversationFontSize(Number.NaN)).toBe(13)
  })

  it('applies and persists the prose size without changing Chromium zoom', () => {
    setConversationFontSize(16)

    expect($conversationFontSize.get()).toBe(16)
    expect(document.documentElement.style.getPropertyValue(CONVERSATION_FONT_SIZE_CSS_VAR)).toBe('16px')
    expect(window.localStorage.getItem(CONVERSATION_FONT_SIZE_STORAGE_KEY)).toBe('16')
  })
})
