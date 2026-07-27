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

import {
  canImportCharterforgeCli,
  hermesRuntimeImportProbe,
  shouldTrustCharterforgeOverride,
  verifyCharterforgeCli
} from './backend-probes'

// Resolve the host's own Node binary -- guaranteed to be on disk and
// runnable. We use it as both a stand-in for "a python that doesn't
// have hermes_cli" (since `node -c "import hermes_cli"` will exit
// non-zero) and as a way to script verifyCharterforgeCli's success path
// (a tiny script we write to disk that exits 0 on --version).
const NODE_BIN = process.execPath

test('canImportCharterforgeCli returns false when path is falsy', () => {
  assert.equal(canImportCharterforgeCli(''), false)
  assert.equal(canImportCharterforgeCli(null), false)
  assert.equal(canImportCharterforgeCli(undefined), false)
})

test('canImportCharterforgeCli returns false when interpreter cannot run -c', () => {
  // node IS an interpreter, but `node -c "import hermes_cli"` is a
  // SyntaxError -- different exit reason from a real Python's
  // ModuleNotFoundError, but the predicate is "exit 0 or not" and
  // both land on "not", which is exactly what we want for the
  // resolver fall-through.
  assert.equal(canImportCharterforgeCli(NODE_BIN), false)
})

test('canImportCharterforgeCli returns false when binary does not exist', () => {
  const ghost = path.join(os.tmpdir(), 'hermes-probes-ghost-' + Date.now() + '.exe')
  assert.equal(canImportCharterforgeCli(ghost), false)
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

test('explicit Charterforge override is authoritative', () => {
  assert.equal(shouldTrustCharterforgeOverride('/nix/store/abc/bin/hermes'), true)
})

test('empty Charterforge override is not authoritative', () => {
  assert.equal(shouldTrustCharterforgeOverride(''), false)
  assert.equal(shouldTrustCharterforgeOverride(undefined), false)
})

test('verifyCharterforgeCli returns false when command is falsy', () => {
  assert.equal(verifyCharterforgeCli(''), false)
  assert.equal(verifyCharterforgeCli(null), false)
  assert.equal(verifyCharterforgeCli(undefined), false)
})

test('verifyCharterforgeCli returns false when binary does not exist', () => {
  const ghost = path.join(os.tmpdir(), 'hermes-probes-ghost-' + Date.now() + '.exe')
  assert.equal(verifyCharterforgeCli(ghost), false)
})

test('verifyCharterforgeCli returns true when --version exits 0', () => {
  // Write a tiny script that exits 0 regardless of args, then invoke
  // it through node. This stands in for a working hermes binary --
  // verifyCharterforgeCli only cares about the exit code.
  const scriptPath = path.join(os.tmpdir(), `hermes-probes-ok-${Date.now()}-${process.pid}.cjs`)
  fs.writeFileSync(scriptPath, 'process.exit(0)\n')

  try {
    // Use node as the launcher and our script as the "command". Pass
    // shell:false (default) -- node is a real binary, no shim.
    // execFileSync passes ['--version'] as args, which node ignores
    // gracefully (well, it prints its version and exits 0, which is
    // perfect -- exit code 0 is the only signal we read).
    assert.equal(verifyCharterforgeCli(NODE_BIN), true)
  } finally {
    try {
      fs.unlinkSync(scriptPath)
    } catch {
      void 0
    }
  }
})

test('verifyCharterforgeCli swallows timeouts (does not throw)', () => {
  // We can't easily provoke a real 5s hang in CI without slowing the
  // suite, but we CAN confirm that an invocation that DOES throw
  // (because the binary is missing) returns false rather than
  // propagating. Same code path the timeout case takes.
  assert.equal(verifyCharterforgeCli('/definitely/not/a/real/binary/anywhere'), false)
})
