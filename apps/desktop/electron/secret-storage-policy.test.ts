/**
 * Tests for electron/secret-storage-policy.ts — the "is OS-keychain
 * encryption enabled at all?" decision seam.
 *
 * The behavior this file pins: keychain-backed encryption is the safe default,
 * and plaintext is an explicit user choice. Once the one-shot legacy
 * migration has run, a
 * safeStorage blob under an opted-out policy reads as 'drop' — i.e. the
 * caller must treat it as absent WITHOUT touching safeStorage, so a broken
 * macOS login keychain can never raise its password dialog on launch.
 *
 * (Wired into the vitest `electron` project via electron/**\/*.test.ts.)
 */

import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  classifyStoredSecret,
  readSecretStoragePolicy,
  type SecretStoragePolicyIo,
  writeSecretStoragePolicy
} from './secret-storage-policy'

function fakeIo(initial: string | null = null): SecretStoragePolicyIo & { fileText: () => string | null } {
  let text = initial

  return {
    readText: () => {
      if (text === null) {
        throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' })
      }

      return text
    },
    writeText: (next: string) => {
      text = next
    },
    fileText: () => text
  }
}

// ── defaults ────────────────────────────────────────────────────────────────

test('missing policy file defaults to encryption ON, not migrated', () => {
  const policy = readSecretStoragePolicy(fakeIo())

  assert.deepEqual(policy, { on: true, migrated: false })
})

test('corrupt or non-object policy file defaults to encryption ON', () => {
  for (const bad of ['not-json', '[]', '"on"', 'null', '123']) {
    assert.deepEqual(readSecretStoragePolicy(fakeIo(bad)), { on: true, migrated: false })
  }
})

test('malformed policy objects default to encryption ON instead of plaintext', () => {
  for (const bad of [
    '{"on":1,"migrated":true}',
    '{"on":"yes","migrated":true}',
    '{"on":"true","migrated":true}',
    '{"on":false}',
    '{"on":false,"migrated":true,"future":false}',
    '{"on":false,"migrated":null}'
  ]) {
    assert.deepEqual(readSecretStoragePolicy(fakeIo(bad)), { on: true, migrated: false })
  }
})

test('duplicate policy members default to encryption ON instead of last-key-wins plaintext', () => {
  for (const bad of [
    '{"on":true,"on":false,"migrated":true}',
    '{"on":false,"migrated":false,"migrated":true}',
    '{"nested":{"on":true,"on":false},"on":false,"migrated":true}'
  ]) {
    assert.deepEqual(readSecretStoragePolicy(fakeIo(bad)), { on: true, migrated: false })
  }
})

test('round trip preserves both fields', () => {
  const io = fakeIo()

  writeSecretStoragePolicy({ on: true, migrated: true }, io)
  assert.deepEqual(readSecretStoragePolicy(io), { on: true, migrated: true })

  writeSecretStoragePolicy({ on: false, migrated: true }, io)
  assert.deepEqual(readSecretStoragePolicy(io), { on: false, migrated: true })
})

// ── classification ──────────────────────────────────────────────────────────

const SAFE_BLOB = { encoding: 'safeStorage', value: 'AAAA' }
const PLAIN_BLOB = { encoding: 'plain', value: 'tok' }

test('non-safeStorage blobs are always keep, under every policy', () => {
  for (const policy of [
    { on: false, migrated: false },
    { on: false, migrated: true },
    { on: true, migrated: true }
  ]) {
    assert.equal(classifyStoredSecret(PLAIN_BLOB, policy), 'keep')
    assert.equal(classifyStoredSecret(null, policy), 'keep')
    assert.equal(classifyStoredSecret(undefined, policy), 'keep')
    assert.equal(classifyStoredSecret({} as any, policy), 'keep')
  }
})

test('safeStorage blob with encryption ON is keep', () => {
  assert.equal(classifyStoredSecret(SAFE_BLOB, { on: true, migrated: true }), 'keep')
})

test('safeStorage blob, encryption OFF, pre-migration is migrate', () => {
  assert.equal(classifyStoredSecret(SAFE_BLOB, { on: false, migrated: false }), 'migrate')
})

test('safeStorage blob, encryption OFF, post-migration is drop — never touch the keychain again', () => {
  assert.equal(classifyStoredSecret(SAFE_BLOB, { on: false, migrated: true }), 'drop')
})
