/**
 * Guard the TUI's empty-bracketed-paste → clipboard.paste probe.
 *
 * Terminals (notably Ghostty with mouse_tracking=all) can emit empty
 * bracketed-paste markers from torn mouse/focus fragments. Each one used to
 * hit the system pasteboard and re-attach the same clipboard image (#75150).
 *
 * Explicit `/paste` and hotkey paste bypass this gate (quiet=false).
 */

/** Minimum gap between quiet pasteboard probes. */
export const QUIET_CLIPBOARD_PROBE_COOLDOWN_MS = 2_000

/**
 * Skip re-attaching an image that matches the last quiet attach within this
 * window. Explicit paste still attaches.
 */
export const QUIET_CLIPBOARD_ATTACH_DEDUPE_MS = 30_000

export type QuietClipboardProbeState = {
  lastAttachedAt: number | null
  lastAttachedKey: string | null
  lastProbeAt: number | null
}

export type ClipboardImageMeta = {
  height?: number
  path?: string
  token_estimate?: number
  width?: number
}

export function createQuietClipboardProbeState(): QuietClipboardProbeState {
  return {
    lastAttachedAt: null,
    lastAttachedKey: null,
    lastProbeAt: null
  }
}

/** Stable-ish fingerprint for "same clipboard image" dedupe. */
export function clipboardImageFingerprint(meta: ClipboardImageMeta): string {
  const width = meta.width ?? 0
  const height = meta.height ?? 0
  const tokens = meta.token_estimate ?? 0

  return `${width}x${height}:${tokens}`
}

export function shouldQuietClipboardProbe(
  state: QuietClipboardProbeState,
  now: number,
  cooldownMs: number = QUIET_CLIPBOARD_PROBE_COOLDOWN_MS
): boolean {
  if (state.lastProbeAt == null) {
    return true
  }

  return now - state.lastProbeAt >= cooldownMs
}

export function shouldAttachQuietClipboardImage(
  state: QuietClipboardProbeState,
  fingerprint: string,
  now: number,
  dedupeMs: number = QUIET_CLIPBOARD_ATTACH_DEDUPE_MS
): boolean {
  if (state.lastAttachedKey == null || state.lastAttachedAt == null) {
    return true
  }

  if (state.lastAttachedKey !== fingerprint) {
    return true
  }

  return now - state.lastAttachedAt >= dedupeMs
}

export function noteQuietClipboardProbe(state: QuietClipboardProbeState, now: number): void {
  state.lastProbeAt = now
}

export function noteQuietClipboardAttach(
  state: QuietClipboardProbeState,
  fingerprint: string,
  now: number
): void {
  state.lastAttachedAt = now
  state.lastAttachedKey = fingerprint
  state.lastProbeAt = now
}
