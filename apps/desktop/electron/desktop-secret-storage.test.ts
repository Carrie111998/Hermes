import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createDesktopSecretStorage } from './desktop-secret-storage'

function storageFixture(available = true) {
  let encryptCalls = 0

  const safeStorage = {
    isEncryptionAvailable: () => available,
    encryptString: (_value: string) => {
      encryptCalls += 1

      return Buffer.from('opaque-ciphertext')
    },
    decryptString: (value: Buffer) => (value.toString() === 'opaque-ciphertext' ? 'secret-value' : '')
  }

  const getPolicy = () => ({ on: true, migrated: false })

  const codec = createDesktopSecretStorage({
    getPolicy,
    safeStorage,
    classifyStoredSecret: () => 'keep',
    normalizeRemoteHeaders: (headers: unknown) =>
      headers && typeof headers === 'object' ? (headers as Record<string, unknown>) : {},
    safeStorageEncoding: 'safeStorage',
    encryptStrict: (value, api, options = {}) => {
      const allowPlainText = (options as { allowPlainText?: boolean }).allowPlainText === true

      if (!(api as typeof safeStorage).isEncryptionAvailable()) {
        if (allowPlainText) {
          return { encoding: 'plain', value }
        }

        throw new Error('secure storage unavailable')
      }

      return { encoding: 'safeStorage', value: api.encryptString(value).toString('base64') }
    }
  })

  return { codec, safeStorage, encryptCalls: () => encryptCalls }
}

test('original namespace aliases resolve to the extracted codec function objects', () => {
  const { codec } = storageFixture()
  const originalNamespace = {
    encryptDesktopSecret: codec.encryptDesktopSecret,
    decryptDesktopSecret: codec.decryptDesktopSecret,
    decryptRemoteHeaders: codec.decryptRemoteHeaders,
    encryptIncomingRemoteHeaders: codec.encryptIncomingRemoteHeaders
  }

  for (const name of Object.keys(originalNamespace) as Array<keyof typeof originalNamespace>) {
    assert.strictEqual(originalNamespace[name], codec[name])
    assert.equal(typeof originalNamespace[name], 'function')
  }
})

test('secret codec persists token and headers as opaque safeStorage envelopes', () => {
  const { codec } = storageFixture()

  const token = codec.encryptDesktopSecret('token-bytes')
  const headers = codec.encryptIncomingRemoteHeaders({ Authorization: 'header-bytes' }, {})

  assert.deepEqual(token, { encoding: 'safeStorage', value: Buffer.from('opaque-ciphertext').toString('base64') })
  assert.deepEqual(headers.Authorization, token)
  assert.equal(JSON.stringify({ token, headers }).includes('token-bytes'), false)
  assert.equal(JSON.stringify({ token, headers }).includes('header-bytes'), false)
})

test('plaintext storage requires an explicit opt-in when safeStorage is unavailable', () => {
  const { codec } = storageFixture(false)

  assert.throws(() => codec.encryptDesktopSecret('token-bytes'), /secure storage unavailable/)
  assert.deepEqual(codec.encryptDesktopSecret('token-bytes', { allowPlainText: true }), {
    encoding: 'plain',
    value: 'token-bytes'
  })
})

test('renderer-supplied header envelopes fail closed instead of bypassing safeStorage', () => {
  const { codec, encryptCalls } = storageFixture()

  for (const envelope of [
    { encoding: 'plain', value: 'plaintext-header' },
    { encoding: 'safeStorage', value: 'caller-controlled-ciphertext' },
    { encoding: 'unknown', value: 'untrusted-header' },
    Object.assign(Object.create({ encoding: 'plain' }), { value: 'prototype-header' })
  ]) {
    assert.throws(
      () => codec.encryptIncomingRemoteHeaders({ 'X-E2E-Access-Secret': envelope }, {}),
      /header envelopes must be supplied as plaintext values or omitted/
    )
  }

  assert.equal(encryptCalls(), 0)
})

test('plaintext header strings still use the explicit opt-out only when secure storage is unavailable', () => {
  const { codec, encryptCalls } = storageFixture(false)

  assert.throws(
    () => codec.encryptIncomingRemoteHeaders({ 'X-E2E-Access-Secret': 'header-bytes' }, {}),
    /secure storage unavailable/
  )
  assert.deepEqual(
    codec.encryptIncomingRemoteHeaders({ 'X-E2E-Access-Secret': 'header-bytes' }, {}, { allowPlainText: true }),
    { 'X-E2E-Access-Secret': { encoding: 'plain', value: 'header-bytes' } }
  )
  assert.equal(encryptCalls(), 0)
})
