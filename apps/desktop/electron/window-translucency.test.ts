import assert from 'node:assert/strict'

import { test } from 'vitest'

import { initialWindowTranslucencyOptions, opacityForTranslucency } from './window-translucency'

test('opacityForTranslucency maps the control range to usable opacity', () => {
  assert.equal(opacityForTranslucency(0), 1)
  assert.equal(opacityForTranslucency(50), 0.65)
  assert.ok(Math.abs(opacityForTranslucency(100) - 0.3) < Number.EPSILON)
})

test('initialWindowTranslucencyOptions omits opacity for opaque Windows starts', () => {
  const options = initialWindowTranslucencyOptions(0, true)

  assert.deepEqual(options, {})
  assert.equal(Object.hasOwn(options, 'opacity'), false)
})

test('initialWindowTranslucencyOptions retains opacity for translucent Windows starts', () => {
  assert.deepEqual(initialWindowTranslucencyOptions(50, true), { opacity: 0.65 })
  assert.ok(Math.abs(initialWindowTranslucencyOptions(100, true).opacity! - 0.3) < Number.EPSILON)
})

test('initialWindowTranslucencyOptions preserves non-Windows behavior', () => {
  assert.deepEqual(initialWindowTranslucencyOptions(0, false), { opacity: 1 })
})
