import assert from 'node:assert/strict'
import test from 'node:test'

import { resolveNotarizationConfig } from './notarize.mjs'

test('ordinary development packaging may skip notarization', () => {
  assert.equal(resolveNotarizationConfig({}), null)
})

test('managed release packaging fails closed without complete notarization credentials', () => {
  assert.throws(() => resolveNotarizationConfig({ EVAOS_MANAGED_RELEASE: '1' }), /requires APPLE_NOTARY_PROFILE/)
  assert.throws(
    () =>
      resolveNotarizationConfig({
        APPLE_API_KEY: 'partial',
        APPLE_API_KEY_ID: 'KEY',
        EVAOS_MANAGED_RELEASE: '1'
      }),
    /complete APPLE_API_KEY/
  )
})

test('a keychain notary profile satisfies the managed release preflight', () => {
  assert.deepEqual(
    resolveNotarizationConfig({
      APPLE_NOTARY_PROFILE: 'electric-sheep-release',
      EVAOS_MANAGED_RELEASE: '1'
    }),
    { kind: 'profile', profile: 'electric-sheep-release' }
  )
})

test('complete API-key credentials satisfy the managed release preflight', () => {
  assert.deepEqual(
    resolveNotarizationConfig({
      APPLE_API_ISSUER: 'issuer',
      APPLE_API_KEY: 'key material',
      APPLE_API_KEY_ID: 'key-id',
      EVAOS_MANAGED_RELEASE: '1'
    }),
    {
      apiKey: 'key material',
      issuer: 'issuer',
      keyId: 'key-id',
      kind: 'api-key'
    }
  )
})
