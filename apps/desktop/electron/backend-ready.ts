import fs from 'node:fs'

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

/** Parse the first READY-sentinel port in arbitrary text (multi-line safe). */
function parseReadyPort(text) {
  const m = text.match(_READY_RE)

  return m ? parseInt(m[1], 10) : null
}

/**
 * Incremental line splitter. Feeds complete `\n`-terminated lines to `onLine`
 * (which returns true to stop scanning — used when the READY sentinel hit).
 * `pending()` exposes the trailing partial line for last-chance exit scans.
 */
function makeLineScanner(onLine) {
  let buf = ''

  return {
    push(chunk) {
      buf += chunk.toString()
      let nl

      while ((nl = buf.indexOf('\n')) !== -1) {
        const line = buf.slice(0, nl)
        buf = buf.slice(nl + 1)

        if (onLine(line)) {
          return
        }
      }
    },
    pending() {
      return buf
    }
  }
}

/**
 * The minimal child-process surface the waiters consume (spawn() result).
 * Loose by design: tests pass EventEmitter stand-ins, main.ts passes real
 * ChildProcess objects.
 */
function waitForDashboardPort(child, timeoutMs = resolvePortAnnounceTimeoutMs(), describeOutputTail = () => '') {
  return waitForDashboardPortAnnouncement(child, { describeOutputTail, timeoutMs })
}

function readDashboardReadyFile(readyFile) {
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
  describeOutputTail = () => ''
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

      child.off('exit', onExit)
      child.off('error', onError)
    }

    function check() {
      const port = readDashboardReadyFile(readyFile)

      if (port) {
        cleanup()
        resolve(port)
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

    check()
  })
}

/**
 * Watch a spawned backend child for the `HERMES_(BACKEND|DASHBOARD)_READY
 * port=<N>` announcement and resolve with the port it binds.
 *
 * The wait is a multi-channel state machine — the FIRST channel to yield a
 * port wins; every other channel is torn down. Channels, in order:
 *
 *   1. `outputTail` (optional): the spawn-time combined stdout+stderr buffer.
 *      The desktop spawn path attaches it before its first `await`, so an
 *      announcement that lands while the claim/bookkeeping awaits are still
 *      pending is observed here instead of being lost to a listener attached
 *      too late (issue #96315 — the backend was up, the 90s timer still won).
 *   2. Live `data` on stdout AND stderr. Watching both matters because
 *      `tui_gateway.server` redirects Python's stdout to stderr at import
 *      time (it reserves stdout for JSON-RPC), so the `serve` backend prints
 *      its READY sentinel on stderr. A stdout-only watcher burned the full
 *      deadline against a healthy backend (the #96315 family: #96312, #96297,
 *      #96294, #96282, #96324, #96327, #96334, ...).
 *   3. `readyFile` (optional): an atomic JSON file the backend writes right
 *      before the sentinel. The reliable channel when the sentinel stream is
 *      lost entirely — e.g. Windows uv venv trampolines whose grandchild
 *      stdout never reaches the spawn pipe (#96280).
 *   4. Last-chance scans on child exit: the waiter's own partial-line buffer
 *      (sentinel without a trailing `\n`), unconsumed data still sitting in
 *      the streams' internal buffers (pipe teardown raced the final chunk),
 *      and the `outputTail` text again. A backend that announced and then
 *      exited (watchdog false positive, superseded attempt) still yields its
 *      port instead of a spurious boot failure.
 *
 * Rejects if the child exits with no announcement anywhere, emits an `error`
 * event, or no channel yields a port within `timeoutMs`.
 *
 * A single `cleanup()` tears down every listener (data/exit/error/timeout/
 * interval) on every terminal path — resolve, reject, or timeout — so
 * repeated backend spawns don't leak listener slots on the child.
 */
function waitForDashboardPortAnnouncement(
  child,
  options: {
    /** Returns a formatted stdout/stderr tail suffix for exit errors (#93608). */
    describeOutputTail?: () => string
    /** Spawn-time combined stdout+stderr buffer (see channel 1 above). */
    outputTail?: { text(): string } | null
    readyFile?: fs.PathOrFileDescriptor | null
    timeoutMs?: number
  } = {}
) {
  const timeoutMs = options.timeoutMs ?? resolvePortAnnounceTimeoutMs()
  const describeOutputTail = options.describeOutputTail ?? (() => '')

  return new Promise((resolve, reject) => {
    // Channel 1: the announcement may already be in the spawn-time tail
    // buffer (attached before any await in the spawn path). Resolving here
    // closes the "listener attached after the READY line already flew past"
    // race from #96315 without burning the 90s deadline.
    const preObserved = options.outputTail ? parseReadyPort(options.outputTail.text()) : null

    if (preObserved !== null) {
      resolve(preObserved)

      return
    }

    let done = false
    let timer = null
    let readyFileInterval = null

    const stdoutScanner = makeLineScanner(onReadyLine)
    const stderrScanner = makeLineScanner(onReadyLine)

    function onReadyLine(line) {
      const port = parseReadyPort(line)

      if (port !== null) {
        cleanup()
        resolve(port)

        return true
      }

      return false
    }

    function onStdoutData(chunk) {
      stdoutScanner.push(chunk)
    }

    function onStderrData(chunk) {
      stderrScanner.push(chunk)
    }

    // Channel 4: exit-time last-chance scans. Every scan resolves through the
    // same settle path; the first hit wins and `done` guards double-settle.
    function lastChanceScan() {
      // 4a. Sentinels whose trailing newline never arrived (final partial
      //     line still in our own buffers).
      for (const scanner of [stdoutScanner, stderrScanner]) {
        const port = parseReadyPort(scanner.pending())

        if (port !== null) {
          cleanup()
          resolve(port)

          return true
        }
      }

      // 4b. Data still sitting in the stream's internal buffer when the pipe
      //     tore down ('data' never fired for the final chunk). One scanner
      //     across reads so a sentinel split across chunks still matches.
      for (const stream of [child.stdout, child.stderr]) {
        if (!stream || typeof stream.read !== 'function') {
          continue
        }

        try {
          const scan = makeLineScanner(onReadyLine)
          let chunk

          while ((chunk = stream.read()) !== null && chunk !== undefined) {
            scan.push(chunk)
          }
        } catch {
          // Stream already destroyed; nothing left to read.
        }
      }

      // 4c. The spawn-time tail buffer, re-scanned at exit in case the
      //     announcement arrived after the wait attached but was consumed by
      //     another listener before ours fired.
      if (options.outputTail) {
        const port = parseReadyPort(options.outputTail.text())

        if (port !== null) {
          cleanup()
          resolve(port)

          return true
        }
      }

      return false
    }

    function onExit(code, signal) {
      if (lastChanceScan()) {
        return
      }

      cleanup()
      reject(new Error(`Hermes backend: exited before port announcement (${signal || code})${describeOutputTail()}`))
    }

    function onError(err) {
      cleanup()
      reject(err)
    }

    function cleanup() {
      if (done) {
        return
      }

      done = true

      if (timer) {
        clearTimeout(timer)
      }

      if (readyFileInterval) {
        clearInterval(readyFileInterval)
      }

      child.stdout?.off?.('data', onStdoutData)
      child.stderr?.off?.('data', onStderrData)
      child.off('exit', onExit)
      child.off('error', onError)
    }

    timer = setTimeout(() => {
      cleanup()
      reject(new Error(`Timed out waiting for Hermes backend port announcement (${timeoutMs}ms)`))
    }, timeoutMs)

    child.stdout?.on?.('data', onStdoutData)
    child.stderr?.on?.('data', onStderrData)
    child.on('exit', onExit)
    child.on('error', onError)

    // Channel 3: ready-file poller. A backend that writes it resolves
    // immediately; one that never writes it (old runtime) simply falls
    // through to the stream channels.
    if (options.readyFile) {
      const checkReadyFile = () => {
        const port = readDashboardReadyFile(options.readyFile)

        if (port !== null) {
          cleanup()
          resolve(port)
        }
      }

      readyFileInterval = setInterval(checkReadyFile, 50)

      if (typeof readyFileInterval.unref === 'function') {
        readyFileInterval.unref()
      }

      checkReadyFile()
    }
  })
}

export {
  DEFAULT_PORT_ANNOUNCE_TIMEOUT_MS,
  MIN_PORT_ANNOUNCE_TIMEOUT_MS,
  parseReadyPort,
  readDashboardReadyFile,
  resolvePortAnnounceTimeoutMs,
  waitForDashboardPort,
  waitForDashboardPortAnnouncement,
  waitForDashboardReadyFile
}
