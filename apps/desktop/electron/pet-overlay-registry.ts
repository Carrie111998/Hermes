// Per-profile pet overlay window registry + IPC sender-role enforcement.
//
// Extracted from main.ts so the security-critical sender checks are unit-testable
// (cf. zoom.ts, link-title-window.ts). The shared preload exposes every
// petOverlay.* API to EVERY renderer, so "same process family" is NOT a trust
// boundary: the lifecycle/state channels are primary-renderer-only, and the
// per-window channels derive the profile from `event.sender.id` — never from a
// renderer-supplied field.

/** Normalize a profile key the same way the renderer does (trim, empty→default). */
export function overlayProfileKey(profile) {
  const value = typeof profile === 'string' ? profile.trim() : ''

  return value || 'default'
}

/**
 * True iff the IPC sender is the primary (main) window's webContents — the only
 * legitimate source of the primary-renderer-only channels (open/close/pushState).
 * `primaryWebContentsId` is null when there is no live main window.
 */
export function isPrimarySender(sender, primaryWebContentsId) {
  return primaryWebContentsId != null && sender != null && sender.id === primaryWebContentsId
}

/**
 * One overlay window per profile, plus the reverse webContents.id → profile map
 * used to authenticate a sender. Capture the wcId at creation: webContents is
 * destroyed by the time the window's 'closed' event fires, so it cannot be read
 * there — unregister takes the captured id.
 */
export class PetOverlayRegistry {
  /** profile → overlay window */
  windows = new Map()
  /** webContents.id → profile */
  profileByWebContentsId = new Map()

  /** Track a freshly created overlay; returns the wcId to hand to unregister. */
  register(profile, win) {
    const wcId = win.webContents.id
    this.windows.set(profile, win)
    this.profileByWebContentsId.set(wcId, profile)

    return wcId
  }

  /** Drop a closed overlay using the wcId captured at creation. */
  unregister(profile, wcId) {
    const current = this.windows.get(profile)

    if (!current || current.webContents.id === wcId) {
      this.windows.delete(profile)
    }

    this.profileByWebContentsId.delete(wcId)
  }

  get(profile) {
    return this.windows.get(profile)
  }

  has(profile) {
    return this.windows.has(profile)
  }

  profiles() {
    return [...this.windows.keys()]
  }

  /**
   * The live overlay window owned by the sender, or null when the sender is not
   * a registered overlay (rejects the primary window AND any unknown renderer).
   */
  windowForSender(sender) {
    const profile = this.profileByWebContentsId.get(sender?.id)

    if (!profile) {
      return null
    }

    const win = this.windows.get(profile)

    return win && !win.isDestroyed() ? win : null
  }

  /** The profile a sender's overlay is bound to, or null for an unknown sender. */
  profileForSender(sender) {
    return this.profileByWebContentsId.get(sender?.id) ?? null
  }
}
