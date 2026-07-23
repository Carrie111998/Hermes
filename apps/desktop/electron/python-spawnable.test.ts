// Unit tests for isSpawnablePythonExe — the guard that stops the desktop from
// handing a Microsoft Store "App Execution Alias" python.exe to
// child_process.spawn(). Those aliases are IO_REPARSE_TAG_APPEXECLINK reparse
// stubs with no real PE payload; spawning one in the desktop's detached,
// windowless launch context throws EPERM (boot failure, 2026-07-09). libuv
// surfaces them via lstat as symbolic links whose target lives under
// %ProgramFiles%\WindowsApps and cannot be followed (statSync → EACCES/EPERM).
//
// Ported verbatim from python-spawnable.test.cjs (node:test) when the electron
// test suite moved to vitest (`vitest run --project electron` collects only
// *.test.ts, so the .cjs file had silently dropped out of the run).

import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { isSpawnablePythonExe } from './python-spawnable.cjs'

// Build a Stats-like object with just the methods the helper touches.
function fakeStats({ size = 0, symlink = false, file = true } = {}) {
  return {
    size,
    isSymbolicLink: () => symlink,
    isFile: () => file
  }
}

// Minimal fs double: lstatSync/statSync driven by a per-path table.
function fakeFs(table) {
  return {
    lstatSync(p) {
      const entry = table[p]
      if (!entry || !entry.lstat) {
        throw Object.assign(new Error(`ENOENT: ${p}`), { code: 'ENOENT' })
      }
      if (entry.lstat instanceof Error) {
        throw entry.lstat
      }

      return entry.lstat
    },
    statSync(p) {
      const entry = table[p]
      if (!entry || !entry.stat) {
        throw Object.assign(new Error(`ENOENT: ${p}`), { code: 'ENOENT' })
      }
      if (entry.stat instanceof Error) {
        throw entry.stat
      }

      return entry.stat
    }
  }
}

function collectingLog() {
  const lines = []
  const log = message => lines.push(String(message))
  return { lines, log }
}

test('rejects a Microsoft Store app-execution-alias reparse stub and logs it', () => {
  const aliasPath =
    'C:\\Users\\diego\\AppData\\Local\\Microsoft\\WindowsApps\\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\\python.exe'
  const eacces = Object.assign(new Error('EACCES: permission denied'), { code: 'EACCES' })
  // Real-world shape: lstat says symlink (size = reparse buffer), follow throws.
  const fsDouble = fakeFs({
    [aliasPath]: { lstat: fakeStats({ size: 111, symlink: true, file: false }), stat: eacces }
  })
  const { lines, log } = collectingLog()

  const result = isSpawnablePythonExe(aliasPath, { fs: fsDouble, log })

  assert.equal(result, false, 'app-execution-alias must not be considered spawnable')
  assert.equal(lines.length, 1, 'exactly one log line should fire')
  assert.match(lines[0], /Ignoring non-spawnable Python \(app-execution-alias reparse stub\):/)
  assert.match(lines[0], /python\.exe$/)
})

test('rejects a plain 0-byte stub file and logs it', () => {
  const stubPath = 'C:\\stub\\python.exe'
  const fsDouble = fakeFs({
    [stubPath]: { lstat: fakeStats({ size: 0, symlink: false, file: true }), stat: fakeStats({ size: 0, file: true }) }
  })
  const { lines, log } = collectingLog()

  const result = isSpawnablePythonExe(stubPath, { fs: fsDouble, log })

  assert.equal(result, false, 'a 0-byte python.exe can never be a real interpreter')
  assert.equal(lines.length, 1)
  assert.match(lines[0], /Ignoring non-spawnable Python/)
})

test('accepts a real python.exe (regular file, non-zero size) without logging', () => {
  const realPath = 'C:\\Python311\\python.exe'
  const fsDouble = fakeFs({
    [realPath]: {
      lstat: fakeStats({ size: 103_936, symlink: false, file: true }),
      stat: fakeStats({ size: 103_936, file: true })
    }
  })
  const { lines, log } = collectingLog()

  const result = isSpawnablePythonExe(realPath, { fs: fsDouble, log })

  assert.equal(result, true, 'a normal interpreter must be accepted')
  assert.equal(lines.length, 0, 'no log line should fire for a healthy interpreter')
})

test('accepts a healthy symlink that resolves to a real interpreter (POSIX venv)', () => {
  const venvPython = '/home/diego/proj/.venv/bin/python'
  const fsDouble = fakeFs({
    [venvPython]: {
      lstat: fakeStats({ size: 20, symlink: true, file: false }),
      stat: fakeStats({ size: 5_872_640, file: true })
    }
  })
  const { lines, log } = collectingLog()

  const result = isSpawnablePythonExe(venvPython, { fs: fsDouble, log })

  assert.equal(result, true, 'a symlink that follows to a real file is spawnable')
  assert.equal(lines.length, 0)
})

test('returns false when the candidate cannot be lstat-ed', () => {
  const { lines, log } = collectingLog()
  const result = isSpawnablePythonExe('C:\\nope\\python.exe', { fs: fakeFs({}), log })
  assert.equal(result, false)
  assert.equal(lines.length, 0, 'a missing candidate is silent — the caller just falls through')
})

test('returns false for empty/invalid input', () => {
  assert.equal(isSpawnablePythonExe('', { fs: fakeFs({}) }), false)
  assert.equal(isSpawnablePythonExe(null, { fs: fakeFs({}) }), false)
  assert.equal(isSpawnablePythonExe(undefined, { fs: fakeFs({}) }), false)
})

test('real-fs: a genuine 0-byte python.exe file is rejected', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-pystub-'))
  const stub = path.join(dir, 'python.exe')
  fs.writeFileSync(stub, '')
  try {
    const { lines, log } = collectingLog()
    assert.equal(isSpawnablePythonExe(stub, { log }), false)
    assert.equal(lines.length, 1)
    assert.match(lines[0], /Ignoring non-spawnable Python/)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})
