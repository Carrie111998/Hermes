// Regression guard for findPythonForRoot() in main.ts.
//
// main.ts has no module.exports for this function (it's an Electron
// entry-point internal, not part of the pure-helper surface extracted into
// windows-hermes-path.ts), so this follows the repo's source-assertion test
// pattern: read the compiled source text and assert on it directly.
//
// This used to be one of four checks in windows-hermes-resolution.test.cjs.
// The other three (findOnPath PATHEXT ordering, the --update/--repair
// haveRealInstall gate, and the venv-python probe-before-trust behavior) are
// now covered by real unit tests against the extracted pure helpers in
// windows-hermes-path.test.ts (buildPathExtCandidates, chooseUpdaterArgs,
// resolveVenvHermesCommand) -- better coverage than a source regex, so those
// three were not re-created here. findPythonForRoot() itself was NOT
// extracted, so its regression guard stays in this source-assertion form:
//   findPythonForRoot() gates BOTH the HERMES_DESKTOP_PYTHON override and
//   every venv candidate path behind isSpawnablePythonExe(), in addition to
//   fileExists(). A Microsoft Store "App Execution Alias" python.exe (a
//   reparse-point stub) passes fileExists() yet throws spawn EPERM in the
//   desktop's detached launch context (boot failure, 2026-07-09); without
//   the isSpawnablePythonExe() guard the resolver hands back that stub
//   instead of falling through to a real interpreter.

import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

import { test } from 'vitest'

function readMain() {
  return fs.readFileSync(path.join(__dirname, 'main.ts'), 'utf8').replace(/\r\n/g, '\n')
}

test('findPythonForRoot rejects non-spawnable Python (app-execution-alias stubs) for override and venv candidates', () => {
  const source = readMain()
  const fnStart = source.indexOf('function findPythonForRoot(')
  assert.notEqual(fnStart, -1, 'findPythonForRoot must exist in main.ts')
  const fnEnd = source.indexOf('\nfunction ', fnStart + 1)
  const body = source.slice(fnStart, fnEnd === -1 ? undefined : fnEnd)

  // The HERMES_DESKTOP_PYTHON override must be gated on spawnability, not just
  // fileExists -- a reparse-point alias passes fileExists yet spawn-EPERMs.
  assert.match(
    body,
    /override && fileExists\(override\) && isSpawnablePythonExe\(override/,
    'override must be guarded by isSpawnablePythonExe so an app-execution-alias falls through'
  )
  // Venv candidates get the same guard.
  assert.match(
    body,
    /fileExists\(candidate\) && isSpawnablePythonExe\(candidate/,
    'venv candidates must be guarded by isSpawnablePythonExe too'
  )
})
