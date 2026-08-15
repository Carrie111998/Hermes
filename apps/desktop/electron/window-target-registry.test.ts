import assert from 'node:assert/strict'

import { test } from 'vitest'

import { type BackendTarget, makeBackendTarget } from './backend-target'
import { createWindowTargetRegistry, destroyRevokedWindows } from './window-target-registry'

// A fresh registry per test keeps assertions independent. We use a plain
// incrementing id source to stand in for webContents.id without importing
// Electron; the registry only depends on the numeric id.
function makeRegistry() {
  return createWindowTargetRegistry({
    resolvePrimaryTarget: () => makeBackendTarget({ kind: 'primary' })
  })
}

let nextId = 100

function freshId() {
  return nextId++
}

test('lookup returns the default-primary target before bind', () => {
  const reg = makeRegistry()
  const id = freshId()

  assert.deepEqual(reg.lookup(id), makeBackendTarget({ kind: 'primary' }))
})

test('lookup contains a malformed primary resolver and still falls back to primary', () => {
  const reg = createWindowTargetRegistry({
    resolvePrimaryTarget: () =>
      ({ kind: 'configured-profile', profile: 'Not Valid!' }) as unknown as BackendTarget
  })

  assert.deepEqual(reg.lookup(freshId()), makeBackendTarget({ kind: 'primary' }))
})

test('bind sets an explicit target that lookup returns', () => {
  const reg = makeRegistry()
  const id = freshId()

  reg.bind(id, makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))

  assert.deepEqual(reg.lookup(id), makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
})

test('bind with a forced-local-profile target survives lookup', () => {
  const reg = makeRegistry()
  const id = freshId()

  reg.bind(id, makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))

  assert.deepEqual(reg.lookup(id), makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))
})

test('lookup returns a defensive copy that cannot mutate the stored target', () => {
  const reg = makeRegistry()
  const id = freshId()

  reg.bind(id, makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))
  const returned = reg.lookup(id)

  if (returned.kind !== 'primary') {
    returned.profile = 'Not Valid!'
  }

  assert.deepEqual(reg.lookup(id), makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))
})

test('rebinding an already-bound window replaces the target', () => {
  const reg = makeRegistry()
  const id = freshId()

  reg.bind(id, makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  reg.bind(id, makeBackendTarget({ kind: 'configured-profile', profile: 'coder' }))

  assert.deepEqual(reg.lookup(id), makeBackendTarget({ kind: 'configured-profile', profile: 'coder' }))
})

test('bind rejects an unvalidated target built by hand', () => {
  const reg = makeRegistry()
  const id = freshId()

  // Simulate a caller that constructed a target object without going through
  // makeBackendTarget — the registry must not trust arbitrary input.
  const bad = { kind: 'configured-profile', profile: 'Not Valid!' } as unknown as BackendTarget

  assert.throws(() => reg.bind(id, bad), /Invalid profile name/)
})

test('inheritFromOpener copies the opener target when the opener is bound', () => {
  const reg = makeRegistry()
  const opener = freshId()
  const child = freshId()

  reg.bind(opener, makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  reg.inheritFromOpener(child, opener)

  assert.deepEqual(reg.lookup(child), makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
})

test('inheritFromOpener falls back to primary when the opener has no explicit binding', () => {
  const reg = makeRegistry()
  const opener = freshId()
  const child = freshId()

  reg.inheritFromOpener(child, opener)

  assert.deepEqual(reg.lookup(child), makeBackendTarget({ kind: 'primary' }))
})

test('inheritFromOpener falls back to primary when the opener id is unknown', () => {
  const reg = makeRegistry()
  const child = freshId()

  // An opener id that was never registered and never bound.
  reg.inheritFromOpener(child, 999_999)

  assert.deepEqual(reg.lookup(child), makeBackendTarget({ kind: 'primary' }))
})

test('inheritFromOpener clears a stale child binding when the opener is unknown', () => {
  const reg = makeRegistry()
  const child = freshId()

  reg.bind(child, makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))
  reg.inheritFromOpener(child, 999_999)

  assert.deepEqual(reg.lookup(child), makeBackendTarget({ kind: 'primary' }))
})

test('inheritFromOpener does not create a binding for the opener as a side effect', () => {
  const reg = makeRegistry()
  const opener = freshId()
  const child = freshId()

  reg.inheritFromOpener(child, opener)

  // The opener itself remains unbound (still default-primary), proving
  // inheritFromOpener only created the child entry.
  assert.deepEqual(reg.lookup(opener), makeBackendTarget({ kind: 'primary' }))
})

test('cleanup removes the explicit binding and lookup reverts to primary', () => {
  const reg = makeRegistry()
  const id = freshId()

  reg.bind(id, makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  reg.cleanup(id)

  assert.deepEqual(reg.lookup(id), makeBackendTarget({ kind: 'primary' }))
})

test('cleanup on an unbound id is a no-op', () => {
  const reg = makeRegistry()
  const id = freshId()

  // Must not throw.
  reg.cleanup(id)

  assert.deepEqual(reg.lookup(id), makeBackendTarget({ kind: 'primary' }))
})

test('cleanup is idempotent', () => {
  const reg = makeRegistry()
  const id = freshId()

  reg.bind(id, makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' }))
  reg.cleanup(id)
  reg.cleanup(id)

  assert.deepEqual(reg.lookup(id), makeBackendTarget({ kind: 'primary' }))
})

test('distinct window ids can hold different targets independently', () => {
  const reg = makeRegistry()
  const a = freshId()
  const b = freshId()

  reg.bind(a, makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  reg.bind(b, makeBackendTarget({ kind: 'configured-profile', profile: 'coder' }))

  assert.deepEqual(reg.lookup(a), makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  assert.deepEqual(reg.lookup(b), makeBackendTarget({ kind: 'configured-profile', profile: 'coder' }))
})

test('revokeProfile preserves target identity while marking matching bindings revoked', () => {
  const reg = makeRegistry()
  const configured = freshId()
  const forcedLocal = freshId()
  const other = freshId()

  reg.bind(configured, makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  reg.bind(forcedLocal, makeBackendTarget({ kind: 'forced-local-profile', profile: 'worker' }))
  reg.bind(other, makeBackendTarget({ kind: 'configured-profile', profile: 'coder' }))

  assert.deepEqual(reg.revokeProfile('worker').sort((a, b) => a - b), [configured, forcedLocal].sort((a, b) => a - b))
  assert.deepEqual(reg.lookup(configured), makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  assert.deepEqual(reg.lookup(forcedLocal), makeBackendTarget({ kind: 'forced-local-profile', profile: 'worker' }))
  assert.deepEqual(reg.lookup(other), makeBackendTarget({ kind: 'configured-profile', profile: 'coder' }))
  assert.equal(reg.isRevoked(configured), true)
  assert.equal(reg.isRevoked(forcedLocal), true)
  assert.equal(reg.isRevoked(other), false)
})

test('a child inherits opener revocation and cleanup removes it', () => {
  const reg = makeRegistry()
  const opener = freshId()
  const child = freshId()

  reg.bind(opener, makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  reg.revokeProfile('worker')
  reg.inheritFromOpener(child, opener)

  assert.equal(reg.isRevoked(child), true)
  reg.cleanup(child)
  assert.equal(reg.isRevoked(child), false)
  assert.deepEqual(reg.lookup(child), makeBackendTarget({ kind: 'primary' }))
})

test('destroyRevokedWindows force-destroys only affected live windows', () => {
  const destroyed: number[] = []

  const windows = [1, 2, 3].map(id => ({
    destroy: () => destroyed.push(id),
    isDestroyed: () => id === 3,
    webContents: { id }
  }))

  destroyRevokedWindows([2, 3], windows)

  assert.deepEqual(destroyed, [2])
})

test('hasSameTarget reports whether singleton child and opener share a backend target', () => {
  const reg = makeRegistry()
  const child = freshId()
  const sameOpener = freshId()
  const otherOpener = freshId()

  reg.bind(child, makeBackendTarget({ kind: 'forced-local-profile', profile: 'worker' }))
  reg.bind(sameOpener, makeBackendTarget({ kind: 'forced-local-profile', profile: 'worker' }))
  reg.bind(otherOpener, makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))

  assert.equal(reg.hasSameTarget(child, sameOpener), true)
  assert.equal(reg.hasSameTarget(child, otherOpener), false)
})