import { describe, expect, it } from 'vitest'

import {
  MOBILE_PREVIEW_MAX_VIEWPORT_PX,
  mobileDrawerForPane,
  shouldDismissDrawerAfterSessionChange,
  shouldRevealPaneForDrawerChange,
  shouldSuppressPreviewOnMobile,
} from './mobile-policy'

describe('mobile interaction policy', () => {
  it('keeps drawer state synchronized with both tree pane IDs and titlebar aliases', () => {
    expect(mobileDrawerForPane('sessions')).toBe('sessions')
    expect(mobileDrawerForPane('chat-sidebar')).toBe('sessions')
    expect(mobileDrawerForPane('files')).toBe('files')
    expect(mobileDrawerForPane('file-browser')).toBe('files')
    expect(mobileDrawerForPane('review')).toBeNull()
  })

  it('dismisses an open drawer after a session selection changes', () => {
    expect(shouldDismissDrawerAfterSessionChange(true)).toBe(true)
    expect(shouldDismissDrawerAfterSessionChange(false)).toBe(false)
  })

  it('reveals a pane only when its drawer opens', () => {
    expect(shouldRevealPaneForDrawerChange(true)).toBe(true)
    expect(shouldRevealPaneForDrawerChange(false)).toBe(false)
  })

  it('suppresses split previews across Fold-class touch viewports', () => {
    expect(shouldSuppressPreviewOnMobile(MOBILE_PREVIEW_MAX_VIEWPORT_PX, 1)).toBe(true)
    expect(shouldSuppressPreviewOnMobile(MOBILE_PREVIEW_MAX_VIEWPORT_PX + 1, 1)).toBe(false)
    expect(shouldSuppressPreviewOnMobile(412, 0)).toBe(false)
  })
})
