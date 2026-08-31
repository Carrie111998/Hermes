/**
 * Tests for electron/backend-ready.ts.
 *
 * Run with: node --test electron/backend-ready.test.ts
 * (Wired into npm test:desktop:platforms in package.json.)
 *
 * Covers the cold-start port-announcement deadline (issue #50209): the clock
 * starts before the backend binds its port, so a tight 45s deadline killed a
 * healthy-but-still-compiling backend on cold Windows installs. The default is
 * now cold-start tolerant and overridable via
 * HERMES_DESKTOP_PORT_ANNOUNCE_TIMEOUT_MS, clamped to a 45s floor.
 */

import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  DEFAULT_PORT_ANNOUNCE_TIMEOUT_MS,
  MIN_PORT_ANNOUNCE_TIMEOUT_MS,
  readDashboardReadyFile,
  resolvePortAnnounceTimeoutMs,
  waitForDashboardPort,
  waitForDashboardPortAnnouncement,
  waitForDashboardReadyFile
} from './backend-ready'

type FakeChildProcess = EventEmitter & {
  stdout: EventEmitter
  stderr: EventEmitter
}

// A minimal stand-in for a spawned child process: an EventEmitter with
// stdout/stderr EventEmitters, matching the surface the readiness wait
// consumes (child.stdout/stderr.on('data'), child.on('exit'|'error') + the
// .off() teardown).
function makeFakeChild(): FakeChildProcess {
  const child = new EventEmitter() as FakeChildProcess
  child.stdout = new EventEmitter()
  child.stderr = new EventEmitter()

  return child
}

// ---------------------------------------------------------------------------
// resolvePortAnnounceTimeoutMs
// ---------------------------------------------------------------------------

test('default is cold-start tolerant (> the historical 45s floor)', () => {
  assert.equal(resolvePortAnnounceTimeoutMs({}), DEFAULT_PORT_ANNOUNCE_TIMEOUT_MS)
  assert.ok(
    DEFAULT_PORT_ANNOUNCE_TIMEOUT_MS > MIN_PORT_ANNOUNCE_TIMEOUT_MS,
    'cold-start default must exceed the warm-start floor'
  )
})

test('honors a valid HERMES_DESKTOP_PORT_ANNOUNCE_TIMEOUT_MS override', () => {
  const env = { HERMES_DESKTOP_PORT_ANNOUNCE_TIMEOUT_MS: '120000' }
  assert.equal(resolvePortAnnounceTimeoutMs(env), 120_000)
})

test('clamps an override below the floor up to the 45s minimum', () => {
  const env = { HERMES_DESKTOP_PORT_ANNOUNCE_TIMEOUT_MS: '1000' }
  assert.equal(resolvePortAnnounceTimeoutMs(env), MIN_PORT_ANNOUNCE_TIMEOUT_MS)
})

test('rounds a fractional override', () => {
  const env = { HERMES_DESKTOP_PORT_ANNOUNCE_TIMEOUT_MS: '60000.7' }
  assert.equal(resolvePortAnnounceTimeoutMs(env), 60_001)
})

test('falls back to the default for malformed / non-positive overrides', () => {
  for (const bad of ['', 'abc', '0', '-5', 'NaN', undefined]) {
    const env = bad === undefined ? {} : { HERMES_DESKTOP_PORT_ANNOUNCE_TIMEOUT_MS: bad }
    assert.equal(
      resolvePortAnnounceTimeoutMs(env),
      DEFAULT_PORT_ANNOUNCE_TIMEOUT_MS,
      `override ${JSON.stringify(bad)} should fall through to the default`
    )
  }
})

// ---------------------------------------------------------------------------
// waitForDashboardPort
// ---------------------------------------------------------------------------

test('resolves with the announced port', async () => {
  const child = makeFakeChild()
  const p = waitForDashboardPort(child, 1000)
  child.stdout.emit('data', 'noise before\nHERMES_DASHBOARD_READY port=54321\n')
  assert.equal(await p, 54321)
})

test('resolves with a HERMES_BACKEND_READY port (headless `serve`)', async () => {
  const child = makeFakeChild()
  const p = waitForDashboardPort(child, 1000)
  child.stdout.emit('data', 'HERMES_BACKEND_READY port=43210\n')
  assert.equal(await p, 43210)
})

test('parses the port even when the line arrives split across chunks', async () => {
  const child = makeFakeChild()
  const p = waitForDashboardPort(child, 1000)
  child.stdout.emit('data', 'HERMES_DASHBOARD_READY po')
  child.stdout.emit('data', 'rt=8080\n')
  assert.equal(await p, 8080)
})

test('rejects when the child exits before announcing', async () => {
  const child = makeFakeChild()
  const p = waitForDashboardPort(child, 1000)
  child.emit('exit', 1, null)
  await assert.rejects(p, /exited before port announcement/)
})

test('rejects on a child error event', async () => {
  const child = makeFakeChild()
  const p = waitForDashboardPort(child, 1000)
  child.emit('error', new Error('spawn ENOENT'))
  await assert.rejects(p, /spawn ENOENT/)
})

test('rejects with the timeout message after the deadline', async () => {
  const child = makeFakeChild()
  await assert.rejects(
    waitForDashboardPort(child, 20),
    /Timed out waiting for Hermes backend port announcement \(20ms\)/
  )
})

test('a late announcement after timeout does not throw (listeners torn down)', async () => {
  const child = makeFakeChild()
  await assert.rejects(waitForDashboardPort(child, 20), /Timed out/)
  // The orphaned backend may still print its READY line later; the watcher
  // must have detached so this emit is a no-op rather than a double-settle.
  assert.doesNotThrow(() => {
    child.stdout.emit('data', 'HERMES_DASHBOARD_READY port=9999\n')
  })
})

// ---------------------------------------------------------------------------
// ready-file port announcement
// ---------------------------------------------------------------------------

function mkTmpReadyFile() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-ready-test-'))

  return {
    dir,
    file: path.join(dir, 'ready.json'),
    cleanup: () => fs.rmSync(dir, { recursive: true, force: true })
  }
}

test('readDashboardReadyFile returns a valid port from JSON', () => {
  const tmp = mkTmpReadyFile()

  try {
    fs.writeFileSync(tmp.file, JSON.stringify({ port: 4567 }))
    assert.equal(readDashboardReadyFile(tmp.file), 4567)
  } finally {
    tmp.cleanup()
  }
})

test('readDashboardReadyFile ignores missing, malformed, or invalid files', () => {
  const tmp = mkTmpReadyFile()

  try {
    assert.equal(readDashboardReadyFile(tmp.file), null)
    fs.writeFileSync(tmp.file, '{')
    assert.equal(readDashboardReadyFile(tmp.file), null)
    fs.writeFileSync(tmp.file, JSON.stringify({ port: 0 }))
    assert.equal(readDashboardReadyFile(tmp.file), null)
  } finally {
    tmp.cleanup()
  }
})

test('waitForDashboardReadyFile resolves when the ready file appears', async () => {
  const tmp = mkTmpReadyFile()
  const child = makeFakeChild()

  try {
    const p = waitForDashboardReadyFile(tmp.file, child, 1000)
    setTimeout(() => fs.writeFileSync(tmp.file, JSON.stringify({ port: 8765 })), 20)
    assert.equal(await p, 8765)
  } finally {
    tmp.cleanup()
  }
})

test('waitForDashboardPortAnnouncement uses ready file when provided', async () => {
  const tmp = mkTmpReadyFile()
  const child = makeFakeChild()

  try {
    const p = waitForDashboardPortAnnouncement(child, { readyFile: tmp.file, timeoutMs: 1000 })
    setTimeout(() => fs.writeFileSync(tmp.file, JSON.stringify({ port: 9876 })), 20)
    assert.equal(await p, 9876)
  } finally {
    tmp.cleanup()
  }
})

test('waitForDashboardReadyFile rejects when the child exits before file readiness', async () => {
  const tmp = mkTmpReadyFile()
  const child = makeFakeChild()

  try {
    const p = waitForDashboardReadyFile(tmp.file, child, 1000)
    child.emit('exit', 1, null)
    await assert.rejects(p, /exited before port announcement/)
  } finally {
    tmp.cleanup()
  }
})

// ---------------------------------------------------------------------------
// #96315 readiness-race family — sentinel on stderr / timing jitter
// ---------------------------------------------------------------------------
//
// The `serve` backend prints its READY sentinel on stderr because
// tui_gateway.server redirects Python's stdout to stderr at import time (it
// reserves stdout for JSON-RPC). A stdout-only watcher burned the full 90s
// deadline against a healthy backend; these tests pin the multi-channel
// state machine that replaces it. Pre-fix, every "resolves" case below was a
// timeout rejection.

test('resolves when the READY sentinel lands on stderr (serve stdout redirect)', async () => {
  const child = makeFakeChild()
  const p = waitForDashboardPort(child, 1000)
  child.stderr.emit('data', 'HERMES_BACKEND_READY port=50468\n')
  assert.equal(await p, 50468)
})

test('resolves when the sentinel arrives split across stderr chunks', async () => {
  const child = makeFakeChild()
  const p = waitForDashboardPort(child, 1000)
  child.stderr.emit('data', 'noise\nHERMES_BACKEND_READY po')
  child.stderr.emit('data', 'rt=50469\n')
  assert.equal(await p, 50469)
})

test('resolves when the announcement landed before the wait attached (spawn-time tail)', async () => {
  const child = makeFakeChild()
  // Simulates outputTail buffering at spawn: the READY line flew past while
  // the spawn path was still awaiting the claim/bookkeeping, before the
  // wait's own data listeners existed (#96315 — the orphaned-promise race).
  const outputTail = { text: () => 'noise before\nHERMES_BACKEND_READY port=50470\nmore noise' }
  const p = waitForDashboardPortAnnouncement(child, { outputTail, timeoutMs: 1000 })
  assert.equal(await p, 50470)
})

test('combined wait resolves via stderr even when a ready file never appears', async () => {
  const tmp = mkTmpReadyFile()
  const child = makeFakeChild()

  try {
    // The ready file is configured but never written (old runtime without
    // ready-file support). The combined wait must NOT wait on the file alone:
    // the stderr channel resolves the port.
    const p = waitForDashboardPortAnnouncement(child, { readyFile: tmp.file, timeoutMs: 1000 })
    child.stderr.emit('data', 'HERMES_BACKEND_READY port=40001\n')
    assert.equal(await p, 40001)
  } finally {
    tmp.cleanup()
  }
})

test('resolves when exit races the final chunk (data still buffered in the stream)', async () => {
  const child = makeFakeChild()
  let buffered = 'HERMES_BACKEND_READY port=30001\n'
  ;(child.stdout as EventEmitter & { read?: () => unknown }).read = () => {
    const chunk = buffered
    buffered = ''
    return chunk || null
  }
  const p = waitForDashboardPort(child, 1000)
  // The child exits before the final chunk is delivered as a 'data' event —
  // e.g. a watchdog/superseded attempt tearing the pipe down mid-boot.
  child.emit('exit', 0, null)
  assert.equal(await p, 30001)
})

test('resolves when the sentinel reached the buffer without a trailing newline at exit', async () => {
  const child = makeFakeChild()
  const p = waitForDashboardPort(child, 1000)
  child.stdout.emit('data', 'HERMES_BACKEND_READY port=30002')
  child.emit('exit', 0, null)
  assert.equal(await p, 30002)
})

test('exit scan finds an announcement already buffered in the spawn-time tail', async () => {
  const child = makeFakeChild()
  const outputTail = { text: () => 'HERMES_BACKEND_READY port=50471\n' }
  const p = waitForDashboardPortAnnouncement(child, { outputTail, timeoutMs: 1000 })
  child.emit('exit', 0, null)
  assert.equal(await p, 50471)
})

test('a backend that announces then exits still resolves (not a boot failure)', async () => {
  const child = makeFakeChild()
  const p = waitForDashboardPort(child, 1000)
  child.stdout.emit('data', 'HERMES_BACKEND_READY port=30003\n')
  child.emit('exit', 0, null)
  assert.equal(await p, 30003)
})

// ---------------------------------------------------------------------------
// describeOutputTail (#93608): the child's real stderr reaches the exit error
// ---------------------------------------------------------------------------

test('exit-before-announcement error carries the buffered output tail (stdout path)', async () => {
  const child = makeFakeChild()

  const wait = waitForDashboardPortAnnouncement(child, {
    describeOutputTail: () => '\nRecent backend output:\nModuleNotFoundError: hermes_cli'
  })

  child.emit('exit', 1, null)

  await assert.rejects(wait, /exited before port announcement \(1\)[\s\S]*ModuleNotFoundError: hermes_cli/)
})

test('exit-before-announcement error carries the buffered output tail (ready-file path)', async () => {
  const child = makeFakeChild()
  const readyFile = path.join(os.tmpdir(), `hermes-ready-${process.pid}-${Date.now()}.json`)

  const wait = waitForDashboardPortAnnouncement(child, {
    describeOutputTail: () => '\nRecent backend output:\nTraceback (most recent call last)',
    readyFile
  })

  child.emit('exit', null, 'SIGSEGV')

  await assert.rejects(wait, /exited before port announcement \(SIGSEGV\)[\s\S]*Traceback/)
})

test('exit-before-announcement error stays clean when no output was buffered', async () => {
  const child = makeFakeChild()

  const wait = waitForDashboardPortAnnouncement(child, {})

  child.emit('exit', 137, null)

  await assert.rejects(wait, error => {
    assert.match((error as Error).message, /exited before port announcement \(137\)$/)

    return true
  })
})
