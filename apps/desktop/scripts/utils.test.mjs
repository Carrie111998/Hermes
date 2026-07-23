import assert from 'node:assert/strict'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { afterEach, test } from 'vitest'

import { isMain } from '../scripts/utils.mjs'

const originalArgv1 = process.argv[1]

afterEach(() => {
  process.argv[1] = originalArgv1
})

test('isMain is true when the module is the entry script', () => {
  const script = path.resolve(fileURLToPath(new URL('.', import.meta.url)), 'assert-dist-built.mjs')
  process.argv[1] = script
  assert.equal(isMain(pathToFileURL(script).href), true)
})

test('isMain is false when some other file is the entry script', () => {
  const dir = fileURLToPath(new URL('.', import.meta.url))
  process.argv[1] = path.join(dir, 'write-build-stamp.mjs')
  assert.equal(isMain(pathToFileURL(path.join(dir, 'assert-dist-built.mjs')).href), false)
})

// `node --input-type=module -e '...'`, --eval and the REPL all leave argv[1]
// unset. pathToFileURL(undefined) throws, so an imported script that asks
// "was I run directly?" used to crash at import time instead of getting false.
test('isMain is false when there is no entry script', () => {
  delete process.argv[1]
  assert.equal(isMain(import.meta.url), false)
})
