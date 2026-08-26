import { describe, expect, it } from 'vitest'

import { shouldUseNarrowPaneLayout, SIDEBAR_COLLAPSE_MEDIA_QUERY } from './layout-constants'

describe('sidebar collapse breakpoint', () => {
  it('collapses both rails before a Fold-class mobile viewport becomes a squeezed desktop', () => {
    expect(SIDEBAR_COLLAPSE_MEDIA_QUERY).toContain('max-width: 1024px')
  })

  it('forces touch drawers for the native mobile renderer even when an unfolded Fold exceeds the CSS breakpoint', () => {
    expect(shouldUseNarrowPaneLayout({ mediaQueryMatches: false, mobileRenderer: true })).toBe(true)
    expect(shouldUseNarrowPaneLayout({ mediaQueryMatches: true, mobileRenderer: false })).toBe(true)
    expect(shouldUseNarrowPaneLayout({ mediaQueryMatches: false, mobileRenderer: false })).toBe(false)
  })

  it('uses the narrow drawer layout for compact landscape phone heights', () => {
    expect(SIDEBAR_COLLAPSE_MEDIA_QUERY).toContain('max-height: 500px')
    expect(SIDEBAR_COLLAPSE_MEDIA_QUERY).toContain('orientation: landscape')
  })
})
