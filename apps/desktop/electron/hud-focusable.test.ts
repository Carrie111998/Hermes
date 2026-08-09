import assert from 'node:assert/strict'

import { test } from 'vitest'

import { makeHudWindowFocusable } from './hud-focusable'

test('makes the HUD panel focusable so macOS lets it become the key window', () => {
  const calls: boolean[] = []

  makeHudWindowFocusable({ setFocusable: focusable => calls.push(focusable) })

  assert.deepEqual(calls, [true])
})
