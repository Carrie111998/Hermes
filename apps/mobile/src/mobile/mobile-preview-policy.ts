export interface MobilePreviewTab {
  id: string
  target: {
    source: string
    url: string
  }
}

/**
 * An agent/user opening a new preview (or reusing Browser for a new address)
 * should present it. Removing a tab must not reopen a preview the user closed.
 */
export function shouldOpenMobilePreview(previous: readonly MobilePreviewTab[], next: readonly MobilePreviewTab[]): boolean {
  if (next.length === 0) return false

  // openPreview replaces the tab array even when it re-fronts the same address,
  // while an explicit close always reduces the tab count. The owning effect only
  // evaluates this when the array reference actually changed.
  return next.length >= previous.length
}

/** The app WebView is HTTPS-only; unsupported/special schemes never become an inline guest. */
export function canEmbedMobilePreviewUrl(value: string): boolean {
  try {
    return new URL(value).protocol === 'https:'
  } catch {
    return false
  }
}
