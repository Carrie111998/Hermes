// Stream-aware background throttling for chat windows.
//
// Chat windows must paint the live transcript while blurred, occluded, or
// minimized — but a static `backgroundThrottling: false` in webPreferences
// costs far more than that feature needs: it pins the renderer's
// `document.visibilityState` to 'visible' for the life of the window, which
// turns every visibility-gated poll and clock tick in the renderer into an
// always-on timer. An idle, hidden Hermes burned ~20% CPU forever.
//
// So throttling is a runtime dial instead: the renderers already report
// "which chats are mid-turn" for the quit guard (`hermes:active-work`), and
// this controller rides the merged edge of those reports. Any turn in flight →
// every registered chat window gets `setBackgroundThrottling(false)`, exactly
// the streaming behavior the static flag used to provide. All turns done →
// after a short trailing delay (so tail flushes land at full cadence) Chromium's
// default throttling returns and hidden windows go quiet.
//
// Pure and Electron-free (timers + the WebContents surface are injected) so it
// can be unit-tested, mirroring session-windows.ts.

/** How long after the last turn ends before throttling is restored. Covers the
 * stream queue's final coalesced flush and the settle writes that trail a
 * turn's completion, so re-throttling never strands a visible delta. */
const RETHROTTLE_DELAY_MS = 5_000

export interface ThrottleWindowLike {
  isDestroyed(): boolean
  webContents?: {
    isDestroyed(): boolean
    setBackgroundThrottling(allowed: boolean): void
  } | null
}

interface TimersLike {
  clearTimeout(handle: unknown): void
  setTimeout(fn: () => void, ms: number): unknown
}

export interface StreamThrottle {
  /** True while windows are currently unthrottled (streaming or trailing). */
  isUnthrottled(): boolean
  /** Track a chat window; applies the current state immediately and stops
   * tracking on close. */
  register(win: ThrottleWindowLike & { on?: (event: string, fn: () => void) => void }): void
  /** Report whether any turn is in flight across all renderers. */
  update(busy: boolean): void
  /** Briefly unthrottle after show/restore/focus so a Windows occluded-window
   * stall can pump the UI task runner again (#83420). */
  wake(): void
}

export function createStreamThrottle(
  timers: TimersLike = { clearTimeout: handle => clearTimeout(handle as never), setTimeout },
  delayMs: number = RETHROTTLE_DELAY_MS
): StreamThrottle {
  const windows = new Set<ThrottleWindowLike>()
  let unthrottled = false
  let trailing: unknown = null
  let busy = false

  function apply(win: ThrottleWindowLike) {
    if (win.isDestroyed()) {
      windows.delete(win)

      return
    }

    const contents = win.webContents

    if (!contents || contents.isDestroyed()) {
      return
    }

    try {
      contents.setBackgroundThrottling(!unthrottled)
    } catch {
      // A window mid-teardown can throw; it's about to leave the set anyway.
    }
  }

  function applyAll() {
    for (const win of windows) {
      apply(win)
    }
  }

  const api: StreamThrottle = {
    isUnthrottled: () => unthrottled,

    register(win) {
      windows.add(win)
      win.on?.('closed', () => windows.delete(win))
      // Defense in depth for #83420: when a chat window returns from
      // minimize/occlusion, pulse unthrottled so the browser task runner
      // gets a wake even if Chromium's occluded path wedged while hidden.
      for (const event of ['show', 'restore', 'focus'] as const) {
        win.on?.(event, () => api.wake())
      }
      apply(win)
    },

    update(nextBusy) {
      busy = nextBusy

      if (nextBusy) {
        if (trailing !== null) {
          timers.clearTimeout(trailing)
          trailing = null
        }

        if (!unthrottled) {
          unthrottled = true
          applyAll()
        }

        return
      }

      if (!unthrottled || trailing !== null) {
        return
      }

      // Trailing edge: keep full cadence briefly so the final flush paints.
      trailing = timers.setTimeout(() => {
        trailing = null
        unthrottled = false
        applyAll()
      }, delayMs)
    },

    wake() {
      // Force a trailing unthrottle window even when idle. If a turn is
      // already in flight, update(true) is enough (and cancels any pending
      // re-throttle). Capture busy first — update(true) latches it.
      const wasBusy = busy
      api.update(true)

      if (!wasBusy) {
        api.update(false)
      }
    }
  }

  return api
}
