// Responsive horizontal gutter for primary content bodies (settings right side,
// skills, artifacts, command center / sessions). Ratio-based so it scales with
// the window, but clamped so it never collapses on narrow widths or runs away
// on ultrawide displays. Headers/tabs intentionally keep their own tighter
// padding.
//
// NOTE: these must stay literal strings — Tailwind's scanner only picks up
// complete class names, so do not build them via template interpolation.
export const PAGE_INSET_X = 'px-[clamp(1.25rem,4vw,4rem)]'

// Matching negative inline-margin to bleed an element (e.g. a sticky header bar)
// out to the gutter edges before re-applying PAGE_INSET_X.
export const PAGE_INSET_NEG_X = '-mx-[clamp(1.25rem,4vw,4rem)]'

// Readable cap for overlay "inner page" bodies (settings, command center). Wide
// enough to breathe, tight enough that content doesn't sprawl on ultrawide
// displays. Pair with `mx-auto w-full` to center within the pane. Literal string
// for Tailwind's scanner (see PAGE_INSET_X note).
export const PAGE_MAX_W = 'max-w-[75rem]'

// Below this viewport width a docked sidebar leaves no room for content, so both
// rails auto-collapse into the hover-reveal overlay. Single source of truth for
// the responsive collapse point.
// A Fold-class Android WebView is commonly about 900–1000 CSS px wide when
// unfolded. Two desktop rails still leave the chat/settings detail below its
// usable width, so treat this as a drawer layout rather than a squeezed desktop.
export const SIDEBAR_COLLAPSE_BREAKPOINT_PX = 1024
// Landscape phones can be wider than 768px while offering only ~360px of
// vertical space; collapse rails there as well so the chat/composer keeps a
// usable touch layout rather than inheriting a cramped desktop grid.
export const SIDEBAR_COMPACT_LANDSCAPE_MEDIA_QUERY = '(max-height: 500px) and (orientation: landscape)'
export const SIDEBAR_COLLAPSE_MEDIA_QUERY = `(max-width: ${SIDEBAR_COLLAPSE_BREAKPOINT_PX}px), ${SIDEBAR_COMPACT_LANDSCAPE_MEDIA_QUERY}`

/** A native mobile shell stays touch-first at every physical width. An unfolded
 * Fold can exceed the CSS breakpoint while still requiring drawer navigation,
 * not a persistent desktop rail. */
export function shouldUseNarrowPaneLayout({
  mediaQueryMatches,
  mobileRenderer
}: {
  mediaQueryMatches: boolean
  mobileRenderer: boolean
}): boolean {
  return mediaQueryMatches || mobileRenderer
}
