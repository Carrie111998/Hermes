// Fullscreen paint watchdog for chat windows (regression coverage for #94865).
//
// On Hyprland (native Wayland), putting a chat window into fullscreen can
// cause the rendered surface to go blank ("white page") after a few minutes:
// the renderer process is alive, but the compositor stops issuing damage /
// paint updates for the fullscreen surface. Exiting fullscreen immediately
// restores the frame. The Chromium render-process-gone path
// (window-renderer-lifecycle.ts) does NOT help — no crash, no exit, just a
// stale compositor surface — so we install a low-frequency paint heartbeat
// while the window is fullscreen: every `intervalMs`, we ask the renderer to
// schedule a full repaint via `webContents.invalidate()`. If the surface was
// stuck, this re-issues a damage event and the frame recovers automatically
// (no manual reload). On platforms where the compositor is already painting
// correctly, the invalidate call is a cheap no-op.
//
// The watchdog is:
//   - Electron-free at the surface: the BrowserWindow / webContents slice we
//     touch is structural, with timers injected — mirroring stream-throttle.ts
//     and window-renderer-lifecycle.ts so the pure decision logic stays
//     unit-testable.
//   - Scoped to fullscreen: windowed chat windows are never touched.
//   - Idempotent: re-entering fullscreen re-arms cleanly; leaving clears the
//     timer; window destruction clears the timer; dispose() removes the
//     listeners so a recreated window does not stack handlers.

/** Default heartbeat interval while fullscreen. 60s keeps the recovery
 *  nudge cheap while being frequent enough to recover within a minute or
 *  two of an idle surface going blank (the symptom window in #94865). */
export const FULLSCREEN_PAINT_WATCHDOG_INTERVAL_MS = 60_000

export interface FullscreenWatchdogWindowLike {
  isDestroyed: () => boolean
  on: (event: string, listener: () => void) => unknown
  removeListener: (event: string, listener: () => void) => unknown
  webContents: {
    isDestroyed: () => boolean
    /** Electron's `webContents.invalidate()` — "Schedules a full repaint". */
    invalidate: () => void
  }
}

export interface FullscreenWatchdogTimers {
  clearTimeout(handle: unknown): void
  setTimeout(fn: () => void, ms: number): unknown
}

export interface FullscreenWatchdogOptions {
  timers?: FullscreenWatchdogTimers
  intervalMs?: number
  /** One line per state change for desktop.log forensics. */
  log?: (message: string) => void
}

const DEFAULT_INTERVAL_MS = FULLSCREEN_PAINT_WATCHDOG_INTERVAL_MS

function defaultTimers(): FullscreenWatchdogTimers {
  return {
    clearTimeout: handle => clearTimeout(handle as never),
    setTimeout: (fn, ms) => setTimeout(fn, ms) as unknown
  }
}

/**
 * Attach a fullscreen paint heartbeat to a window.
 *
 * Returns a `dispose()` that removes the listeners and clears the timer.
 * Dispose is idempotent and safe to call after window destruction.
 */
export function installFullscreenPaintWatchdog(
  win: FullscreenWatchdogWindowLike,
  options: FullscreenWatchdogOptions = {}
): () => void {
  const timers = options.timers ?? defaultTimers()
  const intervalMs = options.intervalMs ?? DEFAULT_INTERVAL_MS
  const log = options.log ?? (() => {})

  let timer: unknown = null
  let disposed = false
  let inFullscreen = false

  function clearTimer(): void {
    if (timer === null) {
      return
    }

    try {
      timers.clearTimeout(timer)
    } catch {
      // Timer already cleared by the runtime — best effort.
    }

    timer = null
  }

  function safeInvalidate(): void {
    if (win.isDestroyed()) {
      return
    }

    const contents = win.webContents

    if (!contents || contents.isDestroyed()) {
      return
    }

    try {
      contents.invalidate()
    } catch (error) {
      log(
        `[fullscreen-paint-watchdog] invalidate failed: ${error instanceof Error ? error.message : String(error)}`
      )
    }
  }

  function tick(): void {
    // The timer fired; if the window is already gone or no longer
    // fullscreen, the disarm path will have cleared us, but a stale fire
    // is still possible if the runtime ran us before clearing the handle.
    timer = null

    if (!inFullscreen) {
      return
    }

    safeInvalidate()

    if (disposed || win.isDestroyed()) {
      return
    }

    // Re-arm for the next tick. We re-arm AFTER the invalidate call so a
    // failing invalidate (above) does not produce a tight repaint loop.
    timer = timers.setTimeout(tick, intervalMs)
  }

  function onEnterFullScreen(): void {
    if (disposed || win.isDestroyed() || inFullscreen) {
      return
    }

    inFullscreen = true
    clearTimer()
    timer = timers.setTimeout(tick, intervalMs)
    log('[fullscreen-paint-watchdog] fullscreen entered; paint heartbeat armed')
  }

  function onLeaveFullScreen(): void {
    if (disposed || !inFullscreen) {
      return
    }

    inFullscreen = false
    clearTimer()
    log('[fullscreen-paint-watchdog] fullscreen left; paint heartbeat disarmed')
  }

  function onClosed(): void {
    inFullscreen = false
    clearTimer()
  }

  win.on('enter-full-screen', onEnterFullScreen)
  win.on('leave-full-screen', onLeaveFullScreen)
  win.on('closed', onClosed)

  return () => {
    if (disposed) {
      return
    }

    disposed = true
    inFullscreen = false
    clearTimer()

    try {
      win.removeListener('enter-full-screen', onEnterFullScreen)
      win.removeListener('leave-full-screen', onLeaveFullScreen)
      win.removeListener('closed', onClosed)
    } catch {
      // Window already torn down — listeners are gone with it.
    }
  }
}