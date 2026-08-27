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

import { createBackendOutputTail } from './backend-claim'
import {
  armPortAnnouncement,
  DEFAULT_PORT_ANNOUNCE_TIMEOUT_MS,
  MIN_PORT_ANNOUNCE_TIMEOUT_MS,
  readDashboardReadyFile,
  resolvePortAnnounceTimeoutMs,
  waitForDashboardPort,
  waitForDashboardPortAnnouncement,
  waitForDashboardReadyFile
} from './backend-ready'

type FakeChildProcess = EventEmitter & {
  stderr: EventEmitter
  stdout: EventEmitter
}

// A minimal stand-in for a spawned child process: an EventEmitter with stdout
// and stderr EventEmitters, matching the surface the waiters and the output
// tail consume (child.std*.on('data'), child.on('exit'|'error') + .off()).
function makeFakeChild(): FakeChildProcess {
  const child = new EventEmitter() as FakeChildProcess
  child.stdout = new EventEmitter()
  child.stderr = new EventEmitter()

  return child
}

/**
 * Reproduce the spawn path's real ordering: attach the output tail and arm the
 * announcement in one tick, then let the caller emit output BEFORE the waiter
 * is created — which is what happens while `claimBackendChild` is awaited.
 */
function spawnWithArmedTail() {
  const child = makeFakeChild()
  const tail = createBackendOutputTail()
  tail.attach(child)

  return { announcement: armPortAnnouncement(tail), child, tail }
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
// armPortAnnouncement (#96315): the sentinel is scanned from the spawn-time
// output tail, so it survives the awaits between spawn and the READY wait.
// ---------------------------------------------------------------------------

test('resolves a sentinel that was emitted BEFORE the waiter was created', async () => {
  const { announcement, child } = spawnWithArmedTail()

  // The claim is in flight here; the only listener on the stream is the tail.
  child.stdout.emit('data', 'HERMES_BACKEND_READY port=50468\n')
  child.stdout.emit('data', '  Hermes backend listening on 127.0.0.1:50468\n')

  // Waiter created several awaits later — pre-fix this could never settle and
  // burned the full deadline against a healthy, listening backend.
  const port = await waitForDashboardPortAnnouncement(child, { announcement, timeoutMs: 50 })
  assert.equal(port, 50468)
})

test('a late-created waiter WITHOUT the armed announcement misses the sentinel', async () => {
  const child = makeFakeChild()
  const tail = createBackendOutputTail()
  tail.attach(child)

  child.stdout.emit('data', 'HERMES_BACKEND_READY port=50468\n')

  // Documents the bug this fix removes: the tail consumed the chunk, and a
  // `data` listener attached afterwards can never see it again.
  await assert.rejects(waitForDashboardPortAnnouncement(child, { timeoutMs: 20 }), /Timed out/)
  assert.match(tail.text(), /HERMES_BACKEND_READY port=50468/)
})

test('resolves a sentinel that lands on stderr (import-time stdout redirect)', async () => {
  const { announcement, child } = spawnWithArmedTail()
  const wait = waitForDashboardPortAnnouncement(child, { announcement, timeoutMs: 1000 })

  child.stderr.emit('data', 'INFO: started\nHERMES_BACKEND_READY port=43210\n')

  assert.equal(await wait, 43210)
})

test('parses a tail-fed sentinel split across chunks and across streams', async () => {
  const { announcement, child } = spawnWithArmedTail()
  const wait = waitForDashboardPortAnnouncement(child, { announcement, timeoutMs: 1000 })

  child.stderr.emit('data', 'HERMES_BACKEND_READY po')
  child.stderr.emit('data', 'rt=8080\n')

  assert.equal(await wait, 8080)
})

test('a burst larger than the tail ring cannot evict an unscanned sentinel', async () => {
  const child = makeFakeChild()
  const tail = createBackendOutputTail(64)
  tail.attach(child)
  const announcement = armPortAnnouncement(tail)

  child.stdout.emit('data', 'HERMES_BACKEND_READY port=61234\n')
  child.stdout.emit('data', `${'x'.repeat(4096)}\n`)

  // The ring buffer has long since dropped the line; the scanner saw the chunk.
  assert.doesNotMatch(tail.text(), /HERMES_BACKEND_READY/)
  assert.equal(await waitForDashboardPortAnnouncement(child, { announcement, timeoutMs: 50 }), 61234)
})

test('the armed announcement settles the ready-file wait when no file appears', async () => {
  const { announcement, child } = spawnWithArmedTail()
  const readyFile = path.join(os.tmpdir(), `hermes-ready-absent-${process.pid}-${Date.now()}.json`)

  const wait = waitForDashboardPortAnnouncement(child, { announcement, readyFile, timeoutMs: 1000 })

  child.stdout.emit('data', 'HERMES_BACKEND_READY port=7777\n')

  assert.equal(await wait, 7777)
})

test('exit before any announcement still rejects with the output tail', async () => {
  const { announcement, child, tail } = spawnWithArmedTail()

  child.stderr.emit('data', 'ModuleNotFoundError: hermes_cli\n')

  const wait = waitForDashboardPortAnnouncement(child, {
    announcement,
    describeOutputTail: () => tail.describe(),
    timeoutMs: 1000
  })

  child.emit('exit', 1, null)

  await assert.rejects(wait, /exited before port announcement \(1\)[\s\S]*ModuleNotFoundError: hermes_cli/)
})

test('a settled waiter detaches from the tail (a later sentinel is inert)', async () => {
  const { announcement, child } = spawnWithArmedTail()
  const wait = waitForDashboardPortAnnouncement(child, { announcement, timeoutMs: 1000 })

  child.stdout.emit('data', 'HERMES_BACKEND_READY port=5555\n')
  assert.equal(await wait, 5555)

  assert.doesNotThrow(() => {
    child.stdout.emit('data', 'HERMES_BACKEND_READY port=9999\n')
  })
  assert.equal(announcement.port(), 5555)
})

test('a timed-out armed waiter tears down and does not double-settle', async () => {
  const { announcement, child } = spawnWithArmedTail()

  await assert.rejects(waitForDashboardPortAnnouncement(child, { announcement, timeoutMs: 20 }), /Timed out/)

  assert.doesNotThrow(() => {
    child.stdout.emit('data', 'HERMES_BACKEND_READY port=9999\n')
  })
  assert.equal(announcement.port(), null)
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
