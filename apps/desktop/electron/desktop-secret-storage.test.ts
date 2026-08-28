import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createDesktopSecretStorage } from './desktop-secret-storage'

function storageFixture(available = true) {
  const safeStorage = {
    isEncryptionAvailable: () => available,
    encryptString: (_value: string) => Buffer.from('opaque-ciphertext'),
    decryptString: (value: Buffer) => (value.toString() === 'opaque-ciphertext' ? 'secret-value' : '')
  }
  const getPolicy = () => ({ on: true, migrated: false })
  const codec = createDesktopSecretStorage({
    getPolicy,
    safeStorage,
    classifyStoredSecret: () => 'keep',
    normalizeRemoteHeaders: (headers: unknown) => (headers && typeof headers === 'object' ? headers as Record<string, unknown> : {}),
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

  return { codec, safeStorage }
}

test('secret codec persists token and headers as opaque safeStorage envelopes', () => {
  const { codec } = storageFixture()

  const token = codec.encryptDesktopSecret('token-bytes')
  const headers = codec.encryptIncomingRemoteHeaders({ Authorization: 'header-bytes' }, {})

  assert.deepEqual(token, { encoding: 'safeStorage', value: Buffer.from('opaque-ciphertext').toString('base64') })
  assert.deepEqual(headers, { Authorization: token })
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
