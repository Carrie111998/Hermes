import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { test } from 'vitest'

const ELECTRON_DIR = path.dirname(fileURLToPath(import.meta.url))

function read(name: string) {
  return fs.readFileSync(path.join(ELECTRON_DIR, name), 'utf8').replace(/\r\n/g, '\n')
}

test('main idle-stop uses quiet/soft teardown and does not write active-profile.json', () => {
  const source = read('main.ts')
  assert.match(source, /from '\.\/primary-idle'/)
  assert.match(source, /hermes:backend:usage/)
  assert.match(source, /teardownPrimaryBackendAndWait\(\{\s*soft:\s*true\s*\}\)/)
  assert.match(source, /reason: 'idle-stop'|['"]idle-stop['"]/)

  const idleFnStart = source.indexOf('async function idleStopPrimaryIfStillSafe')
  assert.notEqual(idleFnStart, -1)
  const idleFn = source.slice(idleFnStart, idleFnStart + 1800)
  assert.doesNotMatch(idleFn, /writeActiveDesktopProfile/)
  assert.doesNotMatch(idleFn, /prepareProfileDeleteRequest/)
  assert.doesNotMatch(idleFn, /mainWindow\?\.reload\(\)/)
  // Latch must survive waitForBackendExit (which can resolve before 'exit'
  // when SIGKILL times out). Clearing here races the later exit toast.
  assert.doesNotMatch(idleFn, /primaryIdleStopChild = null/)
})

test('preload exposes reportBackendUsage', () => {
  const source = read('preload.ts')
  assert.match(source, /reportBackendUsage:\s*payload\s*=>\s*ipcRenderer\.invoke\('hermes:backend:usage'/)
})
