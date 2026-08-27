import fs from 'node:fs'

import type { BackendOutputTail } from './backend-claim'

// `hermes serve` announces HERMES_BACKEND_READY; the legacy `hermes dashboard`
// backend announces HERMES_DASHBOARD_READY. Accept either so the desktop spawn
// works against both the headless backend and old/dashboard runtimes.
const _READY_RE = /^HERMES_(?:BACKEND|DASHBOARD)_READY port=(\d+)/m

// The announcement clock starts the instant the backend process is spawned —
// before uvicorn binds its socket. On a cold install the child must first
// compile and import the whole `hermes_cli.main` → `web_server` → FastAPI/
// uvicorn chain, and on Windows real-time AV (Defender) scans every freshly
// written `.pyc`. That pre-bind cost can run 30-60s on a slow disk, so a tight
// 45s deadline kills a *healthy but still-starting* backend and respawns it,
// piling up orphaned processes (issue #50209). A roomier default absorbs the
// cold-start cost; a warm start still announces in well under a second.
const DEFAULT_PORT_ANNOUNCE_TIMEOUT_MS = 90_000
// Never trust a deadline tighter than the warm-start path needs; floor at 45s
// (the historical default) so a malformed override can't reintroduce the loop.
const MIN_PORT_ANNOUNCE_TIMEOUT_MS = 45_000

/**
 * Resolve the port-announcement deadline. Honors the
 * HERMES_DESKTOP_PORT_ANNOUNCE_TIMEOUT_MS env override (for users on slow
 * disks / aggressive AV who need an even longer cold-start window), clamped
 * to a sane floor so a bad value can't make boot flakier than the default.
 */
function resolvePortAnnounceTimeoutMs(env = process.env) {
  const parsed = Number(env.HERMES_DESKTOP_PORT_ANNOUNCE_TIMEOUT_MS)

  if (Number.isFinite(parsed) && parsed > 0) {
    return Math.max(MIN_PORT_ANNOUNCE_TIMEOUT_MS, Math.round(parsed))
  }

  return DEFAULT_PORT_ANNOUNCE_TIMEOUT_MS
}

/**
 * Line-buffered scanner for the port-announcement sentinel.
 *
 * One implementation, fed either by the spawn-time output tail (the armed
 * path) or by a raw `data` listener (the legacy path), so both parse the
 * sentinel identically — including a line split across chunk boundaries.
 * Latches the first match: later output can never re-settle it.
 */
function createReadyLineScanner(onPort: (port: number) => void) {
  let buf = ''
  let port: number | null = null

  return {
    feed(chunk: unknown) {
      if (port !== null) {
        return
      }

      buf += String(chunk)

      let nl

      while ((nl = buf.indexOf('\n')) !== -1) {
        const line = buf.slice(0, nl)
        buf = buf.slice(nl + 1)
        const m = line.match(_READY_RE)

        if (m) {
          port = parseInt(m[1], 10)
          onPort(port)

          return
        }
      }
    },
    port() {
      return port
    }
  }
}

/**
 * The port announcement, armed on the spawn-time output tail.
 *
 * `port()` is the already-observed port (null until the sentinel arrives);
 * `whenAnnounced(cb)` fires the callback once, immediately if the sentinel was
 * already seen. Never rejects and owns no timers — deadline, exit and error
 * handling stay with the waiters below.
 */
export interface PortAnnouncement {
  port(): number | null
  whenAnnounced(listener: (port: number) => void): void
  dispose(): void
}

/**
 * Arm the port-announcement scanner on a child's output tail (#96315).
 *
 * MUST be called synchronously after `tail.attach(child)` — before the first
 * `await` in the spawn path. This is the whole fix: the readiness sentinel is
 * matched by a listener that exists from the instant of spawn, instead of by
 * one attached several awaits later (after `claimBackendChild`, whose Windows
 * start-marker probe alone budgets 30s of PowerShell). A stream in flowing
 * mode hands each chunk only to the listeners present when it is emitted, so
 * an announcement that arrived during those awaits was delivered to the tail
 * and to nothing else — the waiter then sat on a promise that could no longer
 * be resolved and burned the full 90s deadline against a healthy, listening
 * backend, which is exactly the reported failure.
 *
 * Reading through the tail also means both streams are covered for free: the
 * tail attaches to stdout AND stderr, so a runtime whose sentinel lands on
 * stderr (a `sys.stdout` redirect installed at import time by
 * `tui_gateway/server.py`, see #96282/#96324) is handled by the same code
 * path rather than by a second channel.
 */
export function armPortAnnouncement(tail: BackendOutputTail): PortAnnouncement {
  let listener: ((port: number) => void) | null = null

  const scanner = createReadyLineScanner(port => {
    unsubscribe()
    listener?.(port)
  })

  const unsubscribe = tail.observe(chunk => scanner.feed(chunk))

  return {
    port: scanner.port,
    whenAnnounced(next) {
      const seen = scanner.port()

      if (seen !== null) {
        next(seen)

        return
      }

      listener = next
    },
    dispose() {
      listener = null
      unsubscribe()
    }
  }
}

/**
 * Watch a child process's stdout for the `HERMES_(BACKEND|DASHBOARD)_READY
 * port=<N>` line that web_server.py prints after uvicorn binds its socket.
 *
 * Returns the parsed port. Rejects if:
 *   - the child exits before emitting the line
 *   - the child emits an `error` event
 *   - no line arrives within the timeout
 *
 * The default timeout is cold-start tolerant (see
 * DEFAULT_PORT_ANNOUNCE_TIMEOUT_MS) because the clock starts before the
 * backend has even bound its port. Pass an explicit `timeoutMs` to override.
 *
 * A single `cleanup()` tears down every listener (data/exit/error/timeout)
 * on every terminal path — resolve, reject, or timeout — so repeated
 * backend spawns don't leak listener slots on the child.
 *
 * Pass `announcement` (from `armPortAnnouncement`, armed at spawn time) to
 * read the sentinel out of the output tail instead of attaching a `data`
 * listener here — see `armPortAnnouncement` for why that ordering matters.
 * Without it the legacy stdout-only listener is used, which is correct only
 * when this waiter is created in the same tick as the spawn.
 */
function waitForDashboardPort(
  child,
  timeoutMs = resolvePortAnnounceTimeoutMs(),
  describeOutputTail = () => '',
  announcement: PortAnnouncement | null = null
) {
  return new Promise((resolve, reject) => {
    let done = false

    // Legacy path only: the armed path reads through the tail instead.
    const scanner = announcement ? null : createReadyLineScanner(port => settle(port))

    function cleanup() {
      if (done) {
        return
      }

      done = true
      clearTimeout(timer)

      if (announcement) {
        announcement.dispose()
      } else {
        child.stdout.off('data', onData)
      }

      child.off('exit', onExit)
      child.off('error', onError)
    }

    function settle(port: number) {
      cleanup()
      resolve(port)
    }

    function onData(chunk) {
      scanner?.feed(chunk)
    }

    function onExit(code, signal) {
      cleanup()
      reject(new Error(`Hermes backend: exited before port announcement (${signal || code})${describeOutputTail()}`))
    }

    function onError(err) {
      cleanup()
      reject(err)
    }

    const timer = setTimeout(() => {
      cleanup()
      reject(new Error(`Timed out waiting for Hermes backend port announcement (${timeoutMs}ms)`))
    }, timeoutMs)

    child.on('exit', onExit)
    child.on('error', onError)

    if (announcement) {
      // Resolves synchronously when the sentinel already flew past during the
      // spawn path's awaits — the case that used to hang for the full 90s.
      announcement.whenAnnounced(settle)
    } else {
      child.stdout.on('data', onData)
    }
  })
}

function readDashboardReadyFile(readyFile: fs.PathOrFileDescriptor) {
  if (!readyFile) {
    return null
  }

  try {
    const parsed = JSON.parse(fs.readFileSync(readyFile, 'utf8'))
    const port = Number(parsed?.port)

    return Number.isInteger(port) && port > 0 ? port : null
  } catch {
    return null
  }
}

function waitForDashboardReadyFile(
  readyFile,
  child,
  timeoutMs = resolvePortAnnounceTimeoutMs(),
  describeOutputTail = () => '',
  announcement: PortAnnouncement | null = null
) {
  return new Promise((resolve, reject) => {
    let done = false
    let interval = null

    function cleanup() {
      if (done) {
        return
      }

      done = true
      clearTimeout(timer)

      if (interval) {
        clearInterval(interval)
      }

      announcement?.dispose()
      child.off('exit', onExit)
      child.off('error', onError)
    }

    function settle(port: number) {
      cleanup()
      resolve(port)
    }

    function check() {
      const port = readDashboardReadyFile(readyFile)

      if (port) {
        settle(port)
      }
    }

    function onExit(code, signal) {
      cleanup()
      reject(new Error(`Hermes backend: exited before port announcement (${signal || code})${describeOutputTail()}`))
    }

    function onError(err) {
      cleanup()
      reject(err)
    }

    const timer = setTimeout(() => {
      cleanup()
      reject(new Error(`Timed out waiting for Hermes backend port announcement (${timeoutMs}ms)`))
    }, timeoutMs)

    child.on('exit', onExit)
    child.on('error', onError)
    interval = setInterval(check, 50)

    if (typeof interval.unref === 'function') {
      interval.unref()
    }

    // The ready file is opportunistic — a runtime that never writes it must
    // still be able to announce on its streams, so the armed announcement
    // settles this wait too instead of losing to the deadline.
    announcement?.whenAnnounced(settle)
    check()
  })
}

function waitForDashboardPortAnnouncement(
  child,
  options: {
    /**
     * Port announcement armed on the spawn-time output tail
     * (`armPortAnnouncement`). Supply it whenever this waiter is created
     * after an `await` — without it a sentinel emitted in the meantime is
     * unrecoverable (#96315).
     */
    announcement?: PortAnnouncement | null
    /** Returns a formatted stdout/stderr tail suffix for exit errors (#93608). */
    describeOutputTail?: () => string
    readyFile?: fs.PathOrFileDescriptor | null
    timeoutMs?: number
  } = {}
) {
  const timeoutMs = options.timeoutMs ?? resolvePortAnnounceTimeoutMs()
  const describeOutputTail = options.describeOutputTail ?? (() => '')
  const announcement = options.announcement ?? null

  if (options.readyFile) {
    return waitForDashboardReadyFile(options.readyFile, child, timeoutMs, describeOutputTail, announcement)
  }

  return waitForDashboardPort(child, timeoutMs, describeOutputTail, announcement)
}

export {
  DEFAULT_PORT_ANNOUNCE_TIMEOUT_MS,
  MIN_PORT_ANNOUNCE_TIMEOUT_MS,
  readDashboardReadyFile,
  resolvePortAnnounceTimeoutMs,
  waitForDashboardPort,
  waitForDashboardPortAnnouncement,
  waitForDashboardReadyFile
}
