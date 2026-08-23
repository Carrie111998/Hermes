import { spawn } from 'node:child_process'

import assert from 'node:assert/strict'

import { test } from 'vitest'

import { deadPidError, isPidAlive } from './process-liveness'

// A PID guaranteed not to exist: spawn a child that exits immediately and wait
// for its close event so the PID is released and can never be mistaken for
// alive.
async function deadPid(): Promise<number> {
  const child = spawn(process.execPath, ['-e', 'process.exit(0)'])
  await new Promise<void>((resolve, reject) => {
    child.once('exit', () => resolve())
    child.once('error', reject)
  })
  return child.pid!
}

test('isPidAlive returns true for a live process', () => {
  // process.kill(process.pid, 0) succeeds for the current process.
  assert.equal(isPidAlive(process.pid), true)
})

test('isPidAlive returns false for a dead process', async () => {
  // A dead PID maps to process.kill(pid, 0) throwing 'ESRCH'. This is the exact
  // signal processStartMarker relies on to classify a Windows orphan as
  // reap-able (false -> ESRCH -> reaped) instead of re-probing it forever.
  assert.equal(isPidAlive(await deadPid()), false)
})

test('deadPidError carries code ESRCH so probe catch blocks reap the entry', () => {
  // processIdentityMatches / backendParentMatches map code === 'ESRCH' to
  // `false`, and reapOrphans treats `false` as "reap this dead backend".
  assert.equal((deadPidError() as NodeJS.ErrnoException).code, 'ESRCH')
})
