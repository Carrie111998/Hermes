import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

import { test } from 'vitest'

import { normalizeRemoteHeaders } from './connection-config'
import { createDesktopSecretStorage } from './desktop-secret-storage'
import { createSecretStorageConnections } from './secret-storage-connections'
import { classifyStoredSecret, readSecretStoragePolicy, writeSecretStoragePolicy } from './secret-storage-policy'

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

function connectionStorageFixture(initialPolicy: { on: boolean; migrated: boolean }) {
  const root = fs.mkdtempSync('C:/TEMP/hermes-redteam-f751a8c5/r3-cand-005-secret-storage-')
  const configPath = path.join(root, 'connection.json')
  const registryPath = path.join(root, 'connections.json')
  const policyPath = path.join(root, 'secret-storage-policy.json')
  const config = {
    mode: 'remote',
    remote: {
      url: 'https://gateway.example.test/hermes',
      token: 'legacy-token-value',
      headers: {
        'CF-Access-Client-Id': 'legacy-header-value'
      }
    },
    profiles: {}
  }
  const registry = { version: 2, primary: 'local', launchMode: 'primary', lastUsed: 'local', connections: [] }
  let encryptCalls = 0

  fs.writeFileSync(configPath, JSON.stringify(config))
  fs.writeFileSync(registryPath, JSON.stringify(registry))
  fs.writeFileSync(policyPath, JSON.stringify(initialPolicy))

  const safeStorage = {
    isEncryptionAvailable: () => true,
    encryptString: (value: string) => {
      encryptCalls += 1
      return Buffer.from(value)
    },
    decryptString: (value: Buffer) => value.toString()
  }
  const writeSecretFileAtomic = (target: string, text: string) => {
    fs.mkdirSync(path.dirname(target), { recursive: true })
    fs.writeFileSync(target, text)
  }
  const connections = createSecretStorageConnections({
    DESKTOP_CONNECTIONS_REGISTRY_PATH: registryPath,
    DESKTOP_CONNECTION_CONFIG_PATH: configPath,
    PROFILE_NAME_RE: /^[A-Za-z0-9_-]+$/,
    SAFE_STORAGE_ENCODING: 'safeStorage',
    SECRET_STORAGE_POLICY_FILE: 'secret-storage-policy.json',
    _nativeTokenStoreIo: () => ({}),
    app: { getPath: () => root },
    classifyStoredSecret,
    connectionConfigCache: null,
    connectionConfigCacheMtime: null,
    connectionRegistryCache: null,
    connectionRegistryCacheMtime: null,
    createDesktopSecretStorage,
    encryptDesktopSecretStrict: (value: string, api: typeof safeStorage) => {
      if (!api.isEncryptionAvailable()) {
        throw new Error('secure storage unavailable')
      }

      return { encoding: 'safeStorage', value: api.encryptString(value).toString('base64') }
    },
    fs,
    normalizeRegistry: (value: any) => value,
    normalizeRemoteHeaders,
    modeIsRemoteLike: (mode: string) => mode === 'remote' || mode === 'cloud',
    path,
    readSecretStoragePolicy,
    reconcileRegistryDrift: (_registry: any) => ({ changed: false, registry }),
    rememberLog: () => undefined,
    rewriteNativeTokenStore: () => false,
    safeStorage,
    tightenSecretFileMode: () => undefined,
    writeSecretFileAtomic,
    writeSecretStoragePolicy
  })

  return {
    connections,
    configPath,
    policyPath,
    encryptCalls: () => encryptCalls,
    cleanup: () => fs.rmSync(root, { recursive: true, force: true })
  }
}

test('legacy bare-string global headers and tokens are encrypted during migration', () => {
  const fixture = connectionStorageFixture({ on: true, migrated: false })

  try {
    fixture.connections.migrateLegacyEncryptedSecretsOnce()

    const saved = JSON.parse(fs.readFileSync(fixture.configPath, 'utf8'))
    const serialized = JSON.stringify(saved)

    assert.equal(saved.remote.url, 'https://gateway.example.test/hermes')
    assert.deepEqual(fixture.connections.decryptRemoteHeaders(saved.remote.headers), {
      'CF-Access-Client-Id': 'legacy-header-value'
    })
    assert.equal(fixture.connections.decryptDesktopSecret(saved.remote.token), 'legacy-token-value')
    assert.equal(serialized.includes('legacy-token-value'), false)
    assert.equal(serialized.includes('legacy-header-value'), false)
    assert.deepEqual(JSON.parse(fs.readFileSync(fixture.policyPath, 'utf8')), { on: true, migrated: true })
    assert.equal(fixture.encryptCalls(), 2)
  } finally {
    fixture.cleanup()
  }
})

test('enabling encryption rewrites legacy bare-string headers and tokens before secure policy commit', () => {
  const fixture = connectionStorageFixture({ on: false, migrated: true })

  try {
    assert.deepEqual(fixture.connections.applySecretStorageEncryption(true), { on: true })

    const saved = JSON.parse(fs.readFileSync(fixture.configPath, 'utf8'))
    const serialized = JSON.stringify(saved)

    assert.equal(saved.remote.url, 'https://gateway.example.test/hermes')
    assert.deepEqual(fixture.connections.decryptRemoteHeaders(saved.remote.headers), {
      'CF-Access-Client-Id': 'legacy-header-value'
    })
    assert.equal(fixture.connections.decryptDesktopSecret(saved.remote.token), 'legacy-token-value')
    assert.equal(serialized.includes('legacy-token-value'), false)
    assert.equal(serialized.includes('legacy-header-value'), false)
    assert.deepEqual(JSON.parse(fs.readFileSync(fixture.policyPath, 'utf8')), { on: true, migrated: true })
    assert.equal(fixture.encryptCalls(), 2)
  } finally {
    fixture.cleanup()
  }
})
