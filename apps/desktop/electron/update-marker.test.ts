/**
 * Tests for electron/update-marker.ts — the in-app update mutual-exclusion
 * marker that prevents a desktop relaunched mid-update from spawning a backend
 * the updater then kills in a loop (#50238).
 *
 * Run with: node --test electron/update-marker.test.ts
 * (Wired into npm test:desktop:platforms in package.json.)
 *
 * Why this matters: the gate must report any well-formed marker with a live
 * updater pid even after suspend/clock jumps, treat malformed/unreadable state
 * as conservatively busy, distinguish absent/confirmed-dead markers, and never
 * delete or overwrite another process's claim from this desktop observer.
 */

import fs from 'fs'
import assert from 'node:assert/strict'
import os from 'os'
import path from 'path'

import { test } from 'vitest'

import {
  isPidAlive,
  markerPath,
  readLiveUpdateMarker,
  UPDATE_MARKER_MAX_AGE_MS,
  updateHandoffConflict,
  writeUpdateMarker
} from './update-marker'

function tmpHome(tag) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), `hermes-marker-${tag}-`))

  return dir
}

function writeMarker(home, pid, startedAtSec) {
  fs.writeFileSync(markerPath(home), `${pid}\n${startedAtSec}`)
}

const ALIVE: typeof process.kill = () => true // injected kill that "succeeds" => pid alive

const DEAD: typeof process.kill = () => {
  const err = new Error('no such process')

  ;(err as any).code = 'ESRCH'
  throw err
}

test('named profiles resolve one install-wide marker', () => {
  const root = path.join(os.tmpdir(), 'hermes-marker-profile-root')
  assert.equal(markerPath(path.join(root, 'profiles', 'alpha')), markerPath(path.join(root, 'profiles', 'beta')))
  assert.equal(markerPath(path.join(root, 'profiles', 'alpha')), path.join(root, '.hermes-update-in-progress'))
})

test('absent marker => no live update', () => {
  const home = tmpHome('absent')
  assert.equal(readLiveUpdateMarker(home, { kill: ALIVE }), null)
})

test('live pid within age ceiling => live update reported', () => {
  const home = tmpHome('live')
  const now = 1_000_000_000_000
  writeMarker(home, 4242, Math.floor(now / 1000) - 5) // 5s old
  const res = readLiveUpdateMarker(home, { kill: ALIVE, now: () => now })
  assert.ok(res, 'a fresh, alive marker is a live update')
  assert.equal(res.pid, 4242)
  assert.ok(res.ageMs >= 0 && res.ageMs < 10_000)
  assert.ok(fs.existsSync(markerPath(home)), 'a live marker is NOT deleted')
})

test('dead pid => no live update, with cleanup left to mutex-owning claimers', () => {
  const home = tmpHome('dead')
  writeMarker(home, 999999, Math.floor(Date.now() / 1000))
  assert.equal(readLiveUpdateMarker(home, { kill: DEAD }), null)
  assert.ok(fs.existsSync(markerPath(home)), 'the Electron reader never races an owner-CAS cleanup')
})

test('live pid past the nominal age ceiling remains authoritative', () => {
  const home = tmpHome('expired')
  const now = 1_000_000_000_000
  writeMarker(home, 4242, Math.floor((now - UPDATE_MARKER_MAX_AGE_MS - 60_000) / 1000))
  const result = readLiveUpdateMarker(home, { kill: ALIVE, now: () => now })
  assert.ok(result)
  assert.equal(result.pid, 4242)
  assert.equal(result.leaseExpired, true, 'age remains available for diagnostics')
  assert.ok(fs.existsSync(markerPath(home)), 'a live marker is never deleted')
})

test('malformed marker => conservative busy sentinel and no unlocked cleanup', () => {
  const home = tmpHome('malformed')
  fs.writeFileSync(markerPath(home), 'not-a-pid\nnonsense')
  const result = readLiveUpdateMarker(home, { kill: ALIVE })
  assert.ok(result?.unavailable)
  assert.equal(result.pid, null)
  assert.ok(fs.existsSync(markerPath(home)))
})

test('live pid with a malformed lease fails closed', () => {
  const home = tmpHome('malformed-live')
  fs.writeFileSync(markerPath(home), '4242\nnan\n')
  const result = readLiveUpdateMarker(home, { kill: ALIVE })
  assert.ok(result?.unavailable)
  assert.equal(result.pid, null)
  assert.ok(fs.existsSync(markerPath(home)))
})

test.each([
  '1e3\n123\n',
  '+42\n123\n',
  '0x10\n123\n',
  '4294967296\n123\n',
  '42\n1e3\n',
  '42\n+123\n',
  '42\n0x10\n',
  '42\n9007199254740992\n',
  '42\n123\nextra\n',
  '42\r123\r'
])('noncanonical or overflow wire payload fails closed: %j', body => {
  const home = tmpHome('strict-wire')
  fs.writeFileSync(markerPath(home), body)
  const result = readLiveUpdateMarker(home, { kill: ALIVE })
  assert.ok(result?.unavailable)
  assert.equal(result.pid, null)
  assert.equal(fs.readFileSync(markerPath(home), 'utf8'), body)
})

test('CRLF two-line wire payload remains compatible', () => {
  const home = tmpHome('crlf-wire')
  const now = 1_000_000_000_000
  fs.writeFileSync(markerPath(home), `4242\r\n${Math.floor(now / 1000)}\r\n`)
  const result = readLiveUpdateMarker(home, { kill: ALIVE, now: () => now })
  assert.ok(result && !result.unavailable)
  assert.equal(result.pid, 4242)
})

test('unreadable marker => conservative busy sentinel', () => {
  const home = tmpHome('unreadable')
  fs.mkdirSync(markerPath(home))
  const result = readLiveUpdateMarker(home, { kill: ALIVE })
  assert.ok(result?.unavailable)
  assert.equal(result.pid, null)
  assert.ok(fs.statSync(markerPath(home)).isDirectory())
})

test('isPidAlive: own pid is alive, impossible pid is dead', () => {
  assert.equal(isPidAlive(process.pid), true)
  assert.equal(isPidAlive(-1), false)
  assert.equal(isPidAlive(0), false)
  assert.equal(isPidAlive(NaN), false)
})

test('isPidAlive: EPERM counts as alive (process owned by another user)', () => {
  const eperm = () => {
    const err = new Error('operation not permitted')

    ;(err as any).code = 'EPERM'
    throw err
  }

  assert.equal(isPidAlive(4242, eperm), true)
})

test('isPidAlive: an indeterminate host error fails closed as alive', () => {
  const unknown = () => {
    const err = new Error('host probe unavailable')

    ;(err as any).code = 'EIO'
    throw err
  }

  assert.equal(isPidAlive(4242, unknown), true)
})

test('writeUpdateMarker writes a marker that readLiveUpdateMarker accepts', () => {
  const home = tmpHome('write')
  const now = 1_000_000_000_000
  writeUpdateMarker(home, 4242, { now: () => now })
  // The marker should be readable and report the same pid.
  const res = readLiveUpdateMarker(home, { kill: ALIVE, now: () => now })
  assert.ok(res, 'marker written by writeUpdateMarker should be detected as live')
  assert.equal(res.pid, 4242)
  assert.ok(fs.existsSync(markerPath(home)), 'marker file should exist after write')
  assert.equal(fs.readFileSync(markerPath(home), 'utf8').split(/\r?\n/).filter(Boolean).length, 2)
  assert.deepEqual(
    fs.readdirSync(home).filter(entry => entry.endsWith('.claim')),
    [],
    'the staged no-clobber claim is cleaned'
  )
})

test('writeUpdateMarker never clobbers an existing live holder', () => {
  const home = tmpHome('write-handoff-age')
  const now = 1_000_000_000_000
  const startedAt = Math.floor(now / 1000) - 300
  const original = `1010\n${startedAt}`

  writeMarker(home, 1010, startedAt)
  writeUpdateMarker(home, 2020, { kill: ALIVE, now: () => now })

  assert.equal(fs.readFileSync(markerPath(home), 'utf8'), original)
})

test('writeUpdateMarker uses the acquisition time passed to a detached script', () => {
  const home = tmpHome('write-script-acquired-at')
  const now = 1_000_000_000_000
  const startedAt = Math.floor(now / 1000) - 300

  writeUpdateMarker(home, 2020, { now: () => now, startedAt })

  const [, startedLine] = fs.readFileSync(markerPath(home), 'utf8').split('\n')
  assert.equal(Number.parseInt(startedLine, 10), startedAt)
})

test('writeUpdateMarker is best-effort (no throw on bad path)', () => {
  // A non-existent directory should not throw.
  const badHome = path.join(os.tmpdir(), 'hermes-marker-nonexistent-' + Date.now())
  assert.doesNotThrow(() => writeUpdateMarker(badHome, 4242))
})

test('writeUpdateMarker refuses an invalid pid instead of publishing malformed state', () => {
  const home = tmpHome('write-invalid-pid')
  writeUpdateMarker(home, 0)
  assert.ok(!fs.existsSync(markerPath(home)))
})

test('writeUpdateMarker + dead pid is ignored without unlocked deletion', () => {
  const home = tmpHome('write-dead')
  writeUpdateMarker(home, 999999, { now: () => Date.now() })
  const res = readLiveUpdateMarker(home, { kill: DEAD })
  assert.equal(res, null)
  assert.ok(fs.existsSync(markerPath(home)), 'Python/Rust will clean it under the shared mutex')
})

// ---------------------------------------------------------------------------
// updateHandoffConflict (#75778)
//
// A retried "Update" click must not spawn a second updater over a still-live
// one. writeUpdateMarker's no-clobber publication is the final race barrier
// after this early user-facing check.
// ---------------------------------------------------------------------------

test('no marker => hand-off is not blocked', () => {
  const home = tmpHome('conflict-none')
  assert.equal(updateHandoffConflict(home, { kill: ALIVE }), null)
})

test('a different live updater already owns the marker => hand-off is blocked', () => {
  const home = tmpHome('conflict-live')
  const now = 1_000_000_000_000
  writeMarker(home, 1010, Math.floor(now / 1000) - 6) // 6s old
  const conflict = updateHandoffConflict(home, { kill: ALIVE, now: () => now })
  assert.ok(conflict, 'a live foreign updater must block a new hand-off')
  assert.equal(conflict.pid, 1010)
  assert.match(conflict.message, /already running/)
  assert.match(conflict.message, /PID 1010/)
  assert.match(conflict.message, /6s/)
})

test('a dead-pid marker does not block a hand-off', () => {
  const home = tmpHome('conflict-dead')
  writeMarker(home, 999999, Math.floor(Date.now() / 1000))
  assert.equal(updateHandoffConflict(home, { kill: DEAD }), null)
})

test('a malformed marker blocks a hand-off without deleting it', () => {
  const home = tmpHome('conflict-malformed')
  fs.writeFileSync(markerPath(home), '4242\nnot-a-lease\n')
  const conflict = updateHandoffConflict(home, { kill: ALIVE })
  assert.ok(conflict)
  assert.equal(conflict.pid, null)
  assert.match(conflict.message, /cannot verify/)
  assert.ok(fs.existsSync(markerPath(home)))
})

test('an old marker with a live pid still blocks a hand-off', () => {
  const home = tmpHome('conflict-expired')
  const now = 1_000_000_000_000
  writeMarker(home, 1010, Math.floor((now - UPDATE_MARKER_MAX_AGE_MS - 60_000) / 1000))
  const conflict = updateHandoffConflict(home, { kill: ALIVE, now: () => now })
  assert.ok(conflict)
  assert.equal(conflict.pid, 1010)
})

test('minutes-scale elapsed time is formatted as "Nm Ss"', () => {
  const home = tmpHome('conflict-minutes')
  const now = 1_000_000_000_000
  writeMarker(home, 1010, Math.floor(now / 1000) - 125) // 2m 5s old
  const conflict = updateHandoffConflict(home, { kill: ALIVE, now: () => now })
  assert.ok(conflict)
  assert.match(conflict.message, /2m 5s/)
})
