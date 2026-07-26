// Regression guards for the Windows Desktop runtime interpreter choice.
//
// The active install backend is always passed through ensureRuntime before the
// process spawn. On Windows, ensureRuntime must replace the venv launcher with
// the base interpreter recorded in pyvenv.cfg so the long-lived Desktop backend
// does not hold the venv launcher/native-extension lock path during updates.

import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

function readSource(filename: string) {
  return fs.readFileSync(path.join(__dirname, filename), 'utf8').replace(/\r\n/g, '\n')
}

function functionBody(source: string, declaration: string) {
  const fnStart = source.indexOf(declaration)
  assert.notEqual(fnStart, -1, `${declaration} must exist`)
  const fnEnd = source.indexOf('\nfunction ', fnStart + 1)
  return source.slice(fnStart, fnEnd === -1 ? undefined : fnEnd)
}

test('resolveBasePythonFromVenvCfg reads pyvenv.cfg home and fails closed', () => {
  const source = readSource('main.ts')
  const body = functionBody(source, 'function resolveBasePythonFromVenvCfg')

  assert.ok(body.includes("cfg.match(/^home\\s*=\\s*(.+)$/im)"), 'must parse the pyvenv.cfg home key')
  assert.ok(body.includes("path.join(baseDir, 'python.exe')"), 'must resolve the base Windows interpreter')
  assert.ok(body.includes('fileExists(basePython)'), 'must verify the resolved interpreter exists')
  assert.ok(body.includes('catch') && body.includes('return null'), 'must fall back safely on malformed or missing config')
})

test('ensureRuntime prefers base Python on Windows and retains the venv fallback', () => {
  const source = readSource('main.ts')
  const body = functionBody(source, 'async function ensureRuntime(')

  assert.match(
    body,
    /const basePython = IS_WINDOWS \? resolveBasePythonFromVenvCfg\(VENV_ROOT\) : null/,
    'ensureRuntime must resolve the base interpreter only on Windows'
  )
  assert.match(
    body,
    /backend\.command = basePython \|\| venvPython/,
    'ensureRuntime must prefer base Python and retain the venv fallback'
  )
  assert.doesNotMatch(
    body,
    /backend\.command = getVenvPython\(VENV_ROOT\)/,
    'ensureRuntime must not overwrite the selected base interpreter with the venv launcher'
  )
})

test('the local startup path always runs ensureLocalRuntime before returning a backend', () => {
  const source = readSource('primary-backend-startup.ts')

  assert.match(
    source,
    /return \{ kind: 'local', backend: await ensureLocalRuntime\(backend\) \}/,
    'the active backend must pass through ensureRuntime before the process is spawned'
  )
})
