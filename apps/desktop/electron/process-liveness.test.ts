import { spawn } from 'node:child_process'

import assert from 'node:assert/strict'

import { test } from 'vitest'

import { deadPidError, isPidAlive } from './process-liveness'

// A PID guaranteed not to exist: spawn a child that exits immediately and wait
// for its close event so the PID is released and can never be mistaken for
// alive. Windows is aggressive about PID reuse, so verify the released PID
// actually reports dead and retry with a fresh child until one does, rather
// than asserting on a PID the OS may have handed to a new process.
async function deadPid(): Promise<number> {
  for (let attempt = 0; attempt < 10; attempt++) {
    const child = spawn(process.execPath, ['-e', 'process.exit(0)'])
    await new Promise<void>((resolve, reject) => {
      child.once('exit', () => resolve())
      child.once('error', reject)
    })
    if (!isPidAlive(child.pid!)) {
      return child.pid!
    }
  }
  throw new Error('unable to obtain a dead PID after 10 attempts')
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

test('isPidAlive treats degenerate pids as alive, never as dead', () => {
  // Degenerate pids must NEVER report "dead": process.kill(0, 0) signals the
  // caller's *entire process group* and succeeds, kill(-1) also succeeds on
  // Node, and kill(NaN) throws ERR_INVALID_ARG_TYPE — none of which is ESRCH.
  // Because isPidAlive only treats ESRCH as "definitely dead", all of these
  // resolve to `true` and fall through to the platform probe for verification,
  // so garbage ownership entries can never be reaped as dead. Pin that.
  assert.equal(isPidAlive(0), true)
  assert.equal(isPidAlive(-1), true)
  assert.equal(isPidAlive(Number.NaN), true)
})

test('deadPidError carries code ESRCH so probe catch blocks reap the entry', () => {
  // processIdentityMatches / backendParentMatches map code === 'ESRCH' to
  // `false`, and reapOrphans treats `false` as "reap this dead backend".
  assert.equal((deadPidError() as NodeJS.ErrnoException).code, 'ESRCH')
})
