import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { test } from 'vitest'

import { isMain } from './utils.mjs'

function withEntrypoint(value, callback) {
  const hadEntrypoint = Object.prototype.hasOwnProperty.call(process.argv, 1)
  const original = process.argv[1]
  try {
    if (value === undefined) {
      delete process.argv[1]
    } else {
      process.argv[1] = value
    }
    callback()
  } finally {
    if (hadEntrypoint) {
      process.argv[1] = original
    } else {
      delete process.argv[1]
    }
  }
}

test('isMain returns false when the loader omits process.argv[1]', () => {
  withEntrypoint(undefined, () => {
    assert.equal(isMain(import.meta.url), false)
  })
})

test('isMain matches the current module entrypoint URL', () => {
  withEntrypoint(fileURLToPath(import.meta.url), () => {
    assert.equal(isMain(import.meta.url), true)
  })
})
