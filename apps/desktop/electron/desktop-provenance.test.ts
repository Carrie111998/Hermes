import { strict as assert } from 'node:assert'

import { test } from 'vitest'

import {
  desktopPublicKeyFingerprint,
  desktopTextHash,
  generateDesktopSigningIdentity,
  signDesktopPayload,
  TrustedGestureLedger,
  verifyDesktopPayload
} from './desktop-provenance'

function payload(text = 'log duty') {
  return {
    version: 2 as const,
    event_id: 'event-1',
    observed_at: '2026-07-21T15:00:00.000Z',
    installation_id: 'install-1',
    os_account: 'darwin:501',
    app_identity: 'TEAM:io.hermes.desktop',
    app_instance_id: 'instance-1',
    window_id: '7',
    text_hash: desktopTextHash(text)
  }
}

test('desktop prompt signatures bind every payload field', () => {
  const keys = generateDesktopSigningIdentity()
  const signed = payload()
  const signature = signDesktopPayload(keys.privateKeyPem, signed)

  assert.equal(verifyDesktopPayload(keys.publicKeyPem, signed, signature), true)
  assert.equal(verifyDesktopPayload(keys.publicKeyPem, { ...signed, window_id: '8' }, signature), false)
  assert.match(desktopPublicKeyFingerprint(keys.publicKeyPem), /^[a-f0-9]{64}$/)
})

test('trusted gesture is route-independent, recoverable, and immutable', () => {
  const ledger = new TrustedGestureLedger()
  const now = 1_000
  const firstHash = desktopTextHash('first')

  const begun = ledger.begin(7, '3', firstHash, now)

  assert.ok(begun)
  assert.deepEqual(ledger.mint(7, begun.gestureToken, firstHash, now + 1), begun)
  assert.deepEqual(ledger.mint(7, begun.gestureToken, firstHash, now + 60_000), begun)
  assert.equal(ledger.mint(8, begun.gestureToken, firstHash, now + 2), null)
  assert.equal(ledger.mint(7, begun.gestureToken, desktopTextHash('changed'), now + 2), null)
  assert.equal('profile' in payload(), false)
  assert.equal('session_id' in payload(), false)
})

test('trusted composer gesture cannot authorize substituted renderer text', () => {
  const ledger = new TrustedGestureLedger()
  const now = 1_000
  const intended = desktopTextHash('log my duty')

  const begun = ledger.begin(7, '3', intended, now)

  assert.ok(begun)
  assert.equal(
    ledger.mint(7, begun.gestureToken, desktopTextHash('delete everything'), now + 1),
    null
  )
  assert.ok(ledger.mint(7, begun.gestureToken, intended, now + 2))
})

test('retired, expired, and superseded gestures fail closed', () => {
  const ledger = new TrustedGestureLedger()
  const now = 1_000
  const textHash = desktopTextHash('log my duty')
  const first = ledger.begin(7, '3', textHash, now)

  assert.ok(first)
  assert.equal(ledger.retire(7, first.gestureToken, first.eventId), true)
  assert.equal(ledger.mint(7, first.gestureToken, textHash, now + 1), null)

  const second = ledger.begin(7, '3', textHash, now + 1_000)

  assert.ok(second)
  assert.notEqual(second.eventId, first.eventId)
  assert.equal(ledger.mint(7, second.gestureToken, textHash, now + 10 * 60_000 + 1_001), null)
})
