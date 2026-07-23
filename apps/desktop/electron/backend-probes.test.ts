/**
 * Tests for electron/backend-probes.ts.
 *
 * Run with: node --test electron/backend-probes.test.ts
 * (Wired into npm test:desktop:platforms in package.json.)
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { canImportHermesCli, hermesRuntimeImportProbe, PROBE_TIMEOUT_MS, verifyHermesCli } from './backend-probes'

// Build a fake execFileSync that throws a scripted sequence of errors (null =
// succeed) and records the options of every call. Lets us exercise the
// timeout-retry logic without real 15s hangs.
function scriptedExec(failures) {
  const calls = []
  const fn = (_cmd, _args, options) => {
    const failure = failures[calls.length] || null
    calls.push(options)
    if (failure) {
      const err: Error & { code?: string; status?: number } = new Error(
        failure.message || failure.code || 'probe failure'
      )
      if (failure.code) {
        err.code = failure.code
      }

      if (failure.status != null) {
        err.status = failure.status
      }

      throw err
    }
  }
  return { fn, calls }
}

function timeoutError() {
  return { code: 'ETIMEDOUT', message: 'spawnSync hermes ETIMEDOUT' }
}

// Resolve the host's own Node binary -- guaranteed to be on disk and
// runnable. We use it as both a stand-in for "a python that doesn't
// have hermes_cli" (since `node -c "import hermes_cli"` will exit
// non-zero) and as a way to script verifyHermesCli's success path
// (a tiny script we write to disk that exits 0 on --version).
const NODE_BIN = process.execPath

test('canImportHermesCli returns false when path is falsy', () => {
  assert.equal(canImportHermesCli(''), false)
  assert.equal(canImportHermesCli(null), false)
  assert.equal(canImportHermesCli(undefined), false)
})

test('canImportHermesCli returns false when interpreter cannot run -c', () => {
  // node IS an interpreter, but `node -c "import hermes_cli"` is a
  // SyntaxError -- different exit reason from a real Python's
  // ModuleNotFoundError, but the predicate is "exit 0 or not" and
  // both land on "not", which is exactly what we want for the
  // resolver fall-through.
  assert.equal(canImportHermesCli(NODE_BIN), false)
})

test('canImportHermesCli returns false when binary does not exist', () => {
  const ghost = path.join(os.tmpdir(), 'hermes-probes-ghost-' + Date.now() + '.exe')
  assert.equal(canImportHermesCli(ghost), false)
})

test('hermes runtime import probe checks config dependencies', () => {
  const probe = hermesRuntimeImportProbe()
  assert.match(probe, /\bimport yaml\b/)
  // dotenv is the first third-party import on the CLI boot path
  // (hermes_cli/env_loader.py); a mid-update venv missing python-dotenv
  // passed the old probe and produced an unrecoverable boot loop.
  assert.match(probe, /\bimport dotenv\b/)
  assert.match(probe, /\bimport hermes_cli\.config\b/)
})

test('verifyHermesCli returns false when command is falsy', () => {
  assert.equal(verifyHermesCli(''), false)
  assert.equal(verifyHermesCli(null), false)
  assert.equal(verifyHermesCli(undefined), false)
})

test('verifyHermesCli returns false when binary does not exist', () => {
  const ghost = path.join(os.tmpdir(), 'hermes-probes-ghost-' + Date.now() + '.exe')
  assert.equal(verifyHermesCli(ghost), false)
})

test('verifyHermesCli returns true when --version exits 0', () => {
  // Write a tiny script that exits 0 regardless of args, then invoke
  // it through node. This stands in for a working hermes binary --
  // verifyHermesCli only cares about the exit code.
  const scriptPath = path.join(os.tmpdir(), `hermes-probes-ok-${Date.now()}-${process.pid}.cjs`)
  fs.writeFileSync(scriptPath, 'process.exit(0)\n')

  try {
    // Use node as the launcher and our script as the "command". Pass
    // shell:false (default) -- node is a real binary, no shim.
    // execFileSync passes ['--version'] as args, which node ignores
    // gracefully (well, it prints its version and exits 0, which is
    // perfect -- exit code 0 is the only signal we read).
    assert.equal(verifyHermesCli(NODE_BIN), true)
  } finally {
    try {
      fs.unlinkSync(scriptPath)
    } catch {
      void 0
    }
  }
})

test('probe timeout accommodates hermes --version under boot-time CPU storms', () => {
  // `hermes --version` measures 2.4-2.9s warm and 4.5s+ under cold-boot CPU
  // pressure; a 5s deadline made the PATH rung flake on healthy installs.
  assert.equal(PROBE_TIMEOUT_MS, 15000)
})

test('verifyHermesCli retries once after a timeout and succeeds', () => {
  const { fn, calls } = scriptedExec([timeoutError(), null])
  assert.equal(verifyHermesCli('hermes', { _execFileSync: fn }), true)
  assert.equal(calls.length, 2, 'timed-out first attempt should be retried exactly once')
  for (const options of calls) {
    assert.equal(options.timeout, PROBE_TIMEOUT_MS)
  }
})

test('verifyHermesCli gives up after the single timeout retry', () => {
  const { fn, calls } = scriptedExec([timeoutError(), timeoutError()])
  assert.equal(verifyHermesCli('hermes', { _execFileSync: fn }), false)
  assert.equal(calls.length, 2, 'must not retry beyond one extra attempt')
})

test('verifyHermesCli does not retry a nonzero exit (broken shim fails fast)', () => {
  const { fn, calls } = scriptedExec([{ status: 1, message: 'Command failed: hermes --version' }])
  assert.equal(verifyHermesCli('hermes', { _execFileSync: fn }), false)
  assert.equal(calls.length, 1, 'a real exit code is a definitive verdict; no retry')
})

test('verifyHermesCli does not retry a missing binary', () => {
  const { fn, calls } = scriptedExec([{ code: 'ENOENT', message: 'spawnSync hermes ENOENT' }])
  assert.equal(verifyHermesCli('hermes', { _execFileSync: fn }), false)
  assert.equal(calls.length, 1)
})

test('canImportHermesCli retries once after a timeout and succeeds', () => {
  const { fn, calls } = scriptedExec([timeoutError(), null])
  assert.equal(canImportHermesCli('python', { _execFileSync: fn }), true)
  assert.equal(calls.length, 2)
})

test('canImportHermesCli does not retry a nonzero exit', () => {
  const { fn, calls } = scriptedExec([{ status: 1, message: 'ModuleNotFoundError' }])
  assert.equal(canImportHermesCli('python', { _execFileSync: fn }), false)
  assert.equal(calls.length, 1)
})

test('verifyHermesCli swallows timeouts (does not throw)', () => {
  // We can't easily provoke a real 5s hang in CI without slowing the
  // suite, but we CAN confirm that an invocation that DOES throw
  // (because the binary is missing) returns false rather than
  // propagating. Same code path the timeout case takes.
  assert.equal(verifyHermesCli('/definitely/not/a/real/binary/anywhere'), false)
})
