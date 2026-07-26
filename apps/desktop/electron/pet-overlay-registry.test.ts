/**
 * Unit tests for the per-profile pet overlay registry + IPC sender-role
 * enforcement. The shared preload exposes every petOverlay.* API to every
 * renderer, so the security guarantee lives here: a profile is derived from
 * `event.sender.id`, never a renderer-supplied field, and unknown senders are
 * rejected.
 */

import assert from 'node:assert/strict'

import { test } from 'vitest'

import { isPrimarySender, overlayProfileKey, PetOverlayRegistry } from './pet-overlay-registry'

// Minimal stand-ins for the Electron shapes the registry touches.
function fakeWin(wcId) {
  let destroyed = false

  return {
    webContents: { id: wcId },
    isDestroyed: () => destroyed,
    _destroy: () => {
      destroyed = true
    }
  }
}

const sender = id => ({ id })

test('overlayProfileKey trims and defaults blank to "default"', () => {
  assert.equal(overlayProfileKey('apollo'), 'apollo')
  assert.equal(overlayProfileKey('  apollo  '), 'apollo')
  assert.equal(overlayProfileKey(''), 'default')
  assert.equal(overlayProfileKey(undefined), 'default')
  assert.equal(overlayProfileKey(null), 'default')
})

test('isPrimarySender authenticates only the main window webContents (test 30)', () => {
  assert.equal(isPrimarySender(sender(1), 1), true)
  // A different renderer (e.g. an overlay) is not the primary.
  assert.equal(isPrimarySender(sender(2), 1), false)
  // No live main window → nobody is the primary.
  assert.equal(isPrimarySender(sender(1), null), false)
  assert.equal(isPrimarySender(null, 1), false)
})

test('register tracks one window per profile independently (test 18)', () => {
  const reg = new PetOverlayRegistry()
  const apollo = fakeWin(10)
  const nova = fakeWin(11)

  reg.register('apollo', apollo)
  reg.register('nova', nova)

  assert.equal(reg.get('apollo'), apollo)
  assert.equal(reg.get('nova'), nova)
  assert.deepEqual(reg.profiles().sort(), ['apollo', 'nova'])
})

test('unregister evicts from BOTH maps using the captured wcId (test 19)', () => {
  const reg = new PetOverlayRegistry()
  const apollo = fakeWin(10)
  const wcId = reg.register('apollo', apollo)

  assert.equal(wcId, 10)
  assert.equal(reg.profileForSender(sender(10)), 'apollo')

  reg.unregister('apollo', wcId)

  assert.equal(reg.get('apollo'), undefined)
  assert.equal(reg.profileForSender(sender(10)), null)
  assert.equal(reg.has('apollo'), false)
})

test('windowForSender returns the sender\u2019s OWN window, never another profile\u2019s (test 32)', () => {
  const reg = new PetOverlayRegistry()
  const apollo = fakeWin(10)
  const nova = fakeWin(11)
  reg.register('apollo', apollo)
  reg.register('nova', nova)

  // The sender's id selects its profile's window — a renderer cannot reach a
  // different profile's overlay by supplying a profile field; only its own id
  // matters.
  assert.equal(reg.windowForSender(sender(10)), apollo)
  assert.equal(reg.windowForSender(sender(11)), nova)
})

test('windowForSender rejects an unknown sender (test 31)', () => {
  const reg = new PetOverlayRegistry()
  reg.register('apollo', fakeWin(10))

  // A webContents id that is not a registered overlay (the primary window, or
  // some other renderer) gets nothing.
  assert.equal(reg.windowForSender(sender(999)), null)
  assert.equal(reg.windowForSender(sender(1)), null)
})

test('windowForSender returns null for a destroyed overlay window', () => {
  const reg = new PetOverlayRegistry()
  const apollo = fakeWin(10)
  reg.register('apollo', apollo)
  apollo._destroy()

  assert.equal(reg.windowForSender(sender(10)), null)
})

test('profileForSender derives the bound profile from the sender id (test 32)', () => {
  const reg = new PetOverlayRegistry()
  reg.register('apollo', fakeWin(10))

  assert.equal(reg.profileForSender(sender(10)), 'apollo')
  assert.equal(reg.profileForSender(sender(11)), null)
  assert.equal(reg.profileForSender(null), null)
})

test('state push targets only the named profile\u2019s window (test 55)', () => {
  // The main-process state handler looks up the target by profile key; only that
  // profile's window receives the push.
  const reg = new PetOverlayRegistry()
  const apollo = fakeWin(10)
  const nova = fakeWin(11)
  reg.register('apollo', apollo)
  reg.register('nova', nova)

  const target = reg.get(overlayProfileKey('apollo'))
  assert.equal(target, apollo)
  assert.notEqual(target, nova)
})
