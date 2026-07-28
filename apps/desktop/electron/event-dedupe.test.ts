import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createEventDeduper, notificationDedupeKey } from './event-dedupe'

test('collapses the same key inside the window (two windows, one event)', () => {
  const isDup = createEventDeduper(1000)

  assert.equal(isDup('input:s1', 0), false, 'first window claims')
  assert.equal(isDup('input:s1', 5), true, 'second window is deduped')
})

test('distinct keys are independent', () => {
  const isDup = createEventDeduper(1000)

  assert.equal(isDup('input:s1', 0), false)
  assert.equal(isDup('approval:s1', 0), false, 'different kind')
  assert.equal(isDup('input:s2', 0), false, 'different session')
})

test('notification keys partition colliding session ids by normalized profile', () => {
  assert.notEqual(
    notificationDedupeKey({ kind: 'turnDone', profile: 'alpha', sessionId: 'shared' }),
    notificationDedupeKey({ kind: 'turnDone', profile: 'beta', sessionId: 'shared' })
  )
  assert.equal(
    notificationDedupeKey({ kind: 'turnDone', profile: ' default ', sessionId: 'shared' }),
    notificationDedupeKey({ kind: 'turnDone', profile: null, sessionId: 'shared' })
  )
})

test('re-fires once the window elapses', () => {
  const isDup = createEventDeduper(1000)

  assert.equal(isDup('turnDone:s1', 0), false)
  assert.equal(isDup('turnDone:s1', 999), true, 'still within window')
  assert.equal(isDup('turnDone:s1', 1000), false, 'window elapsed → fires again')
})

test('prunes stale keys so the map cannot grow unbounded', () => {
  const isDup = createEventDeduper(1000)

  for (let i = 0; i < 100; i += 1) {
    // Each far-apart key is pruned before the next, so none linger as duplicates.
    assert.equal(isDup(`turnDone:s${i}`, i * 2000), false)
  }
})
