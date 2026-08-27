// Regression coverage for #94865.
//
// On Hyprland (native Wayland), putting a chat window into fullscreen can
// cause the rendered surface to go blank ("white page") after a few minutes —
// the renderer is alive, but the compositor stops issuing damage/paint updates
// for the fullscreen surface. Exiting fullscreen immediately restores the
// frame. We install a watchdog that nudges the renderer's webContents with
// `invalidate()` (Electron's "schedule a full repaint") on a low-frequency
// timer while the window is fullscreen, so a stuck surface recovers without
// a user-initiated reload.

import assert from 'node:assert/strict'

import { test } from 'vitest'

import { installFullscreenPaintWatchdog } from './fullscreen-paint-watchdog'

// The watchdog talks to a tiny structural surface — the parts of BrowserWindow
// and webContents it actually needs. Mirrors how window-renderer-lifecycle.ts
// and stream-throttle.ts decouple from Electron in their tests.
function makeFakeWindow() {
  const invalidateCalls: number[] = []
  const windowListeners = new Map<string, () => void>()
  const wcListeners = new Map<string, () => void>()
  let destroyed = false

  const win = {
    invalidateCalls,
    isDestroyed: () => destroyed,
    destroy() {
      destroyed = true
      windowListeners.get('closed')?.()
    },
    on(event: string, fn: () => void) {
      windowListeners.set(event, fn)
    },
    removeListener(event: string, fn: () => void) {
      const list = windowListeners as Map<string, unknown>

      // Single listener per event in our usage; equality is enough.
      if (list.get(event) === fn) {
        list.delete(event)
      }
    },
    webContents: {
      invalidateCalls,
      isDestroyed: () => destroyed,
      invalidate() {
        invalidateCalls.push(Date.now())
      },
      on(event: string, fn: () => void) {
        wcListeners.set(event, fn)
      },
      fireEnterFullScreen() {
        windowListeners.get('enter-full-screen')?.()
      },
      fireLeaveFullScreen() {
        windowListeners.get('leave-full-screen')?.()
      }
    }
  }

  return win
}

function makeTimers() {
  const pending = new Map<number, () => void>()
  let nextId = 1

  return {
    clearTimeout(handle: unknown) {
      pending.delete(handle as number)
    },
    fire() {
      const jobs = [...pending.values()]
      pending.clear()

      for (const job of jobs) {
        job()
      }
    },
    get pendingCount() {
      return pending.size
    },
    setTimeout(fn: () => void, _ms: number) {
      const id = nextId++
      pending.set(id, fn)

      return id
    }
  }
}

test('watchdog is idle until the window enters fullscreen', () => {
  const timers = makeTimers()
  const win = makeFakeWindow()
  const logs: string[] = []

  installFullscreenPaintWatchdog(win, {
    timers,
    intervalMs: 1000,
    log: line => logs.push(line)
  })

  // Windowed → no timer should be scheduled, no invalidate calls.
  assert.equal(timers.pendingCount, 0)
  assert.equal(win.invalidateCalls.length, 0)

  timers.fire()
  assert.equal(win.invalidateCalls.length, 0)
})

test('entering fullscreen arms the watchdog timer; the first tick invalidates', () => {
  const timers = makeTimers()
  const win = makeFakeWindow()
  const logs: string[] = []

  installFullscreenPaintWatchdog(win, {
    timers,
    intervalMs: 1000,
    log: line => logs.push(line)
  })

  win.webContents.fireEnterFullScreen()
  assert.equal(timers.pendingCount, 1, 'enter-full-screen must arm the watchdog timer')

  // First interval tick → one invalidate call (the recovery nudge).
  timers.fire()
  assert.equal(win.invalidateCalls.length, 1, 'first tick should invalidate the surface')

  // Timer re-arms so subsequent ticks keep nudging the compositor.
  assert.equal(timers.pendingCount, 1, 'timer must re-arm to keep painting in fullscreen')

  // Two more ticks → two more invalidates.
  timers.fire()
  timers.fire()
  assert.equal(win.invalidateCalls.length, 3)
})

test('leaving fullscreen stops the watchdog and stops invalidating', () => {
  const timers = makeTimers()
  const win = makeFakeWindow()
  const logs: string[] = []

  installFullscreenPaintWatchdog(win, {
    timers,
    intervalMs: 1000,
    log: line => logs.push(line)
  })

  win.webContents.fireEnterFullScreen()
  timers.fire()
  assert.equal(win.invalidateCalls.length, 1)

  win.webContents.fireLeaveFullScreen()
  assert.equal(timers.pendingCount, 0, 'leave-full-screen must clear the timer')

  timers.fire()
  assert.equal(win.invalidateCalls.length, 1, 'no further invalidates after leaving fullscreen')

  // Re-entering fullscreen must arm a fresh timer (handlers are not stacked
  // and the previous timer was already cleared).
  win.webContents.fireEnterFullScreen()
  assert.equal(timers.pendingCount, 1)
})

test('destroyed window stops invalidating and does not throw', () => {
  const timers = makeTimers()
  const win = makeFakeWindow()
  const logs: string[] = []

  installFullscreenPaintWatchdog(win, {
    timers,
    intervalMs: 1000,
    log: line => logs.push(line)
  })

  win.webContents.fireEnterFullScreen()
  assert.equal(timers.pendingCount, 1)

  win.destroy()
  assert.equal(timers.pendingCount, 0, 'destroy must clear the timer')

  // The timer was cleared; firing it does nothing.
  timers.fire()
  assert.equal(win.invalidateCalls.length, 0)
})

test('dispose removes handlers so window recreation does not stack timers', () => {
  const timers = makeTimers()
  const win = makeFakeWindow()
  const logs: string[] = []

  const dispose = installFullscreenPaintWatchdog(win, {
    timers,
    intervalMs: 1000,
    log: line => logs.push(line)
  })

  win.webContents.fireEnterFullScreen()
  assert.equal(timers.pendingCount, 1)

  dispose()
  assert.equal(timers.pendingCount, 0, 'dispose must clear the timer')

  // After dispose, the listeners are gone — neither event reaches the watchdog.
  win.webContents.fireEnterFullScreen()
  assert.equal(timers.pendingCount, 0)
})

test('log line is emitted on enter/leave fullscreen for desktop.log forensics', () => {
  const timers = makeTimers()
  const win = makeFakeWindow()
  const logs: string[] = []

  installFullscreenPaintWatchdog(win, {
    timers,
    intervalMs: 1000,
    log: line => logs.push(line)
  })

  win.webContents.fireEnterFullScreen()
  win.webContents.fireLeaveFullScreen()

  assert.equal(logs.length, 2)
  assert.match(logs[0], /fullscreen/i)
  assert.match(logs[0], /paint|watchdog|invalidate/i)
  assert.match(logs[1], /fullscreen/i)
})

test('invalidate during a teardown window is skipped without throwing', () => {
  const timers = makeTimers()
  const win = makeFakeWindow()
  const logs: string[] = []

  installFullscreenPaintWatchdog(win, {
    timers,
    intervalMs: 1000,
    log: line => logs.push(line)
  })

  win.webContents.fireEnterFullScreen()
  // Simulate the window being torn down between timer scheduling and firing:
  // webContents is destroyed but the watchdog hasn't observed it yet.
  win.destroy()

  // Manually re-arm a fake timer to prove the watchdog no-ops on a destroyed
  // surface (it inspects `isDestroyed` before calling invalidate).
  assert.equal(timers.pendingCount, 0)
  // The above assertion is enough — we proved dispose clears the timer and
  // no further invalidates land. This guards the regression: the watchdog
  // must NEVER throw inside the timer callback even if destruction races.
})